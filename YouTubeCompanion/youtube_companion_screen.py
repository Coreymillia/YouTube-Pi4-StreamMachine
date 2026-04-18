#!/usr/bin/env python3
"""
Fullscreen HDMI status screen for the YouTube Companion.

This app runs locally on the Pi Zero, polls the companion's JSON endpoints,
and renders a lightweight animated background plus large status cards that are
readable from across the room.
"""

import argparse
import ctypes
import ctypes.util
import json
import math
import os
import random
import signal
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PROJECT_DIR, 'config.json')

try:
    import pygame
except ImportError as exc:
    raise SystemExit('Install python3-pygame to use the HDMI display.') from exc


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clip(text, limit):
    value = str(text or '').strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + '...'


def _fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _load_screen_cfg():
    cfg = {
        'streamer_status_host': '192.168.0.123',
        'streamer_status_port': 8090,
    }
    try:
        with open(_CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    cfg['streamer_status_host'] = str(cfg.get('streamer_status_host', '192.168.0.123')).strip()
    try:
        cfg['streamer_status_port'] = max(1, int(cfg.get('streamer_status_port', 8090)))
    except (TypeError, ValueError):
        cfg['streamer_status_port'] = 8090
    return cfg


def _streamer_url(args):
    if args.streamer_status_url:
        return args.streamer_status_url
    cfg = _load_screen_cfg()
    host = cfg.get('streamer_status_host', '')
    if not host:
        return ''
    return f"http://{host}:{cfg['streamer_status_port']}/status"


def _blank_streamer_state():
    return {
        'online': False,
        'running': False,
        'uptime_s': 0,
        'retries': 0,
        'cam_name': '',
        'preview_cam': '',
        'audio_name': '',
        'audio_silent': False,
        'error': '',
        'rtmp_state': '',
        'eth_carrier': False,
        'eth_oper': '',
        'tx_bytes': 0,
        'rx_bytes': 0,
        'tx_kbps': 0,
        'rx_kbps': 0,
        'temp_c': None,
        'throttled': '',
        'msg_enabled': False,
        'msg_text': '',
    }


def _parse_streamer_state(raw, previous, now):
    state = _blank_streamer_state()
    if not isinstance(raw, dict):
        return state, previous

    state['online'] = True
    state['running'] = bool(raw.get('running'))
    state['uptime_s'] = _safe_int(raw.get('uptime_s'))
    state['retries'] = _safe_int(raw.get('retries'))
    state['cam_name'] = str(raw.get('cam_name') or '')
    state['preview_cam'] = str(raw.get('preview_cam') or '')
    state['audio_name'] = str(raw.get('audio_name') or '')
    state['audio_silent'] = bool(raw.get('audio_silent'))
    state['error'] = str(raw.get('error') or '')
    state['rtmp_state'] = str(raw.get('rtmp_state') or '')

    eth = raw.get('eth0') or {}
    state['eth_carrier'] = bool(eth.get('carrier'))
    state['eth_oper'] = str(eth.get('operstate') or '')
    state['tx_bytes'] = _safe_int(eth.get('tx_bytes'))
    state['rx_bytes'] = _safe_int(eth.get('rx_bytes'))

    sys = raw.get('system') or {}
    try:
        state['temp_c'] = float(sys.get('temp_c')) if sys.get('temp_c') is not None else None
    except (TypeError, ValueError):
        state['temp_c'] = None
    state['throttled'] = str(sys.get('throttled') or '')

    msg = raw.get('stream_message') or {}
    state['msg_enabled'] = bool(msg.get('enabled'))
    state['msg_text'] = str(msg.get('text') or '')

    prev_tx, prev_rx, prev_time = previous
    if prev_time and now > prev_time:
        dt_ms = max(1.0, (now - prev_time) * 1000.0)
        tx_delta = max(0, state['tx_bytes'] - prev_tx)
        rx_delta = max(0, state['rx_bytes'] - prev_rx)
        state['tx_kbps'] = int((tx_delta * 8.0) / dt_ms)
        state['rx_kbps'] = int((rx_delta * 8.0) / dt_ms)
    return state, (state['tx_bytes'], state['rx_bytes'], now)


def _active_cam_label(streamer):
    if streamer.get('cam_name'):
        return streamer['cam_name']
    if streamer.get('preview_cam'):
        return streamer['preview_cam']
    return 'No camera'


def _fmt_uptime(seconds):
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


def _display_drivers(windowed):
    if windowed:
        return [None]

    discovered = []
    try:
        lib_name = ctypes.util.find_library('SDL2') or 'libSDL2-2.0.so.0'
        sdl = ctypes.CDLL(lib_name)
        sdl.SDL_GetNumVideoDrivers.restype = ctypes.c_int
        sdl.SDL_GetVideoDriver.argtypes = [ctypes.c_int]
        sdl.SDL_GetVideoDriver.restype = ctypes.c_char_p
        for i in range(sdl.SDL_GetNumVideoDrivers()):
            name = sdl.SDL_GetVideoDriver(i)
            if name:
                discovered.append(name.decode())
    except Exception:
        discovered = []

    seen = set()
    drivers = []
    for driver in (
        os.environ.get('SDL_VIDEODRIVER', '').strip() or None,
        'KMSDRM',
        'kmsdrm',
        'fbcon',
        'directfb',
        None,
    ):
        if driver in seen:
            continue
        seen.add(driver)
        drivers.append(driver)
    for driver in discovered:
        if driver.lower() in {'offscreen', 'dummy', 'evdev'}:
            continue
        if driver in seen:
            continue
        seen.add(driver)
        drivers.append(driver)
    return drivers


def _local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


def _derive_mode(status, auth, fetch_error):
    if fetch_error:
        return 'offline'
    if auth.get('pending'):
        return 'auth'
    if not status.get('authorized'):
        return 'auth'
    if status.get('error'):
        return 'warning'

    issues = status.get('issues') or []
    for issue in issues:
        if str(issue.get('severity', '')).lower() in {'error', 'warning'}:
            return 'warning'

    broadcast = status.get('broadcast') or {}
    stream = status.get('stream') or {}
    life_cycle = str(broadcast.get('life_cycle_status', '')).lower()
    stream_status = str(stream.get('stream_status', '')).lower()
    if life_cycle in {'live', 'testing'} or stream_status == 'active':
        return 'live'
    return 'ready'


def _resolve_display_mode(base_mode, idle_since, now, idle_seconds, streamer_running, streamer_online):
    if not streamer_online and base_mode not in {'offline', 'auth'}:
        return 'streamer_offline', None
    if base_mode == 'ready':
        if streamer_running:
            return 'ready', None
        if idle_since is None:
            idle_since = now
        if now - idle_since >= idle_seconds:
            return 'idle', idle_since
        return 'ready', idle_since
    return base_mode, None


class DotField:
    def __init__(self, width, height, count):
        self.width = width
        self.height = height
        self.dots = [self._spawn(initial=True) for _ in range(count)]

    def resize(self, width, height):
        self.width = width
        self.height = height
        for dot in self.dots:
            dot['x'] = min(dot['x'], width)
            dot['y'] = min(dot['y'], height)

    def _spawn(self, initial=False):
        return {
            'x': random.uniform(0, self.width),
            'y': random.uniform(0, self.height) if initial else random.uniform(-40, 0),
            'speed': random.uniform(16, 44),
            'radius': random.choice((1, 1, 1, 2, 2, 3)),
            'phase': random.uniform(0, math.tau),
            'sway': random.uniform(5, 22),
            'color_shift': random.random(),
        }

    def update(self, dt, mode):
        speed_factor = {
            'offline': 0.35,
            'auth': 0.45,
            'ready': 0.6,
            'streamer_offline': 0.4,
            'live': 0.4,
            'warning': 0.55,
        }.get(mode, 0.5)
        for idx, dot in enumerate(self.dots):
            dot['phase'] += dt * (0.35 + idx % 5 * 0.03)
            dot['y'] += dot['speed'] * speed_factor * dt
            dot['x'] += math.sin(dot['phase']) * dot['sway'] * dt
            if dot['y'] - dot['radius'] > self.height or dot['x'] < -20 or dot['x'] > self.width + 20:
                self.dots[idx] = self._spawn()

    def draw(self, surface, mode, now):
        palettes = {
            'offline': ((50, 90, 140), (110, 140, 180)),
            'auth': ((120, 170, 240), (255, 210, 110)),
            'ready': ((0, 220, 160), (70, 190, 255)),
            'streamer_offline': ((28, 140, 74), (76, 210, 108)),
            'live': ((0, 190, 120), (60, 150, 235)),
            'warning': ((255, 120, 80), (255, 210, 90)),
        }
        color_a, color_b = palettes.get(mode, palettes['ready'])
        for dot in self.dots:
            blend = 0.5 + 0.5 * math.sin(now * 0.7 + dot['phase'] + dot['color_shift'] * math.pi)
            color = (
                int(color_a[0] * (1 - blend) + color_b[0] * blend),
                int(color_a[1] * (1 - blend) + color_b[1] * blend),
                int(color_a[2] * (1 - blend) + color_b[2] * blend),
            )
            pygame.draw.circle(surface, color, (int(dot['x']), int(dot['y'])), dot['radius'])


class MatrixRain:
    _CHARS = '01[]{}<>/\\+-=*#@!?$%&'

    def __init__(self, width, height, cell_width=16, cell_height=18):
        self.width = width
        self.height = height
        self.cell_width = max(10, cell_width)
        self.cell_height = max(12, cell_height)
        self._build_streams()

    def _build_streams(self):
        self.columns = max(12, self.width // self.cell_width)
        self.streams = [self._spawn(col, initial=True) for col in range(self.columns)]

    def resize(self, width, height):
        self.width = width
        self.height = height
        self._build_streams()

    def _spawn(self, col, initial=False):
        length = random.randint(10, 22)
        head_y = random.uniform(-self.height * 0.8, self.height if initial else 0)
        return {
            'col': col,
            'head_y': head_y,
            'speed': random.uniform(42, 92),
            'length': length,
            'chars': [random.choice(self._CHARS) for _ in range(length + 6)],
            'last_row': int(head_y // self.cell_height),
            'mutate_timer': random.uniform(0.02, 0.18),
        }

    def update(self, dt):
        for idx, stream in enumerate(self.streams):
            stream['head_y'] += stream['speed'] * dt
            stream['mutate_timer'] -= dt
            if stream['mutate_timer'] <= 0:
                replace_at = random.randrange(len(stream['chars']))
                stream['chars'][replace_at] = random.choice(self._CHARS)
                stream['mutate_timer'] = random.uniform(0.04, 0.22)

            row = int(stream['head_y'] // self.cell_height)
            if row != stream['last_row']:
                stream['chars'].insert(0, random.choice(self._CHARS))
                del stream['chars'][-1]
                stream['last_row'] = row

            if stream['head_y'] - (stream['length'] * self.cell_height) > self.height + self.cell_height:
                self.streams[idx] = self._spawn(stream['col'])

    def glyphs(self):
        for stream in self.streams:
            col_x = stream['col'] * self.cell_width + 6
            head_row = int(stream['head_y'] // self.cell_height)
            for offset in range(stream['length']):
                row = head_row - offset
                if row < 0:
                    continue
                y = row * self.cell_height
                if y >= self.height:
                    continue
                if offset == 0:
                    color = (186, 255, 190)
                elif offset == 1:
                    color = (110, 240, 126)
                else:
                    fade = max(0.12, 1.0 - (offset / max(1, stream['length'])))
                    color = (0, int(160 * fade) + 10, 0)
                yield col_x, y, stream['chars'][offset], color


class CompanionScreen:
    def __init__(self, args):
        self.args = args
        pygame.init()
        pygame.font.init()

        self.display_driver = ''
        self.screen = self._init_display()
        pygame.display.set_caption('YouTube Companion HDMI')
        pygame.mouse.set_visible(False)

        self.clock = pygame.time.Clock()
        self.width, self.height = self.screen.get_size()
        self.hostname = socket.gethostname()
        self.ip_address = _local_ip()

        self.status = {}
        self.auth = {}
        self.fetch_error = ''
        self.last_refresh = 0.0
        self.last_ok = 0.0
        self.idle_since = None
        self.streamer = _blank_streamer_state()
        self.streamer_status_url = _streamer_url(args)
        self._streamer_prev_sample = (0, 0, 0.0)

        self.dots = DotField(self.width, self.height, max(120, (self.width * self.height) // 18000))
        self.matrix = MatrixRain(self.width, self.height)
        self._build_fonts()

    def _init_display(self):
        flags = pygame.RESIZABLE if self.args.windowed else pygame.FULLSCREEN
        resolution = (
            (self.args.width, self.args.height)
            if self.args.windowed and self.args.width and self.args.height
            else (0, 0)
        )
        errors = []

        for driver in _display_drivers(self.args.windowed):
            if driver:
                os.environ['SDL_VIDEODRIVER'] = driver
            else:
                os.environ.pop('SDL_VIDEODRIVER', None)

            if driver == 'fbcon':
                os.environ.setdefault('SDL_FBDEV', '/dev/fb0')

            try:
                pygame.display.init()
                screen = pygame.display.set_mode(resolution, flags)
                self.display_driver = pygame.display.get_driver()
                return screen
            except pygame.error as exc:
                errors.append(f'{driver or "default"}: {exc}')
                pygame.display.quit()

        raise RuntimeError('No usable SDL video driver. ' + '; '.join(errors))

    def _build_fonts(self):
        short_edge = min(self.width, self.height)
        self.font_title = pygame.font.SysFont('dejavusansmono', max(18, short_edge // 26), bold=True)
        self.font_mode = pygame.font.SysFont('dejavusansmono', max(28, short_edge // 17), bold=True)
        self.font_section = pygame.font.SysFont('dejavusansmono', max(15, short_edge // 38), bold=True)
        self.font_text = pygame.font.SysFont('dejavusansmono', max(13, short_edge // 48))
        self.font_small = pygame.font.SysFont('dejavusansmono', max(11, short_edge // 64))
        self.font_matrix = pygame.font.SysFont('dejavusansmono', max(14, short_edge // 30), bold=True)
        self.font_code = pygame.font.SysFont('dejavusansmono', max(34, short_edge // 11), bold=True)

    def _refresh_if_needed(self):
        now = time.time()
        if now - self.last_refresh < self.args.poll_seconds:
            return
        self.last_refresh = now
        self.streamer_status_url = _streamer_url(self.args)
        try:
            self.status = _fetch_json(self.args.status_url, self.args.http_timeout)
            self.auth = _fetch_json(self.args.auth_url, self.args.http_timeout)
            self.fetch_error = ''
            self.last_ok = now
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.fetch_error = str(exc)
        if self.streamer_status_url:
            try:
                raw = _fetch_json(self.streamer_status_url, self.args.http_timeout)
                self.streamer, self._streamer_prev_sample = _parse_streamer_state(raw, self._streamer_prev_sample, now)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
                self.streamer = _blank_streamer_state()
        else:
            self.streamer = _blank_streamer_state()

    def _accent(self, mode):
        pulse = 0.65 + 0.35 * math.sin(time.time() * 4.0)
        colors = {
            'offline': (100, 140, 190),
            'auth': (255, 196, 86),
            'ready': (78, 195, 255),
            'idle': (78, 185, 104),
            'streamer_offline': (72, 188, 94),
            'live': (92, 220, 132),
            'warning': (255, int(120 + 70 * pulse), 90),
        }
        return colors.get(mode, colors['ready'])

    def _mode_label(self, mode):
        return {
            'offline': 'OFFLINE',
            'auth': 'AUTH',
            'ready': 'READY',
            'idle': 'IDLE',
            'streamer_offline': 'PI4 OFFLINE',
            'live': 'LIVE',
            'warning': 'WARNING',
        }[mode]

    def _wrap_lines(self, text, width, font):
        words = str(text or '').split()
        if not words:
            return []
        lines = []
        current = words[0]
        for word in words[1:]:
            trial = current + ' ' + word
            if font.size(trial)[0] <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _draw_text(self, text, font, color, x, y):
        surf = font.render(text, True, color)
        self.screen.blit(surf, (x, y))
        return surf.get_height()

    def _draw_card(self, rect, title, lines, accent, title_color=(245, 245, 245)):
        x, y, w, h = rect
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        panel.fill((12, 18, 24, 208))
        pygame.draw.rect(panel, (*accent, 230), panel.get_rect(), width=2, border_radius=18)
        self.screen.blit(panel, (x, y))

        title_h = self.font_section.render(title, True, title_color)
        self.screen.blit(title_h, (x + 12, y + 10))
        line_y = y + 14 + title_h.get_height() + 6
        for label, value, color in lines:
            label_text = f'{label}: ' if label else ''
            line = label_text + value
            wrapped = self._wrap_lines(line, w - 24, self.font_text) or ['']
            for item in wrapped[:3]:
                line_y += self._draw_text(item, self.font_text, color, x + 12, line_y)
                line_y += 3
                if line_y >= y + h - 14:
                    return

    def _summary_lines(self, mode):
        broadcast = self.status.get('broadcast') or {}
        stream = self.status.get('stream') or {}
        issues = self.status.get('issues') or []
        accent = self._accent(mode)

        if mode == 'offline':
            return [
                ('COMPANION', 'The local YouTube companion service is not responding.', (245, 245, 245)),
                ('CHECK', 'Make sure youtube-companion.service is running on this Pi.', (214, 214, 214)),
                ('TARGET', self.args.status_url, accent),
            ]

        if mode == 'auth':
            if self.auth.get('pending'):
                return [
                    ('ACTION', 'Open google.com/device and enter the code below.', (245, 245, 245)),
                    ('URL', self.auth.get('verification_url') or 'https://www.google.com/device', accent),
                    ('STATE', 'Waiting for Google approval...', (214, 214, 214)),
                ]
            return [
                ('ACTION', 'Open the companion web UI and start device auth.', (245, 245, 245)),
                ('WEB', self.args.web_url, accent),
                ('STATE', 'No saved YouTube token yet.', (214, 214, 214)),
            ]

        title = _clip(broadcast.get('title') or 'No active broadcast yet', 84)
        lines = [
            ('BROADCAST', title, (245, 245, 245)),
            ('LIFECYCLE', broadcast.get('life_cycle_status') or 'none', accent),
            ('STREAM', stream.get('stream_status') or 'unknown', (214, 214, 214)),
            ('HEALTH', stream.get('health_status') or 'unknown', (214, 214, 214)),
        ]
        if mode == 'warning' and issues:
            first = issues[0]
            lines.append(('ISSUE', _clip(first.get('description') or first.get('reason') or first.get('type') or 'Warning reported', 86), (255, 196, 160)))
        return lines

    def _streamer_overview_lines(self, mode):
        accent = self._accent(mode)
        if not self.streamer['online']:
            return [
                ('STREAMER', 'Pi 4 status offline', (245, 245, 245)),
                ('TARGET', self.streamer_status_url or 'Not configured', accent),
            ]
        state = 'LIVE' if self.streamer['running'] else 'IDLE'
        if self.streamer['running'] and self.streamer['retries'] > 0:
            state = 'RECOVER'
        audio = self.streamer['audio_name'] or 'No audio'
        if self.streamer['audio_silent']:
            audio = 'No Audio'
        return [
            ('STATE', state, accent),
            ('CAM', _active_cam_label(self.streamer), (245, 245, 245)),
            ('AUDIO', audio, (214, 214, 214)),
        ]

    def _streamer_network_lines(self, mode):
        accent = self._accent(mode)
        if not self.streamer['online']:
            return [('INFO', 'Waiting for streamer status', accent)]
        eth = 'up ' + (self.streamer['eth_oper'] or '-') if self.streamer['eth_carrier'] else 'down ' + (self.streamer['eth_oper'] or '-')
        return [
            ('UP / RET', f"{_fmt_uptime(self.streamer['uptime_s'])}   r:{self.streamer['retries']}", (245, 245, 245)),
            ('ETH / RTMP', f"{eth.strip()}   {self.streamer['rtmp_state'] or '-'}", (214, 214, 214)),
            ('LAN', f"tx:{self.streamer['tx_kbps']}   rx:{self.streamer['rx_kbps']} kbps", accent),
        ]

    def _streamer_system_lines(self, mode):
        accent = self._accent(mode)
        if not self.streamer['online']:
            return [('INFO', 'No local Pi 4 system data yet', accent)]
        temp = f"{self.streamer['temp_c']:.1f} C" if self.streamer['temp_c'] is not None else '-'
        message = self.streamer['msg_text'] if self.streamer['msg_enabled'] and self.streamer['msg_text'] else 'off'
        return [
            ('TEMP / THR', f"{temp}   {self.streamer['throttled'] or '-'}", (245, 245, 245)),
            ('MSG', _clip(message, 52), accent if self.streamer['msg_enabled'] else (214, 214, 214)),
        ]

    def _audience_lines(self, mode):
        audience = self.status.get('audience') or {}
        views = audience.get('views')
        avg_view = audience.get('average_view_duration_label') or 'n/a'
        ccv = audience.get('concurrent_viewers')
        note = audience.get('note') or ''
        lines = [
            ('VIEWS', str(views) if views is not None else 'n/a', (245, 245, 245)),
            ('AVG VIEW', avg_view, (214, 214, 214)),
            ('CONCURRENT', str(ccv) if ccv is not None else 'n/a', (214, 214, 214)),
        ]
        if note:
            lines.append(('NOTE', _clip(note, 82), self._accent(mode)))
        return lines

    def _system_lines(self, mode):
        updated_at = _safe_int(self.status.get('updated_at'))
        updated_label = datetime.fromtimestamp(updated_at).strftime('%H:%M:%S') if updated_at else 'n/a'
        stale_seconds = int(max(0, time.time() - self.last_ok)) if self.last_ok else None
        if self.fetch_error:
            freshness = f'No update for {stale_seconds}s' if stale_seconds is not None else 'Waiting for first refresh'
        else:
            freshness = 'Fresh' if stale_seconds is not None and stale_seconds < max(10, self.args.poll_seconds * 2) else f'{stale_seconds}s old'
        lines = [
            ('HOST', self.hostname, (245, 245, 245)),
            ('IP', self.ip_address, (214, 214, 214)),
            ('UPDATED', updated_label, (214, 214, 214)),
            ('LINK', freshness, self._accent(mode)),
        ]
        if self.status.get('error') and mode != 'offline':
            lines.append(('ERROR', _clip(self.status['error'], 82), (255, 180, 180)))
        return lines

    def _draw_header(self, mode):
        accent = self._accent(mode)
        now_text = datetime.now().strftime('%H:%M:%S')
        badge = self.font_mode.render(self._mode_label(mode), True, accent)
        badge_rect = badge.get_rect()
        badge_rect.topleft = (18, 10)
        self.screen.blit(badge, badge_rect)
        self._draw_text(now_text, self.font_title, (232, 244, 255), self.width - 18 - self.font_title.size(now_text)[0], 12)
        if self.streamer['online']:
            state = 'LIVE' if self.streamer['running'] else 'IDLE'
            if self.streamer['running'] and self.streamer['retries'] > 0:
                state = 'RECOVER'
            label = f'PI4 {state}'
            self._draw_text(label, self.font_small, (148, 165, 180), self.width - 18 - self.font_small.size(label)[0], 36)

    def _draw_auth_code(self):
        if not self.auth.get('pending'):
            return
        code = self.auth.get('user_code') or '---- ----'
        surf = self.font_code.render(code, True, (255, 232, 176))
        rect = surf.get_rect(center=(self.width // 2, int(self.height * 0.34)))
        glow = pygame.Surface((rect.width + 60, rect.height + 30), pygame.SRCALPHA)
        pygame.draw.rect(glow, (255, 214, 120, 42), glow.get_rect(), border_radius=18)
        glow_rect = glow.get_rect(center=rect.center)
        self.screen.blit(glow, glow_rect)
        self.screen.blit(surf, rect)

    def _draw_footer(self, mode):
        issues = self.status.get('issues') or []
        audience = self.status.get('audience') or {}
        line = ''
        if mode == 'offline':
            line = 'Waiting for the local companion service.'
        elif mode == 'auth':
            line = self.auth.get('error') or 'Authorize the companion to unlock YouTube status.'
        elif issues:
            first = issues[0]
            line = first.get('description') or first.get('reason') or first.get('type') or ''
        elif audience.get('note'):
            line = audience['note']
        elif self.streamer.get('error'):
            line = self.streamer['error']
        elif self.streamer.get('msg_enabled') and self.streamer.get('msg_text'):
            line = self.streamer['msg_text']
        else:
            line = 'Animated standby view stays active while the Pi polls YouTube.'

        footer_rect = pygame.Rect(18, self.height - 46, self.width - 36, 28)
        panel = pygame.Surface((footer_rect.width, footer_rect.height), pygame.SRCALPHA)
        panel.fill((10, 14, 18, 205))
        pygame.draw.rect(panel, (*self._accent(mode), 210), panel.get_rect(), width=1, border_radius=12)
        self.screen.blit(panel, footer_rect.topleft)
        self._draw_text(_clip(line, 110), self.font_small, (232, 236, 240), footer_rect.x + 10, footer_rect.y + 6)

    def _draw_idle_screen(self, footer_text='READY  Waiting for next stream', border_color=(30, 100, 42), footer_color=(150, 220, 158)):
        now_text = datetime.now().strftime('%H:%M')
        sub_text = datetime.now().strftime('%a %b %d')
        self.screen.fill((0, 5, 0))
        self.matrix.update(1.0 / max(1, self.args.fps))
        for x, y, char, color in self.matrix.glyphs():
            glyph = self.font_matrix.render(char, True, color)
            self.screen.blit(glyph, (x, y))

        plate = pygame.Surface((190, 64), pygame.SRCALPHA)
        plate.fill((2, 10, 2, 210))
        pygame.draw.rect(plate, (36, 120, 52, 220), plate.get_rect(), width=1, border_radius=14)
        self.screen.blit(plate, (self.width - 208, 18))
        clock = self.font_title.render(now_text, True, (198, 248, 200))
        date = self.font_small.render(sub_text, True, (118, 180, 126))
        self.screen.blit(clock, (self.width - 190, 22))
        self.screen.blit(date, (self.width - 188, 52))

        bottom = pygame.Surface((320, 44), pygame.SRCALPHA)
        bottom.fill((2, 8, 2, 205))
        pygame.draw.rect(bottom, (*border_color, 220), bottom.get_rect(), width=1, border_radius=12)
        self.screen.blit(bottom, (18, self.height - 62))
        self._draw_text(footer_text, self.font_text, footer_color, 34, self.height - 51)

    def _draw_layout(self, mode):
        accent = self._accent(mode)
        top = 54 if not self.auth.get('pending') else 118
        card_gap = 12
        left_w = 376
        right_w = self.width - left_w - card_gap - 36
        left_x = 18
        right_x = left_x + left_w + card_gap

        summary_h = 92
        lower_h = 92
        sys_h = 64

        self._draw_card(
            (left_x, top, left_w, summary_h),
            'STREAMER',
            self._streamer_overview_lines(mode),
            accent,
        )
        self._draw_card(
            (right_x, top, right_w, summary_h),
            'YOUTUBE',
            self._summary_lines(mode),
            accent,
        )
        self._draw_card(
            (left_x, top + summary_h + card_gap, left_w, lower_h),
            'NETWORK',
            self._streamer_network_lines(mode),
            accent,
        )
        self._draw_card(
            (right_x, top + summary_h + card_gap, right_w, lower_h),
            'AUDIENCE',
            self._audience_lines(mode),
            accent,
        )
        self._draw_card(
            (left_x, top + summary_h + lower_h + card_gap * 2, left_w, sys_h),
            'SYSTEM',
            self._streamer_system_lines(mode),
            accent,
        )
        self._draw_card(
            (right_x, top + summary_h + lower_h + card_gap * 2, right_w, sys_h),
            'STATUS',
            self._system_lines(mode),
            accent,
        )

    def _next_step_lines(self, mode):
        if mode == 'offline':
            return [
                ('1', 'Start youtube-companion.service', (245, 245, 245)),
                ('2', 'Confirm /status responds locally', (214, 214, 214)),
                ('3', 'The screen will reconnect on its own', self._accent(mode)),
            ]
        if mode == 'auth':
            if self.auth.get('pending'):
                return [
                    ('1', 'Use the code shown above', (245, 245, 245)),
                    ('2', 'Approve the readonly YouTube scope', (214, 214, 214)),
                    ('3', 'This screen will switch to READY automatically', self._accent(mode)),
                ]
            return [
                ('1', 'Open the web UI from another device', (245, 245, 245)),
                ('2', 'Save the client ID and secret', (214, 214, 214)),
                ('3', 'Start device auth', self._accent(mode)),
            ]
        if mode == 'warning':
            return [
                ('1', 'Check the highlighted issue first', (245, 245, 245)),
                ('2', 'Refresh YouTube Studio if needed', (214, 214, 214)),
                ('3', 'The warning clears when YouTube reports healthy status', self._accent(mode)),
            ]
        if mode == 'live':
            return [
                ('1', 'Keep this screen visible on HDMI', (245, 245, 245)),
                ('2', 'Watch health and view changes from across the room', (214, 214, 214)),
                ('3', 'Warnings will pulse automatically if YouTube complains', self._accent(mode)),
            ]
        return [
            ('1', 'Companion is authorized and waiting', (245, 245, 245)),
            ('2', 'The screen will switch to LIVE as soon as YouTube does', (214, 214, 214)),
            ('3', 'Leave the HDMI display running full-time', self._accent(mode)),
        ]

    def run(self):
        while True:
            dt = self.clock.tick(self.args.fps) / 1000.0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                    return
                if event.type == pygame.VIDEORESIZE and self.args.windowed:
                    self.screen = pygame.display.set_mode(event.size, pygame.RESIZABLE)
                    self.width, self.height = self.screen.get_size()
                    self.dots.resize(self.width, self.height)
                    self.matrix.resize(self.width, self.height)
                    self._build_fonts()

            self._refresh_if_needed()
            base_mode = _derive_mode(self.status, self.auth, self.fetch_error)
            mode, self.idle_since = _resolve_display_mode(
                base_mode,
                self.idle_since,
                time.time(),
                self.args.idle_seconds,
                self.streamer['running'],
                self.streamer['online'],
            )

            if mode in {'idle', 'streamer_offline'}:
                if mode == 'streamer_offline':
                    self._draw_idle_screen(
                        footer_text='PI4 OFFLINE  Matrix standby active',
                        border_color=(42, 132, 68),
                        footer_color=(160, 236, 172),
                    )
                else:
                    self._draw_idle_screen()
            else:
                self.screen.fill((2, 4, 8))
                self.dots.update(dt, mode)
                self.dots.draw(self.screen, mode, time.time())
                overlay = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
                overlay.fill((0, 0, 0, 118 if mode in {'ready', 'auth'} else 134))
                self.screen.blit(overlay, (0, 0))

                self._draw_header(mode)
                self._draw_auth_code()
                self._draw_layout(mode)
                self._draw_footer(mode)
            pygame.display.flip()


class FramebufferScreen:
    def __init__(self, args):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise RuntimeError('Install python3-pil to use framebuffer mode.') from exc

        self.args = args
        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont

        self.width, self.height, self.stride, self.bpp = self._read_fb_info()
        self.fb = open('/dev/fb0', 'r+b', buffering=0)
        self.hostname = socket.gethostname()
        self.ip_address = _local_ip()
        self.status = {}
        self.auth = {}
        self.fetch_error = ''
        self.last_refresh = 0.0
        self.last_ok = 0.0
        self.idle_since = None
        self.streamer = _blank_streamer_state()
        self.streamer_status_url = _streamer_url(args)
        self._streamer_prev_sample = (0, 0, 0.0)
        self.dots = DotField(self.width, self.height, max(90, (self.width * self.height) // 22000))
        self.matrix = MatrixRain(self.width, self.height)
        self._build_fonts()

    def _read_fb_info(self):
        base = '/sys/class/graphics/fb0'
        with open(os.path.join(base, 'virtual_size')) as f:
            width_s, height_s = f.read().strip().split(',', 1)
        with open(os.path.join(base, 'stride')) as f:
            stride = int(f.read().strip())
        with open(os.path.join(base, 'bits_per_pixel')) as f:
            bpp = int(f.read().strip())
        return int(width_s), int(height_s), stride, bpp

    def _font(self, size):
        for path in (
            '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
        ):
            if os.path.exists(path):
                return self.ImageFont.truetype(path, size=size)
        return self.ImageFont.load_default()

    def _build_fonts(self):
        short_edge = min(self.width, self.height)
        self.font_title = self._font(max(18, short_edge // 26))
        self.font_mode = self._font(max(28, short_edge // 17))
        self.font_section = self._font(max(15, short_edge // 38))
        self.font_text = self._font(max(13, short_edge // 48))
        self.font_small = self._font(max(11, short_edge // 64))
        self.font_matrix = self._font(max(14, short_edge // 30))
        self.font_code = self._font(max(30, short_edge // 10))

    def _refresh_if_needed(self):
        now = time.time()
        if now - self.last_refresh < self.args.poll_seconds:
            return
        self.last_refresh = now
        self.streamer_status_url = _streamer_url(self.args)
        try:
            self.status = _fetch_json(self.args.status_url, self.args.http_timeout)
            self.auth = _fetch_json(self.args.auth_url, self.args.http_timeout)
            self.fetch_error = ''
            self.last_ok = now
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.fetch_error = str(exc)
        if self.streamer_status_url:
            try:
                raw = _fetch_json(self.streamer_status_url, self.args.http_timeout)
                self.streamer, self._streamer_prev_sample = _parse_streamer_state(raw, self._streamer_prev_sample, now)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
                self.streamer = _blank_streamer_state()
        else:
            self.streamer = _blank_streamer_state()

    def _accent(self, mode):
        pulse = 0.65 + 0.35 * math.sin(time.time() * 4.0)
        colors = {
            'offline': (100, 140, 190),
            'auth': (255, 196, 86),
            'ready': (78, 195, 255),
            'idle': (78, 185, 104),
            'live': (92, 220, 132),
            'warning': (255, int(120 + 70 * pulse), 90),
        }
        return colors.get(mode, colors['ready'])

    def _mode_label(self, mode):
        return {
            'offline': 'OFFLINE',
            'auth': 'AUTH',
            'ready': 'READY',
            'idle': 'IDLE',
            'live': 'LIVE',
            'warning': 'WARNING',
        }[mode]

    def _text_size(self, draw, text, font):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top

    def _wrap_lines(self, draw, text, width, font):
        words = str(text or '').split()
        if not words:
            return []
        lines = []
        current = words[0]
        for word in words[1:]:
            trial = current + ' ' + word
            if self._text_size(draw, trial, font)[0] <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _summary_lines(self, mode):
        broadcast = self.status.get('broadcast') or {}
        stream = self.status.get('stream') or {}
        issues = self.status.get('issues') or []
        accent = self._accent(mode)
        if mode == 'offline':
            return [
                ('COMPANION', 'The local YouTube companion service is not responding.', (245, 245, 245)),
                ('CHECK', 'Make sure youtube-companion.service is running.', (214, 214, 214)),
                ('TARGET', self.args.status_url, accent),
            ]
        if mode == 'auth':
            if self.auth.get('pending'):
                return [
                    ('ACTION', 'Open google.com/device and enter the code below.', (245, 245, 245)),
                    ('URL', self.auth.get('verification_url') or 'https://www.google.com/device', accent),
                    ('STATE', 'Waiting for Google approval...', (214, 214, 214)),
                ]
            return [
                ('ACTION', 'Open the companion web UI and start device auth.', (245, 245, 245)),
                ('WEB', self.args.web_url, accent),
                ('STATE', 'No saved YouTube token yet.', (214, 214, 214)),
            ]
        title = _clip(broadcast.get('title') or 'No active broadcast yet', 72)
        lines = [
            ('BROADCAST', title, (245, 245, 245)),
            ('LIFECYCLE', broadcast.get('life_cycle_status') or 'none', accent),
            ('STREAM', stream.get('stream_status') or 'unknown', (214, 214, 214)),
            ('HEALTH', stream.get('health_status') or 'unknown', (214, 214, 214)),
        ]
        if mode == 'warning' and issues:
            first = issues[0]
            lines.append(('ISSUE', _clip(first.get('description') or first.get('reason') or first.get('type') or 'Warning reported', 74), (255, 196, 160)))
        return lines

    def _streamer_overview_lines(self, mode):
        accent = self._accent(mode)
        if not self.streamer['online']:
            return [
                ('STREAMER', 'Pi 4 status offline', (245, 245, 245)),
                ('TARGET', self.streamer_status_url or 'Not configured', accent),
            ]
        state = 'LIVE' if self.streamer['running'] else 'IDLE'
        if self.streamer['running'] and self.streamer['retries'] > 0:
            state = 'RECOVER'
        audio = self.streamer['audio_name'] or 'No audio'
        if self.streamer['audio_silent']:
            audio = 'No Audio'
        return [
            ('STATE', state, accent),
            ('CAM', _active_cam_label(self.streamer), (245, 245, 245)),
            ('AUDIO', audio, (214, 214, 214)),
        ]

    def _streamer_network_lines(self, mode):
        accent = self._accent(mode)
        if not self.streamer['online']:
            return [('INFO', 'Waiting for streamer status', accent)]
        eth = 'up ' + (self.streamer['eth_oper'] or '-') if self.streamer['eth_carrier'] else 'down ' + (self.streamer['eth_oper'] or '-')
        return [
            ('UP / RET', f"{_fmt_uptime(self.streamer['uptime_s'])}   r:{self.streamer['retries']}", (245, 245, 245)),
            ('ETH / RTMP', f"{eth.strip()}   {self.streamer['rtmp_state'] or '-'}", (214, 214, 214)),
            ('LAN', f"tx:{self.streamer['tx_kbps']}   rx:{self.streamer['rx_kbps']} kbps", accent),
        ]

    def _streamer_system_lines(self, mode):
        accent = self._accent(mode)
        if not self.streamer['online']:
            return [('INFO', 'No local Pi 4 system data yet', accent)]
        temp = f"{self.streamer['temp_c']:.1f} C" if self.streamer['temp_c'] is not None else '-'
        message = self.streamer['msg_text'] if self.streamer['msg_enabled'] and self.streamer['msg_text'] else 'off'
        return [
            ('TEMP / THR', f"{temp}   {self.streamer['throttled'] or '-'}", (245, 245, 245)),
            ('MSG', _clip(message, 52), accent if self.streamer['msg_enabled'] else (214, 214, 214)),
        ]

    def _audience_lines(self, mode):
        audience = self.status.get('audience') or {}
        lines = [
            ('VIEWS', str(audience.get('views')) if audience.get('views') is not None else 'n/a', (245, 245, 245)),
            ('AVG VIEW', audience.get('average_view_duration_label') or 'n/a', (214, 214, 214)),
            ('CONCURRENT', str(audience.get('concurrent_viewers')) if audience.get('concurrent_viewers') is not None else 'n/a', (214, 214, 214)),
        ]
        if audience.get('note'):
            lines.append(('NOTE', _clip(audience['note'], 70), self._accent(mode)))
        return lines

    def _system_lines(self, mode):
        updated_at = _safe_int(self.status.get('updated_at'))
        updated_label = datetime.fromtimestamp(updated_at).strftime('%H:%M:%S') if updated_at else 'n/a'
        stale_seconds = int(max(0, time.time() - self.last_ok)) if self.last_ok else None
        freshness = 'Waiting for first refresh'
        if stale_seconds is not None:
            freshness = 'Fresh' if stale_seconds < max(10, self.args.poll_seconds * 2) else f'{stale_seconds}s old'
        lines = [
            ('HOST', self.hostname, (245, 245, 245)),
            ('IP', self.ip_address, (214, 214, 214)),
            ('UPDATED', updated_label, (214, 214, 214)),
            ('LINK', freshness, self._accent(mode)),
        ]
        if self.status.get('error') and mode != 'offline':
            lines.append(('ERROR', _clip(self.status['error'], 70), (255, 180, 180)))
        return lines

    def _footer_text(self, mode):
        issues = self.status.get('issues') or []
        audience = self.status.get('audience') or {}
        if mode == 'offline':
            return 'Waiting for the local companion service.'
        if mode == 'auth':
            return self.auth.get('error') or 'Authorize the companion to unlock YouTube status.'
        if issues:
            first = issues[0]
            return first.get('description') or first.get('reason') or first.get('type') or ''
        if audience.get('note'):
            return audience['note']
        if self.streamer.get('error'):
            return self.streamer['error']
        if self.streamer.get('msg_enabled') and self.streamer.get('msg_text'):
            return self.streamer['msg_text']
        return 'Animated standby view stays active while the Pi polls YouTube.'

    def _draw_card(self, draw, rect, title, lines, accent):
        x, y, w, h = rect
        draw.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=(12, 18, 24), outline=accent, width=2)
        draw.text((x + 10, y + 8), title, font=self.font_section, fill=(245, 245, 245))
        line_y = y + 28
        for label, value, color in lines:
            line = f'{label}: {value}' if label else value
            for item in self._wrap_lines(draw, line, w - 18, self.font_text)[:3]:
                draw.text((x + 10, line_y), item, font=self.font_text, fill=color)
                line_y += 18
                if line_y >= y + h - 10:
                    return

    def _write_frame(self, image):
        if self.bpp != 16:
            raise RuntimeError(f'Unsupported framebuffer depth: {self.bpp}')
        rgb = image.convert('RGB').tobytes()
        raw = bytearray((len(rgb) // 3) * 2)
        out = memoryview(raw)
        src = memoryview(rgb)
        j = 0
        for i in range(0, len(rgb), 3):
            r = src[i]
            g = src[i + 1]
            b = src[i + 2]
            pixel = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[j] = pixel & 0xFF
            out[j + 1] = pixel >> 8
            j += 2
        self.fb.seek(0)
        self.fb.write(raw)

    def _draw_idle_frame(self, image, draw, footer_text='READY  Waiting for next stream', border_color=(30, 100, 42), footer_color=(150, 220, 158)):
        now_text = datetime.now().strftime('%H:%M')
        sub_text = datetime.now().strftime('%a %b %d')
        self.matrix.update(1.0 / max(1, min(self.args.fps, 12)))
        for x, y, char, color in self.matrix.glyphs():
            draw.text((x, y), char, font=self.font_matrix, fill=color)

        draw.rounded_rectangle((self.width - 206, 18, self.width - 18, 80), radius=14, fill=(2, 10, 2), outline=(36, 120, 52), width=1)
        draw.text((self.width - 186, 20), now_text, font=self.font_title, fill=(198, 248, 200))
        draw.text((self.width - 184, 50), sub_text, font=self.font_small, fill=(118, 180, 126))
        draw.rounded_rectangle((18, self.height - 62, 338, self.height - 18), radius=12, fill=(2, 8, 2), outline=border_color, width=1)
        draw.text((34, self.height - 50), footer_text, font=self.font_text, fill=footer_color)

    def run(self):
        frame_delay = 1.0 / max(1, min(self.args.fps, 12))
        while True:
            started = time.time()
            self._refresh_if_needed()
            base_mode = _derive_mode(self.status, self.auth, self.fetch_error)
            mode, self.idle_since = _resolve_display_mode(
                base_mode,
                self.idle_since,
                time.time(),
                self.args.idle_seconds,
                self.streamer['running'],
                self.streamer['online'],
            )

            image = self.Image.new('RGB', (self.width, self.height), (2, 4, 8))
            draw = self.ImageDraw.Draw(image)
            if mode in {'idle', 'streamer_offline'}:
                image.paste((0, 5, 0), (0, 0, self.width, self.height))
                if mode == 'streamer_offline':
                    self._draw_idle_frame(
                        image,
                        draw,
                        footer_text='PI4 OFFLINE  Matrix standby active',
                        border_color=(42, 132, 68),
                        footer_color=(160, 236, 172),
                    )
                else:
                    self._draw_idle_frame(image, draw)
            else:
                self.dots.update(frame_delay, mode)
                for dot in self.dots.dots:
                    blend = 0.5 + 0.5 * math.sin(time.time() * 0.7 + dot['phase'] + dot['color_shift'] * math.pi)
                    color_a, color_b = {
                        'offline': ((50, 90, 140), (110, 140, 180)),
                        'auth': ((120, 170, 240), (255, 210, 110)),
                        'ready': ((0, 220, 160), (70, 190, 255)),
                        'streamer_offline': ((28, 140, 74), (76, 210, 108)),
                        'live': ((0, 190, 120), (60, 150, 235)),
                        'warning': ((255, 120, 80), (255, 210, 90)),
                    }.get(mode, ((0, 220, 160), (70, 190, 255)))
                    color = (
                        int(color_a[0] * (1 - blend) + color_b[0] * blend),
                        int(color_a[1] * (1 - blend) + color_b[1] * blend),
                        int(color_a[2] * (1 - blend) + color_b[2] * blend),
                    )
                    r = dot['radius']
                    draw.ellipse((dot['x'] - r, dot['y'] - r, dot['x'] + r, dot['y'] + r), fill=color)

                accent = self._accent(mode)
                now_text = datetime.now().strftime('%H:%M:%S')
                draw.text((18, 10), self._mode_label(mode), font=self.font_mode, fill=accent)
                time_w, _ = self._text_size(draw, now_text, self.font_title)
                draw.text((self.width - time_w - 18, 12), now_text, font=self.font_title, fill=(232, 244, 255))
                if self.streamer['online']:
                    state = 'LIVE' if self.streamer['running'] else 'IDLE'
                    if self.streamer['running'] and self.streamer['retries'] > 0:
                        state = 'RECOVER'
                    draw.text((self.width - 94, 34), f'PI4 {state}', font=self.font_small, fill=(148, 165, 180))

                if self.auth.get('pending'):
                    code = self.auth.get('user_code') or '---- ----'
                    code_w, code_h = self._text_size(draw, code, self.font_code)
                    code_x = (self.width - code_w) // 2
                    code_y = int(self.height * 0.16)
                    draw.rounded_rectangle((code_x - 20, code_y - 8, code_x + code_w + 20, code_y + code_h + 12), radius=14, fill=(40, 32, 8), outline=(255, 214, 120), width=2)
                    draw.text((code_x, code_y), code, font=self.font_code, fill=(255, 232, 176))

                top = 54 if not self.auth.get('pending') else 118
                gap = 12
                left_w = 376
                right_w = self.width - left_w - gap - 36
                summary_h = 92
                lower_h = 92
                sys_h = 64
                self._draw_card(draw, (18, top, left_w, summary_h), 'STREAMER', self._streamer_overview_lines(mode), accent)
                self._draw_card(draw, (18 + left_w + gap, top, right_w, summary_h), 'YOUTUBE', self._summary_lines(mode), accent)
                self._draw_card(draw, (18, top + summary_h + gap, left_w, lower_h), 'NETWORK', self._streamer_network_lines(mode), accent)
                self._draw_card(draw, (18 + left_w + gap, top + summary_h + gap, right_w, lower_h), 'AUDIENCE', self._audience_lines(mode), accent)
                self._draw_card(draw, (18, top + summary_h + lower_h + gap * 2, left_w, sys_h), 'SYSTEM', self._streamer_system_lines(mode), accent)
                self._draw_card(draw, (18 + left_w + gap, top + summary_h + lower_h + gap * 2, right_w, sys_h), 'STATUS', self._system_lines(mode), accent)

                footer = _clip(self._footer_text(mode), 110)
                draw.rounded_rectangle((18, self.height - 46, self.width - 18, self.height - 18), radius=10, fill=(10, 14, 18), outline=accent, width=1)
                draw.text((28, self.height - 40), footer, font=self.font_small, fill=(232, 236, 240))

            self._write_frame(image)
            elapsed = time.time() - started
            if elapsed < frame_delay:
                time.sleep(frame_delay - elapsed)


def _parse_resolution(value):
    if not value:
        return None, None
    try:
        width, height = value.lower().split('x', 1)
        return max(320, int(width)), max(240, int(height))
    except ValueError as exc:
        raise argparse.ArgumentTypeError('resolution must look like 1280x720') from exc


def _install_signal_handlers():
    def _handle_stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)


def main():
    parser = argparse.ArgumentParser(description='YouTube Companion HDMI screen')
    parser.add_argument('--status-url', default='http://127.0.0.1:8091/status')
    parser.add_argument('--auth-url', default='http://127.0.0.1:8091/auth_status')
    parser.add_argument('--web-url', default='http://127.0.0.1:8091')
    parser.add_argument('--streamer-status-url', default='')
    parser.add_argument('--poll-seconds', type=float, default=5.0)
    parser.add_argument('--http-timeout', type=float, default=3.0)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--idle-seconds', type=float, default=300.0, help='seconds in READY before switching to idle matrix mode')
    parser.add_argument('--windowed', action='store_true', help='use a resizable window instead of fullscreen')
    parser.add_argument('--resolution', default='', help='window size for --windowed, for example 1280x720')
    args = parser.parse_args()

    _install_signal_handlers()
    args.width, args.height = _parse_resolution(args.resolution)
    try:
        app = CompanionScreen(args)
    except RuntimeError as exc:
        if args.windowed:
            raise
        app = FramebufferScreen(args)
    app.run()
    pygame.quit()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
