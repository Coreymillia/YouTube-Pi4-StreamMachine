#!/usr/bin/env python3
"""Waveshare 1.44" LCD HAT controller for YouTube Studio."""

import argparse
import io
import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import RPi.GPIO as GPIO
    import LCD_1in44
    import LCD_Config
except ImportError as exc:
    raise RuntimeError(
        'Install the Waveshare 1.44" LCD HAT Python libraries and RPi.GPIO on the Pi 4.'
    ) from exc


KEY1_PIN = 21
KEY2_PIN = 20
KEY3_PIN = 16
JOYSTICK_UP = 6
JOYSTICK_DOWN = 19
JOYSTICK_PRESS = 13
JOYSTICK_LEFT = 5
JOYSTICK_RIGHT = 26

BUTTON_PINS = {
    'key1': KEY1_PIN,
    'key2': KEY2_PIN,
    'key3': KEY3_PIN,
    'up': JOYSTICK_UP,
    'down': JOYSTICK_DOWN,
    'press': JOYSTICK_PRESS,
    'left': JOYSTICK_LEFT,
    'right': JOYSTICK_RIGHT,
}

MODE_STATUS = 0
MODE_SNAPSHOT = 1
MODE_CONTROL = 2
MODE_LABELS = {
    MODE_STATUS: 'STATUS',
    MODE_SNAPSHOT: 'SNAPSHOT',
    MODE_CONTROL: 'CONTROL',
}

CAMERA_LABELS = {
    0: 'USB CAM',
    1: 'HQ CAM',
}

_FONT_PATH_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
_RESAMPLE = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.BICUBIC)
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PROJECT_DIR, 'config.json')
_HAT_STATS_PATH = os.path.join(_PROJECT_DIR, 'hat_stats.json')

OFFLINE_MODE_MATRIX = 0
OFFLINE_MODE_CLOCK = 1
OFFLINE_MODE_STATS = 2
OFFLINE_MODE_RETRO = 3
OFFLINE_MODE_PLASMA = 4
OFFLINE_MODE_LABELS = {
    OFFLINE_MODE_MATRIX: 'MATRIX',
    OFFLINE_MODE_CLOCK: 'CLOCK',
    OFFLINE_MODE_STATS: 'STATS',
    OFFLINE_MODE_RETRO: 'RETRO',
    OFFLINE_MODE_PLASMA: 'PLASMA',
}


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _clip(text, limit):
    text = str(text or '').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + '…'


def _fmt_uptime(seconds):
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


def _fmt_compact_duration(seconds):
    total = max(0, int(seconds or 0))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f'{days}d {hours:02d}h'
    if hours:
        return f'{hours}h {minutes:02d}m'
    return f'{minutes}m'


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_coord(value, minimum, maximum):
    try:
        coord = float(value)
    except (TypeError, ValueError):
        return None
    if coord < minimum or coord > maximum:
        return None
    return coord


def _read_system_uptime():
    try:
        with open('/proc/uptime') as f:
            return _safe_float(f.read().split()[0], 0.0)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return 0.0


def _default_hat_stats():
    return {
        'total_seconds': 0.0,
        'daily_seconds': {},
        'updated_at': 0,
    }


