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


def _resolve_display_mode(base_mode, idle_since, now, idle_seconds):
    if base_mode == 'ready':
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
        self.font_title = pygame.font.SysFont('dejavusansmono', max(24, short_edge // 22), bold=True)
        self.font_mode = pygame.font.SysFont('dejavusansmono', max(38, short_edge // 13), bold=True)
        self.font_section = pygame.font.SysFont('dejavusansmono', max(18, short_edge // 34), bold=True)
        self.font_text = pygame.font.SysFont('dejavusansmono', max(16, short_edge // 42))
        self.font_small = pygame.font.SysFont('dejavusansmono', max(14, short_edge // 54))
        self.font_matrix = pygame.font.SysFont('dejavusansmono', max(14, short_edge // 30), bold=True)
        self.font_code = pygame.font.SysFont('dejavusansmono', max(34, short_edge // 11), bold=True)

    def _refresh_if_needed(self):
        now = time.time()
        if now - self.last_refresh < self.args.poll_seconds:
            return
        self.last_refresh = now
        try:
            self.status = _fetch_json(self.args.status_url, self.args.http_timeout)
            self.auth = _fetch_json(self.args.auth_url, self.args.http_timeout)
            self.fetch_error = ''
            self.last_ok = now
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.fetch_error = str(exc)

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
        self.screen.blit(title_h, (x + 18, y + 14))
        line_y = y + 18 + title_h.get_height() + 10
        for label, value, color in lines:
            label_text = f'{label}: ' if label else ''
            line = label_text + value
            wrapped = self._wrap_lines(line, w - 36, self.font_text) or ['']
            for item in wrapped[:4]:
                line_y += self._draw_text(item, self.font_text, color, x + 18, line_y)
                line_y += 6
                if line_y >= y + h - 24:
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
        self._draw_text('YouTube Companion HDMI', self.font_title, (232, 244, 255), 28, 22)
        badge = self.font_mode.render(self._mode_label(mode), True, accent)
        badge_rect = badge.get_rect()
        badge_rect.topright = (self.width - 26, 18)
        self.screen.blit(badge, badge_rect)
        self._draw_text(now_text, self.font_small, (180, 198, 214), self.width - 26 - self.font_small.size(now_text)[0], badge_rect.bottom + 2)
        self._draw_text(self.args.web_url, self.font_small, (148, 165, 180), 30, 64)

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
        else:
            line = 'Animated standby view stays active while the Pi polls YouTube.'

        footer_rect = pygame.Rect(24, self.height - 72, self.width - 48, 44)
        panel = pygame.Surface((footer_rect.width, footer_rect.height), pygame.SRCALPHA)
        panel.fill((10, 14, 18, 205))
        pygame.draw.rect(panel, (*self._accent(mode), 210), panel.get_rect(), width=2, border_radius=16)
        self.screen.blit(panel, footer_rect.topleft)
        self._draw_text(_clip(line, 120), self.font_text, (232, 236, 240), footer_rect.x + 16, footer_rect.y + 10)

    def _draw_idle_screen(self):
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
        pygame.draw.rect(bottom, (30, 100, 42, 220), bottom.get_rect(), width=1, border_radius=12)
        self.screen.blit(bottom, (18, self.height - 62))
        self._draw_text('READY  Waiting for next stream', self.font_text, (150, 220, 158), 34, self.height - 51)

    def _draw_layout(self, mode):
        accent = self._accent(mode)
        top = int(self.height * 0.18)
        card_gap = 18
        left_w = int(self.width * 0.5) - 34
        right_w = self.width - left_w - card_gap - 52
        left_x = 24
        right_x = left_x + left_w + card_gap

        summary_h = int(self.height * 0.25)
        lower_h = int(self.height * 0.22)
        sys_h = int(self.height * 0.18)

        self._draw_card(
            (left_x, top, left_w, summary_h),
            'SUMMARY',
            self._summary_lines(mode),
            accent,
        )
        self._draw_card(
            (right_x, top, right_w, summary_h),
            'AUDIENCE',
            self._audience_lines(mode),
            accent,
        )
        self._draw_card(
            (left_x, top + summary_h + card_gap, left_w, lower_h),
            'SYSTEM',
            self._system_lines(mode),
            accent,
        )
        self._draw_card(
            (right_x, top + summary_h + card_gap, right_w, lower_h),
            'NEXT STEP',
            self._next_step_lines(mode),
            accent,
        )
        self._draw_card(
            (left_x, top + summary_h + lower_h + card_gap * 2, self.width - 48, sys_h),
            'STATUS URLS',
            [
                ('STATUS', self.args.status_url, (245, 245, 245)),
                ('AUTH', self.args.auth_url, (214, 214, 214)),
            ],
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
            )

            if mode == 'idle':
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
        self.font_title = self._font(max(20, short_edge // 22))
        self.font_mode = self._font(max(34, short_edge // 12))
        self.font_section = self._font(max(18, short_edge // 32))
        self.font_text = self._font(max(15, short_edge // 40))
        self.font_small = self._font(max(13, short_edge // 52))
        self.font_matrix = self._font(max(14, short_edge // 30))
        self.font_code = self._font(max(30, short_edge // 10))

    def _refresh_if_needed(self):
        now = time.time()
        if now - self.last_refresh < self.args.poll_seconds:
            return
        self.last_refresh = now
        try:
            self.status = _fetch_json(self.args.status_url, self.args.http_timeout)
            self.auth = _fetch_json(self.args.auth_url, self.args.http_timeout)
            self.fetch_error = ''
            self.last_ok = now
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.fetch_error = str(exc)

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
        return 'Animated standby view stays active while the Pi polls YouTube.'

    def _draw_card(self, draw, rect, title, lines, accent):
        x, y, w, h = rect
        draw.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=(12, 18, 24), outline=accent, width=2)
        draw.text((x + 14, y + 10), title, font=self.font_section, fill=(245, 245, 245))
        line_y = y + 40
        for label, value, color in lines:
            line = f'{label}: {value}' if label else value
            for item in self._wrap_lines(draw, line, w - 24, self.font_text)[:4]:
                draw.text((x + 14, line_y), item, font=self.font_text, fill=color)
                line_y += 24
                if line_y >= y + h - 18:
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

    def _draw_idle_frame(self, image, draw):
        now_text = datetime.now().strftime('%H:%M')
        sub_text = datetime.now().strftime('%a %b %d')
        self.matrix.update(1.0 / max(1, min(self.args.fps, 12)))
        for x, y, char, color in self.matrix.glyphs():
            draw.text((x, y), char, font=self.font_matrix, fill=color)

        draw.rounded_rectangle((self.width - 206, 18, self.width - 18, 80), radius=14, fill=(2, 10, 2), outline=(36, 120, 52), width=1)
        draw.text((self.width - 186, 20), now_text, font=self.font_title, fill=(198, 248, 200))
        draw.text((self.width - 184, 50), sub_text, font=self.font_small, fill=(118, 180, 126))
        draw.rounded_rectangle((18, self.height - 62, 338, self.height - 18), radius=12, fill=(2, 8, 2), outline=(30, 100, 42), width=1)
        draw.text((34, self.height - 50), 'READY  Waiting for next stream', font=self.font_text, fill=(150, 220, 158))

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
            )

            image = self.Image.new('RGB', (self.width, self.height), (2, 4, 8))
            draw = self.ImageDraw.Draw(image)
            if mode == 'idle':
                image.paste((0, 5, 0), (0, 0, self.width, self.height))
                self._draw_idle_frame(image, draw)
            else:
                self.dots.update(frame_delay, mode)
                for dot in self.dots.dots:
                    blend = 0.5 + 0.5 * math.sin(time.time() * 0.7 + dot['phase'] + dot['color_shift'] * math.pi)
                    color_a, color_b = {
                        'offline': ((50, 90, 140), (110, 140, 180)),
                        'auth': ((120, 170, 240), (255, 210, 110)),
                        'ready': ((0, 220, 160), (70, 190, 255)),
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
                draw.text((18, 14), 'YouTube Companion HDMI', font=self.font_title, fill=(232, 244, 255))
                mode_w, _ = self._text_size(draw, self._mode_label(mode), self.font_mode)
                draw.text((self.width - mode_w - 18, 10), self._mode_label(mode), font=self.font_mode, fill=accent)
                time_w, _ = self._text_size(draw, now_text, self.font_small)
                draw.text((self.width - time_w - 18, 56), now_text, font=self.font_small, fill=(180, 198, 214))
                draw.text((18, 50), self.args.web_url, font=self.font_small, fill=(148, 165, 180))

                if self.auth.get('pending'):
                    code = self.auth.get('user_code') or '---- ----'
                    code_w, code_h = self._text_size(draw, code, self.font_code)
                    code_x = (self.width - code_w) // 2
                    code_y = int(self.height * 0.16)
                    draw.rounded_rectangle((code_x - 20, code_y - 8, code_x + code_w + 20, code_y + code_h + 12), radius=14, fill=(40, 32, 8), outline=(255, 214, 120), width=2)
                    draw.text((code_x, code_y), code, font=self.font_code, fill=(255, 232, 176))

                top = 110 if not self.auth.get('pending') else 170
                gap = 14
                left_w = 386
                right_w = self.width - left_w - gap - 36
                self._draw_card(draw, (18, top, left_w, 142), 'SUMMARY', self._summary_lines(mode), accent)
                self._draw_card(draw, (18 + left_w + gap, top, right_w, 142), 'AUDIENCE', self._audience_lines(mode), accent)
                self._draw_card(draw, (18, top + 142 + gap, left_w, 124), 'SYSTEM', self._system_lines(mode), accent)
                self._draw_card(draw, (18 + left_w + gap, top + 142 + gap, right_w, 124), 'NEXT STEP', [('1', 'Leave this screen on HDMI' if mode != 'offline' else 'Start youtube-companion.service', (245, 245, 245)), ('2', 'It will switch modes automatically', (214, 214, 214)), ('3', 'Warnings and auth prompts show here', accent)], accent)

                footer = _clip(self._footer_text(mode), 96)
                draw.rounded_rectangle((18, self.height - 58, self.width - 18, self.height - 16), radius=14, fill=(10, 14, 18), outline=accent, width=2)
                draw.text((30, self.height - 48), footer, font=self.font_text, fill=(232, 236, 240))

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