def _load_hat_stats():
    stats = _default_hat_stats()
    try:
        with open(_HAT_STATS_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return stats
    if not isinstance(raw, dict):
        return stats

    stats['total_seconds'] = max(0.0, _safe_float(raw.get('total_seconds')))
    stats['updated_at'] = max(0, int(_safe_float(raw.get('updated_at'))))
    daily = raw.get('daily_seconds') or {}
    if isinstance(daily, dict):
        for day, value in daily.items():
            day_key = str(day).strip()[:10]
            if len(day_key) == 10:
                stats['daily_seconds'][day_key] = max(0.0, _safe_float(value))
    return stats


def _save_hat_stats(stats):
    daily = stats.get('daily_seconds') or {}
    if len(daily) > 400:
        keep = set(sorted(daily)[-400:])
        daily = {key: daily[key] for key in sorted(daily) if key in keep}
        stats['daily_seconds'] = daily
    payload = {
        'total_seconds': round(_safe_float(stats.get('total_seconds')), 2),
        'daily_seconds': {key: round(_safe_float(value), 2) for key, value in sorted(daily.items())},
        'updated_at': int(_safe_float(stats.get('updated_at'))),
    }
    with open(_HAT_STATS_PATH, 'w') as f:
        json.dump(payload, f, indent=2, sort_keys=True)


class RetroGeometryLite:
    _PALETTE = [
        (255, 0, 255),
        (0, 255, 255),
        (255, 255, 0),
        (0, 255, 120),
        (255, 140, 0),
        (140, 80, 255),
        (255, 0, 120),
        (0, 140, 255),
    ]

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.time = 0.0
        self.shapes = [self._spawn(initial=True) for _ in range(8)]

    def _spawn(self, initial=False):
        size = random.randint(8, 24)
        return {
            'type': random.choice(('circle', 'triangle', 'line', 'box')),
            'x': random.uniform(0, self.width),
            'y': random.uniform(0, self.height) if initial else random.uniform(12, self.height - 12),
            'size': size,
            'color': random.choice(self._PALETTE),
            'angle': random.uniform(0.0, math.tau),
            'spin': random.uniform(-1.1, 1.1),
            'speed': random.uniform(8.0, 26.0),
            'direction': random.uniform(0.0, math.tau),
            'life': random.uniform(8.0, 18.0),
            'pulse': random.uniform(1.2, 3.5),
        }

    def update(self, dt):
        self.time += dt
        for idx, shape in enumerate(self.shapes):
            shape['x'] += math.cos(shape['direction']) * shape['speed'] * dt
            shape['y'] += math.sin(shape['direction']) * shape['speed'] * dt
            shape['angle'] += shape['spin'] * dt
            shape['life'] -= dt
            if shape['x'] < -20 or shape['x'] > self.width + 20:
                shape['direction'] = math.pi - shape['direction']
            if shape['y'] < -20 or shape['y'] > self.height + 20:
                shape['direction'] = -shape['direction']
            if shape['life'] <= 0:
                self.shapes[idx] = self._spawn()

    def draw(self, image, draw):
        image.paste((3, 3, 8), (0, 0, self.width, self.height))
        grid = (18, 18, 30)
        for x in range(0, self.width, 16):
            draw.line((x, 0, x, self.height), fill=grid)
        for y in range(0, self.height, 16):
            draw.line((0, y, self.width, y), fill=grid)

        for y in range(6, self.height, 5):
            if (y // 5) % 2 == 0:
                draw.line((0, y, self.width, y), fill=(10, 10, 12))

        for shape in self.shapes:
            size = int(shape['size'] * (1.0 + 0.18 * math.sin(self.time * shape['pulse'])))
            x = int(shape['x'])
            y = int(shape['y'])
            color = shape['color']
            if shape['type'] == 'circle':
                draw.ellipse((x - size, y - size, x + size, y + size), outline=color, width=2)
            elif shape['type'] == 'line':
                dx = int(math.cos(shape['angle']) * size)
                dy = int(math.sin(shape['angle']) * size)
                draw.line((x - dx, y - dy, x + dx, y + dy), fill=color, width=2)
            elif shape['type'] == 'box':
                draw.rectangle((x - size, y - size, x + size, y + size), outline=color, width=2)
            else:
                points = []
                for idx in range(3):
                    angle = shape['angle'] + idx * (math.tau / 3.0)
                    points.append((int(x + math.cos(angle) * size), int(y + math.sin(angle) * size)))
                draw.polygon(points, outline=color, width=2)


class PlasmaFieldLite:
    def __init__(self, width, height, scale=4):
        self.width = width
        self.height = height
        self.scale = max(2, scale)
        self.grid_w = max(8, width // self.scale)
        self.grid_h = max(8, height // self.scale)
        self.time = 0.0
        self.palette = self._build_palette()

    def _build_palette(self):
        palette = []
        for idx in range(256):
            t = idx / 255.0
            palette.append(
                (
                    int(128 + 127 * math.sin(t * math.tau)),
                    int(128 + 127 * math.sin(t * math.tau + math.pi / 3.0)),
                    int(128 + 127 * math.sin(t * math.tau + 2.0 * math.pi / 3.0)),
                )
            )
        return palette

    def update(self, dt):
        self.time += dt * 1.8

    def draw(self):
        image = Image.new('RGB', (self.grid_w, self.grid_h), (0, 0, 0))
        pix = image.load()
        cx = self.grid_w / 2.0
        cy = self.grid_h / 2.0
        for y in range(self.grid_h):
            for x in range(self.grid_w):
                dx = x - cx
                dy = y - cy
                dist = math.sqrt(dx * dx + dy * dy)
                value = (
                    math.sin((x + self.time * 3.0) / 3.1)
                    + math.sin((y + self.time * 2.0) / 2.2)
                    + math.sin((x + y + self.time * 2.6) / 4.3)
                    + math.sin((dist + self.time * 4.2) / 2.4)
                )
                pix[x, y] = self.palette[int(((value + 4.0) / 8.0) * 255) % 256]
        return image.resize((self.width, self.height), _RESAMPLE)


def _load_cfg():
    cfg = {
        'streamer_status_host': '',
        'streamer_status_port': 8090,
        'streamer_control_token': '',
        'forecast_latitude': None,
        'forecast_longitude': None,
    }
    try:
        with open(_CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    cfg['streamer_status_host'] = str(cfg.get('streamer_status_host', '')).strip()
    try:
        cfg['streamer_status_port'] = max(1, int(cfg.get('streamer_status_port', 8090)))
    except (TypeError, ValueError):
        cfg['streamer_status_port'] = 8090
    cfg['streamer_control_token'] = str(cfg.get('streamer_control_token', '')).strip()[:120]
    cfg['forecast_latitude'] = _safe_coord(cfg.get('forecast_latitude'), -90.0, 90.0)
    cfg['forecast_longitude'] = _safe_coord(cfg.get('forecast_longitude'), -180.0, 180.0)
    return cfg


def _split_hosts(*values):
    hosts = []
    seen = set()
    for value in values:
        if isinstance(value, (list, tuple)):
            items = value
        else:
            items = str(value or '').replace(';', ',').split(',')
        for item in items:
            host = str(item).strip()
            if not host or host in seen:
                continue
            seen.add(host)
            hosts.append(host)
    return hosts


def _streamer_base_urls(base_url=''):
    if base_url:
        return [base_url.rstrip('/')]
    cfg = _load_cfg()
    hosts = _split_hosts(cfg.get('streamer_status_hosts'), cfg.get('streamer_status_host'))
    port = cfg.get('streamer_status_port', 8090)
    if hosts:
        return [f'http://{host}:{port}' for host in hosts]
    return ['http://127.0.0.1:8090']


class MatrixRainLite:
    _CHARS = '01[]{}<>/\\+-=*#@!?$%&'

    def __init__(self, width, height, column_width=10, row_height=10):
        self.width = width
        self.height = height
        self.column_width = max(8, column_width)
        self.row_height = max(8, row_height)
        self._build_streams()

    def _build_streams(self):
        self.columns = max(8, self.width // self.column_width)
        self.streams = [self._spawn(col, initial=True) for col in range(self.columns)]

    def _spawn(self, col, initial=False):
        length = random.randint(5, 11)
        head = random.uniform(-self.height * 0.8, self.height if initial else 0)
        return {
            'col': col,
            'head': head,
            'speed': random.uniform(24, 58),
            'length': length,
            'chars': [random.choice(self._CHARS) for _ in range(length + 4)],
            'mutate_at': time.time() + random.uniform(0.04, 0.18),
        }

    def update(self, dt):
        now = time.time()
        for idx, stream in enumerate(self.streams):
            stream['head'] += stream['speed'] * dt
            if now >= stream['mutate_at']:
                stream['chars'][random.randrange(len(stream['chars']))] = random.choice(self._CHARS)
                stream['mutate_at'] = now + random.uniform(0.05, 0.16)
            if stream['head'] - (stream['length'] * self.row_height) > self.height + self.row_height:
                self.streams[idx] = self._spawn(stream['col'])

    def glyphs(self):
        for stream in self.streams:
            col_x = stream['col'] * self.column_width + 3
            head_row = int(stream['head'] // self.row_height)
            for offset in range(stream['length']):
                row = head_row - offset
                if row < 0:
                    continue
                y = row * self.row_height
                if y >= self.height:
                    continue
                if offset == 0:
                    color = (190, 255, 194)
                elif offset == 1:
                    color = (110, 236, 122)
                else:
                    fade = max(0.18, 1.0 - (offset / max(1, stream['length'])))
                    color = (0, int(150 * fade) + 10, 0)
                yield col_x, y, stream['chars'][offset], color


class StudioHatUI:
    def __init__(self, args):
        self.args = args
        cfg = _load_cfg()
        self.base_urls = _streamer_base_urls(args.base_url)
        self.base_url = self.base_urls[0]
        self.status_url = ''
        self.snapshot_url = ''
        self.start_url = ''
        self.stop_url = ''
        self.shutdown_url = ''
        self._set_base_url(self.base_url)
        self.control_token = cfg.get('streamer_control_token', '')

        self.width = 128
        self.height = 128
        self.mode = MODE_STATUS
        self.status = {}
        self.fetch_error = ''
        self.snapshot_error = ''
        self.snapshot_image = None
        self.last_status_refresh = 0.0
        self.last_snapshot_refresh = 0.0
        self.prev_sample = (None, None, 0.0)
        self.tx_kbps = 0
        self.rx_kbps = 0
        self.selected_cam = 1
        self.control_index = 0
        self.notice = ''
        self.notice_until = 0.0
        self.confirm_action = ''
        self.confirm_until = 0.0
        self.offline_mode = OFFLINE_MODE_MATRIX
        self.last_online_at = 0.0
        self.stream_stats = _load_hat_stats()
        self.stats_dirty = False
        self.last_stats_save = 0.0
        self.last_stream_sample = None
        self.last_stream_running = False
        self.weather_temp_line = ''
        self.weather_desc = ''
        self.weather_error = ''
        self.weather_updated_at = 0.0
        self.weather_refresh_after = 0.0
        self.weather_location = (None, None)

        self.font_header = _font(_FONT_PATH_BOLD, 12)
        self.font_title = _font(_FONT_PATH_BOLD, 16)
        self.font_text = _font(_FONT_PATH, 11)
        self.font_small = _font(_FONT_PATH, 9)
        self.font_matrix = _font(_FONT_PATH_BOLD, 10)
        self.font_clock = _font(_FONT_PATH_BOLD, 28)
        self.font_clock_small = _font(_FONT_PATH_BOLD, 14)
        self.font_metric = _font(_FONT_PATH_BOLD, 13)
        self.matrix = MatrixRainLite(self.width, self.height)
        self.retro = RetroGeometryLite(self.width, self.height)
        self.plasma = PlasmaFieldLite(self.width, self.height)
        self.last_frame_at = time.time()

        LCD_Config.GPIO_Init()
        self.lcd = LCD_1in44.LCD()
        self.lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
        self.lcd.LCD_Clear()

        for pin in BUTTON_PINS.values():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.previous_buttons = {name: self._pressed(pin) for name, pin in BUTTON_PINS.items()}

    def _vertical_edge(self, direction, current):
        mapped = direction
        if self.args.invert_vertical:
            mapped = 'down' if direction == 'up' else 'up'
        return self._edge(mapped, current)

    def _pressed(self, pin):
        return GPIO.input(pin) == GPIO.LOW

    def _edge(self, name, current):
        return current[name] and not self.previous_buttons[name]

    def _camera_choices(self):
        cams = self.status.get('available_cams') or []
        if not cams:
            return [self.selected_cam]
        return [int(cam) for cam in cams]

    def _camera_label(self, cam_idx):
        return CAMERA_LABELS.get(cam_idx, f'CAM {cam_idx}')

    def _fetch_json(self, url):
        with urllib.request.urlopen(url, timeout=self.args.http_timeout) as resp:
            return json.load(resp)

    def _post(self, url, data=None):
        payload = None
        headers = {}
        if data is not None:
            payload = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if url == self.shutdown_url and self.control_token:
            headers['X-Streamer-Control-Token'] = self.control_token
        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=self.args.http_timeout) as resp:
            body = resp.read().decode() or '{}'
        return json.loads(body)

    def _set_notice(self, text, seconds=2.5):
        self.notice = _clip(text, 26)
        self.notice_until = time.time() + seconds

    def _forecast_coords(self):
        cfg = _load_cfg()
        lat = cfg.get('forecast_latitude')
        lon = cfg.get('forecast_longitude')
        if lat is None or lon is None:
            return (None, None)
        return (lat, lon)

    def _forecast_high_low(self, periods):
        current = periods[0] if periods else {}
        current_temp = current.get('temperature')
        if current_temp is None:
            return (None, None)
        high = current_temp if current.get('isDaytime') else None
        low = current_temp if not current.get('isDaytime') else None
        for period in periods[1:6]:
            temp = period.get('temperature')
            if temp is None:
                continue
            if period.get('isDaytime') and high is None:
                high = temp
            if not period.get('isDaytime') and low is None:
                low = temp
            if high is not None and low is not None:
                break
        return (high, low)

    def _format_forecast(self, periods):
        current = periods[0] if periods else {}
        temp = current.get('temperature')
        unit = str(current.get('temperatureUnit') or 'F').strip()[:1]
        short = str(current.get('shortForecast') or '').strip()
        if temp is None and not short:
            return '', ''
        high, low = self._forecast_high_low(periods)
        temp_line = f'{temp}{unit}' if temp is not None else ''
        if high is not None and low is not None:
            temp_line = f'{temp}{unit}-{high}/{low}'
        elif high is not None and high != temp:
            temp_line = f'{temp}{unit}-{high}'
        elif low is not None and low != temp:
            temp_line = f'{temp}{unit}-{low}'
        return temp_line[:16], short[:36]

    def _refresh_weather(self, force=False):
        now = time.time()
        coords = self._forecast_coords()
        if coords != self.weather_location:
            self.weather_location = coords
            self.weather_temp_line = ''
            self.weather_desc = ''
            self.weather_error = ''
            self.weather_updated_at = 0.0
            self.weather_refresh_after = 0.0
        lat, lon = coords
        if lat is None or lon is None:
            self.weather_error = 'Set forecast lat/lon in companion UI'
            return
        if not force and now < self.weather_refresh_after and (self.weather_temp_line or self.weather_desc or self.weather_error):
            return
        try:
            req = urllib.request.Request(
                f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}',
                headers={'User-Agent': 'youtube-pi-zero-hat/1.0'},
            )
            with urllib.request.urlopen(req, timeout=min(self.args.http_timeout, 4.0)) as resp:
                points = json.load(resp)
            forecast_url = (((points.get('properties') or {}).get('forecast')) or '').strip()
            if not forecast_url:
                raise ValueError('Forecast endpoint unavailable')
            req = urllib.request.Request(
                forecast_url,
                headers={'User-Agent': 'youtube-pi-zero-hat/1.0'},
            )
            with urllib.request.urlopen(req, timeout=min(self.args.http_timeout, 4.0)) as resp:
                forecast = json.load(resp)
            periods = ((forecast.get('properties') or {}).get('periods')) or []
            temp_line, desc = self._format_forecast(periods)
            if not temp_line and not desc:
                raise ValueError('Forecast summary unavailable')
            self.weather_temp_line = temp_line
            self.weather_desc = desc
            self.weather_error = ''
            self.weather_updated_at = now
            self.weather_refresh_after = now + 900.0
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            self.weather_temp_line = ''
            self.weather_desc = ''
            self.weather_error = str(exc)
            self.weather_refresh_after = now + 300.0

    def _set_base_url(self, base_url):
        self.base_url = base_url.rstrip('/')
        self.status_url = self.base_url + '/status'
        self.snapshot_url = self.base_url + '/snapshot'
        self.start_url = self.base_url + '/start'
        self.stop_url = self.base_url + '/stop'
        self.shutdown_url = self.base_url + '/shutdown'

    def _candidate_base_urls(self):
        urls = _streamer_base_urls(self.args.base_url)
        self.base_urls = urls
        if self.base_url in urls:
            return [self.base_url] + [url for url in urls if url != self.base_url]
        return urls

    def _mark_stream_seconds(self, delta, now):
        if delta <= 0:
            return
        daily = self.stream_stats.setdefault('daily_seconds', {})
        day_key = time.strftime('%Y-%m-%d', time.localtime(now))
        daily[day_key] = _safe_float(daily.get(day_key)) + delta
        self.stream_stats['total_seconds'] = _safe_float(self.stream_stats.get('total_seconds')) + delta
        self.stream_stats['updated_at'] = int(now)
        self.stats_dirty = True

    def _flush_stream_stats(self, force=False):
        now = time.time()
        if not self.stats_dirty:
            return
        if not force and now - self.last_stats_save < 20.0:
            return
        _save_hat_stats(self.stream_stats)
        self.stats_dirty = False
        self.last_stats_save = now

    def _update_stream_stats(self, now, running):
        if self.last_stream_sample is not None and self.last_stream_running and running:
            self._mark_stream_seconds(max(0.0, now - self.last_stream_sample), now)
        self.last_stream_sample = now
        self.last_stream_running = running
        self._flush_stream_stats(force=not running)

    def _handle_stream_disconnect(self):
        self.last_stream_sample = None
        self.last_stream_running = False
        self._flush_stream_stats(force=True)

    def _cycle_offline_mode(self, step):
        total = len(OFFLINE_MODE_LABELS)
        self.offline_mode = (self.offline_mode + step) % total

    def _month_totals(self, count=12):
        buckets = {}
        for day_key, value in (self.stream_stats.get('daily_seconds') or {}).items():
            buckets[day_key[:7]] = buckets.get(day_key[:7], 0.0) + _safe_float(value)

        cursor = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months = []
        for _ in range(count):
            months.append(cursor.strftime('%Y-%m'))
            if cursor.month == 1:
                cursor = cursor.replace(year=cursor.year - 1, month=12)
            else:
                cursor = cursor.replace(month=cursor.month - 1)
        months.reverse()
        return [(month, buckets.get(month, 0.0)) for month in months]

    def _last_seen_label(self):
        if not self.last_online_at:
            return 'never'
        delta = max(0, int(time.time() - self.last_online_at))
        if delta < 60:
            return f'{delta}s ago'
        if delta < 3600:
            return f'{delta // 60}m ago'
        return f'{delta // 3600}h ago'

    def _refresh_status(self, force=False):
        now = time.time()
        if not force and now - self.last_status_refresh < self.args.poll_seconds:
            return
        self.last_status_refresh = now
        was_offline = bool(self.fetch_error)
        last_exc = None
        for base_url in self._candidate_base_urls():
            try:
                self._set_base_url(base_url)
                self.status = self._fetch_json(self.status_url)
                last_exc = None
                break
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                last_exc = exc
        if last_exc is None:
            self.fetch_error = ''
            self.last_online_at = now
            self._update_stream_stats(now, bool(self.status.get('running')))

            eth = self.status.get('eth0') or {}
            tx_now = eth.get('tx_bytes')
            rx_now = eth.get('rx_bytes')
            prev_tx, prev_rx, prev_time = self.prev_sample
            if prev_time and tx_now is not None and rx_now is not None and now > prev_time:
                dt_ms = max(1.0, (now - prev_time) * 1000.0)
                self.tx_kbps = int(max(0, int(tx_now) - int(prev_tx)) * 8.0 / dt_ms)
                self.rx_kbps = int(max(0, int(rx_now) - int(prev_rx)) * 8.0 / dt_ms)
            self.prev_sample = (tx_now, rx_now, now)

            choices = self._camera_choices()
            if self.selected_cam not in choices:
                self.selected_cam = 1 if 1 in choices else choices[0]
            if self.control_index >= len(self._idle_control_actions()):
                self.control_index = 0
        else:
            self._handle_stream_disconnect()
            self.fetch_error = str(last_exc)
            self.status = {}
            if not was_offline:
                self.offline_mode = OFFLINE_MODE_MATRIX

    def _refresh_snapshot(self, force=False):
        now = time.time()
        if not force and now - self.last_snapshot_refresh < self.args.snapshot_seconds:
            return
        self.last_snapshot_refresh = now

        if self.status.get('running'):
            self.snapshot_image = None
            self.snapshot_error = 'Preview disabled while live'
            return

        try:
            with urllib.request.urlopen(self.snapshot_url, timeout=self.args.http_timeout) as resp:
                raw = resp.read()
            image = Image.open(io.BytesIO(raw)).convert('RGB')
            self.snapshot_image = ImageOps.fit(image, (self.width, self.height), _RESAMPLE)
            self.snapshot_error = ''
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            self.snapshot_image = None
            self.snapshot_error = str(exc)

    def _current_action(self):
        if self.status.get('running'):
            return 'stop'
        actions = self._idle_control_actions()
        if not actions:
            return 'shutdown'
        self.control_index = min(self.control_index, len(actions) - 1)
        action = actions[self.control_index]
        if action.startswith('start:'):
            try:
                self.selected_cam = int(action.split(':', 1)[1])
            except ValueError:
                pass
        return action

    def _idle_control_actions(self):
        actions = [f'start:{cam_idx}' for cam_idx in self._camera_choices()]
        actions.append('shutdown')
        return actions

    def _action_label(self, action):
        if action == 'stop':
            return 'STOP STREAM'
        if action == 'shutdown':
            return 'SHUTDOWN PI4'
        cam_idx = int(action.split(':', 1)[1])
        return f'START {self._camera_label(cam_idx)}'

    def _execute_control_action(self):
        action = self._current_action()
        try:
            if action == 'stop':
                result = self._post(self.stop_url)
            elif action == 'shutdown':
                result = self._post(self.shutdown_url)
            else:
                cam_idx = action.split(':', 1)[1]
                result = self._post(self.start_url, {'cam_idx': cam_idx})
            self._set_notice(result.get('msg') or ('OK' if result.get('ok') else 'Failed'))
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            self._set_notice(f'Action failed: {exc}', seconds=3.5)
        self.confirm_action = ''
        self.confirm_until = 0.0
        self._refresh_status(force=True)

    def _handle_control_press(self):
        action = self._current_action()
        now = time.time()
        if self.confirm_action == action and now < self.confirm_until:
            self._execute_control_action()
            return
        self.confirm_action = action
        self.confirm_until = now + 3.0
        if action == 'stop':
            self._set_notice('Press again to stop')
        elif action == 'shutdown':
            self._set_notice('Press again to shut down Pi4')
        else:
            self._set_notice(f'Press again to start {self._camera_label(self.selected_cam)}')

    def _cycle_mode(self, step):
        self.mode = (self.mode + step) % 3
        self.confirm_action = ''
        self.confirm_until = 0.0
        if self.mode == MODE_SNAPSHOT:
            self._refresh_snapshot(force=True)

    def _handle_buttons(self):
        current = {name: self._pressed(pin) for name, pin in BUTTON_PINS.items()}

        if self._edge('key1', current):
            self.mode = MODE_STATUS
            self.confirm_action = ''
        elif self._edge('key2', current):
            self.mode = MODE_SNAPSHOT
            self.confirm_action = ''
            self._refresh_snapshot(force=True)
        elif self._edge('key3', current):
            self.mode = MODE_CONTROL
            self.confirm_action = ''

        if self.fetch_error:
            if self._edge('left', current):
                self._cycle_offline_mode(-1)
            if self._edge('right', current):
                self._cycle_offline_mode(1)
            if self._edge('press', current):
                self._refresh_status(force=True)
                self._set_notice('Pi4 online' if not self.fetch_error else 'Pi4 still offline', seconds=1.5)
            self.previous_buttons = current
            return

        if self._edge('left', current):
            self._cycle_mode(-1)
        if self._edge('right', current):
            self._cycle_mode(1)

        if self.mode == MODE_CONTROL and not self.status.get('running'):
            actions = self._idle_control_actions()
            if actions:
                if self._vertical_edge('up', current):
                    self.control_index = (self.control_index - 1) % len(actions)
                    self.confirm_action = ''
                if self._vertical_edge('down', current):
                    self.control_index = (self.control_index + 1) % len(actions)
                    self.confirm_action = ''
                action = actions[self.control_index]
                if action.startswith('start:'):
                    self.selected_cam = int(action.split(':', 1)[1])

        if self._edge('press', current):
            if self.mode == MODE_STATUS:
                self._refresh_status(force=True)
                self._set_notice('Status refreshed', seconds=1.5)
            elif self.mode == MODE_SNAPSHOT:
                self._refresh_snapshot(force=True)
                if self.snapshot_error:
                    self._set_notice(self.snapshot_error, seconds=2.0)
                else:
                    self._set_notice('Snapshot refreshed', seconds=1.5)
            else:
                self._handle_control_press()

        self.previous_buttons = current

    def _draw_header(self, draw):
        mode_label = MODE_LABELS[self.mode]
        if self.fetch_error:
            color = (210, 70, 70)
        elif self.status.get('running'):
            color = (90, 210, 120)
        else:
            color = (90, 160, 255)
        draw.rectangle((0, 0, self.width, 16), fill=(12, 16, 20))
        draw.text((4, 2), mode_label, font=self.font_header, fill=color)
        clock = time.strftime('%H:%M')
        draw.text((self.width - 28, 2), clock, font=self.font_small, fill=(170, 170, 170))

    def _draw_footer(self, draw, hint):
        draw.rectangle((0, self.height - 16, self.width, self.height), fill=(12, 16, 20))
        active_notice = self.notice if time.time() < self.notice_until else ''
        if active_notice:
            text = active_notice
            color = (255, 230, 110)
        else:
            text = hint
            color = (140, 140, 140)
        draw.text((4, self.height - 13), _clip(text, 24), font=self.font_small, fill=color)

    def _draw_status_mode(self, draw):
        if self.fetch_error:
            draw.text((6, 24), 'Streamer offline', font=self.font_title, fill=(255, 120, 120))
            for idx, line in enumerate(self._wrap_text(self.fetch_error, 24)[:5]):
                draw.text((6, 48 + idx * 12), line, font=self.font_text, fill=(210, 210, 210))
            self._draw_footer(draw, 'JOY press refresh')
            return

        system = self.status.get('system') or {}
        stream_message = self.status.get('stream_message') or {}
        lines = [
            ('STATE', 'LIVE' if self.status.get('running') else 'IDLE'),
            ('CAM', self.status.get('cam_name') or self.status.get('preview_cam') or '-'),
            ('UP', _fmt_uptime(self.status.get('uptime_s', 0))),
            ('LAN', f'tx:{self.tx_kbps} rx:{self.rx_kbps}'),
            ('TEMP', f"{system.get('temp_c'):.1f} C" if system.get('temp_c') is not None else '-'),
            ('MSG', 'ON' if stream_message.get('enabled') and stream_message.get('text') else 'OFF'),
        ]

        y = 22
        for label, value in lines:
            draw.text((6, y), f'{label}:', font=self.font_text, fill=(110, 170, 255))
            draw.text((48, y), _clip(value, 11), font=self.font_text, fill=(240, 240, 240))
            y += 15

        rtmp = self.status.get('rtmp_state') or '-'
        retries = int(self.status.get('retries') or 0)
        draw.text((6, 112), f'RTMP {rtmp}', font=self.font_small, fill=(170, 170, 170))
        draw.text((82, 112), f'R:{retries}', font=self.font_small, fill=(170, 170, 170))
        self._draw_footer(draw, 'KEY2 snapshot')

    def _draw_snapshot_mode(self, image, draw):
        if self.snapshot_image:
            image.paste(self.snapshot_image, (0, 0))
            overlay = Image.new('RGBA', (self.width, self.height), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle((0, 0, self.width, 18), fill=(0, 0, 0, 140))
            overlay_draw.rectangle((0, self.height - 18, self.width, self.height), fill=(0, 0, 0, 150))
            image.alpha_composite(overlay)
            draw = ImageDraw.Draw(image)
            draw.text((4, 20), 'PREVIEW READY', font=self.font_small, fill=(255, 255, 255))
        else:
            draw.rectangle((0, 18, self.width, self.height - 16), fill=(18, 22, 28))
            msg = self.snapshot_error or 'Waiting for snapshot'
            for idx, line in enumerate(self._wrap_text(msg, 20)[:6]):
                draw.text((8, 34 + idx * 12), line, font=self.font_text, fill=(230, 230, 230))

        cam_label = self.status.get('preview_cam') or self._camera_label(self.selected_cam)
        draw.text((4, 2), 'SNAPSHOT', font=self.font_header, fill=(120, 220, 255))
        draw.text((4, self.height - 13), _clip(cam_label, 22), font=self.font_small, fill=(255, 255, 255))
        draw.text((90, self.height - 13), 'PRESS', font=self.font_small, fill=(120, 220, 255))

    def _draw_control_mode(self, draw):
        running = bool(self.status.get('running'))
        if self.fetch_error:
            draw.text((6, 24), 'No local API', font=self.font_title, fill=(255, 120, 120))
            for idx, line in enumerate(self._wrap_text(self.fetch_error, 22)[:5]):
                draw.text((6, 48 + idx * 12), line, font=self.font_text, fill=(220, 220, 220))
            self._draw_footer(draw, 'KEY1 status')
            return

        if running:
            draw.rectangle((8, 28, 120, 86), outline=(220, 90, 90), width=2, fill=(48, 10, 10))
            draw.text((18, 36), 'STOP', font=self.font_title, fill=(255, 110, 110))
            draw.text((18, 56), 'STREAM', font=self.font_title, fill=(255, 210, 210))
            draw.text((10, 92), _clip(self.status.get('cam_name') or '-', 18), font=self.font_text, fill=(230, 230, 230))
            hint = 'Press twice to stop'
        else:
            action = self._current_action()
            if action == 'shutdown':
                outline = (220, 180, 80)
                fill = (54, 26, 8)
                title = ('SHUT', 'DOWN')
                title_fill = (255, 210, 120)
                detail = 'PI 4 graceful off'
                hint = 'Press twice to shut down'
            else:
                outline = (90, 200, 110)
                fill = (12, 36, 16)
                title = ('START', 'STREAM')
                title_fill = (120, 220, 130)
                detail = self._camera_label(self.selected_cam)
                hint = 'Press twice to start'
            draw.rectangle((8, 28, 120, 86), outline=outline, width=2, fill=fill)
            draw.text((18, 36), title[0], font=self.font_title, fill=title_fill)
            draw.text((18, 56), title[1], font=self.font_title, fill=(220, 255, 220) if action != 'shutdown' else (255, 236, 180))
            draw.text((10, 92), _clip(detail, 18), font=self.font_text, fill=(230, 230, 230))
            draw.text((10, 106), 'UP/DOWN choose', font=self.font_small, fill=(170, 170, 170))

        if self.confirm_action and time.time() < self.confirm_until:
            secs = max(0, int(self.confirm_until - time.time()) + 1)
            draw.text((10, 18), f'ARMED {secs}s', font=self.font_small, fill=(255, 220, 120))
        self._draw_footer(draw, hint)

    def _wrap_text(self, text, width_chars):
        words = str(text or '').split()
        if not words:
            return ['']
        lines = []
        current = words[0]
        for word in words[1:]:
            trial = current + ' ' + word
            if len(trial) <= width_chars:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _draw_offline_matrix(self, image, draw):
        now = time.time()
        dt = max(0.02, min(0.20, now - self.last_frame_at))
        self.matrix.update(dt)
        image.paste((0, 6, 0), (0, 0, self.width, self.height))
        for x, y, char, color in self.matrix.glyphs():
            draw.text((x, y), char, font=self.font_matrix, fill=color)

    def _draw_offline_clock(self, image, draw):
        image.paste((4, 8, 18), (0, 0, self.width, self.height))
        now = datetime.now()
        self._refresh_weather()
        for x in range(0, self.width, 16):
            draw.line((x, 18, x, self.height - 18), fill=(18, 24, 36))
        for y in range(18, self.height - 18, 16):
            draw.line((0, y, self.width, y), fill=(18, 24, 36))
        draw.text((18, 12), now.strftime('%H:%M'), font=self.font_clock, fill=(120, 220, 255))
        draw.text((24, 44), now.strftime('%a %b %d'), font=self.font_clock_small, fill=(220, 236, 255))
        temp_line = self.weather_temp_line or '--'
        temp_color = (232, 238, 244) if self.weather_temp_line else (176, 184, 196)
        draw.text((10, 62), temp_line, font=self.font_clock_small, fill=temp_color)
        forecast = self.weather_desc or self.weather_error or 'Forecast unavailable'
        color = (232, 238, 244) if self.weather_desc else (176, 184, 196)
        lines = self._wrap_text(forecast, 18)[:2]
        y = 92
        for line in lines:
            draw.text((10, y), line, font=self.font_small, fill=color)
            y += 12

    def _draw_offline_stats(self, image, draw):
        image.paste((8, 8, 12), (0, 0, self.width, self.height))
        total_seconds = _safe_float(self.stream_stats.get('total_seconds'))
        today_key = time.strftime('%Y-%m-%d')
        today_seconds = _safe_float((self.stream_stats.get('daily_seconds') or {}).get(today_key))
        labels = [
            ('ZERO UP', _fmt_compact_duration(_read_system_uptime())),
            ('TODAY', _fmt_compact_duration(today_seconds)),
            ('TOTAL', _fmt_compact_duration(total_seconds)),
            ('LAST PI4', self._last_seen_label()),
        ]
        y = 20
        for label, value in labels:
            draw.text((8, y), label, font=self.font_small, fill=(132, 160, 190))
            draw.text((58, y - 1), value, font=self.font_metric, fill=(230, 236, 244))
            y += 14

        chart_x = 8
        chart_y = 80
        chart_w = 112
        chart_h = 30
        draw.rounded_rectangle((chart_x, chart_y, chart_x + chart_w, chart_y + chart_h), radius=6, outline=(70, 100, 132), width=1, fill=(12, 16, 24))
        months = self._month_totals()
        max_value = max([value for _, value in months] or [0.0])
        if max_value <= 0:
            draw.text((20, 91), 'No live history yet', font=self.font_small, fill=(136, 148, 166))
            return

        bar_w = 7
        gap = 2
        for idx, (_, value) in enumerate(months):
            x1 = chart_x + 4 + idx * (bar_w + gap)
            x2 = x1 + bar_w - 1
            height = max(2, int((value / max_value) * (chart_h - 10))) if value > 0 else 1
            y1 = chart_y + chart_h - 4 - height
            color = (100, 210, 120) if idx == len(months) - 1 else (88, 146, 220)
            draw.rectangle((x1, y1, x2, chart_y + chart_h - 4), fill=color)
        draw.text((10, 112), '12M LIVE', font=self.font_small, fill=(142, 170, 200))

    def _draw_offline_retro(self, image, draw):
        dt = max(0.02, min(0.20, time.time() - self.last_frame_at))
        self.retro.update(dt)
        self.retro.draw(image, draw)

    def _draw_offline_plasma(self, image, draw):
        dt = max(0.02, min(0.20, time.time() - self.last_frame_at))
        self.plasma.update(dt)
        image.paste(self.plasma.draw(), (0, 0))

    def _draw_offline_header(self, draw):
        draw.rectangle((0, 0, self.width, 16), fill=(8, 12, 16))
        draw.text((4, 2), 'PI4 OFF', font=self.font_header, fill=(132, 236, 144))
        draw.text((70, 2), OFFLINE_MODE_LABELS[self.offline_mode], font=self.font_small, fill=(196, 204, 212))

    def _draw_offline_view(self, image, draw):
        if self.offline_mode == OFFLINE_MODE_MATRIX:
            self._draw_offline_matrix(image, draw)
        elif self.offline_mode == OFFLINE_MODE_CLOCK:
            self._draw_offline_clock(image, draw)
        elif self.offline_mode == OFFLINE_MODE_STATS:
            self._draw_offline_stats(image, draw)
        elif self.offline_mode == OFFLINE_MODE_RETRO:
            self._draw_offline_retro(image, draw)
        else:
            self._draw_offline_plasma(image, draw)
        draw = ImageDraw.Draw(image)
        self._draw_offline_header(draw)
        self._draw_footer(draw, 'L/R mode  PRESS ping')

    def _render(self):
        base = Image.new('RGBA', (self.width, self.height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(base)

        if self.fetch_error:
            self._draw_offline_view(base, draw)
        elif self.mode == MODE_STATUS:
            self._draw_header(draw)
            self._draw_status_mode(draw)
        elif self.mode == MODE_SNAPSHOT:
            self._draw_snapshot_mode(base, draw)
        else:
            self._draw_header(draw)
            self._draw_control_mode(draw)

        output = base.convert('RGB')
        if self.args.rotate_180:
            output = output.rotate(180)
        self.lcd.LCD_ShowImage(output, 0, 0)
        self.last_frame_at = time.time()

    def run(self):
        while True:
            if self.confirm_action and time.time() >= self.confirm_until:
                self.confirm_action = ''
                self.confirm_until = 0.0

            self._refresh_status()
            if self.mode == MODE_SNAPSHOT:
                self._refresh_snapshot()
            self._handle_buttons()
            self._render()
            time.sleep(0.05)

    def close(self):
        self._handle_stream_disconnect()
        try:
            self.lcd.LCD_Clear()
        except Exception:
            pass
        GPIO.cleanup()


def main():
    parser = argparse.ArgumentParser(description='YouTube Studio LCD HAT UI')
    parser.add_argument('--base-url', default='')
    parser.add_argument('--invert-vertical', action='store_true')
    parser.add_argument('--rotate-180', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=1.0)
    parser.add_argument('--snapshot-seconds', type=float, default=2.5)
    parser.add_argument('--http-timeout', type=float, default=3.0)
    args = parser.parse_args()

    app = StudioHatUI(args)
    try:
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        app.close()


if __name__ == '__main__':
    main()
