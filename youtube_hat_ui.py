#!/usr/bin/env python3
"""Waveshare 1.44" LCD HAT controller for YouTube Studio."""

import argparse
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

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


def _load_cfg():
    cfg = {
        'streamer_status_host': '',
        'streamer_status_port': 8090,
        'streamer_control_token': '',
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
    return cfg


def _resolve_base_url(base_url):
    if base_url:
        return base_url.rstrip('/')
    cfg = _load_cfg()
    host = cfg.get('streamer_status_host')
    if host:
        return f"http://{host}:{cfg['streamer_status_port']}"
    return 'http://127.0.0.1:8090'


class StudioHatUI:
    def __init__(self, args):
        self.args = args
        cfg = _load_cfg()
        self.base_url = _resolve_base_url(args.base_url)
        self.status_url = self.base_url + '/status'
        self.snapshot_url = self.base_url + '/snapshot'
        self.start_url = self.base_url + '/start'
        self.stop_url = self.base_url + '/stop'
        self.shutdown_url = self.base_url + '/shutdown'
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

        self.font_header = _font(_FONT_PATH_BOLD, 12)
        self.font_title = _font(_FONT_PATH_BOLD, 16)
        self.font_text = _font(_FONT_PATH, 11)
        self.font_small = _font(_FONT_PATH, 9)

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

    def _refresh_status(self, force=False):
        now = time.time()
        if not force and now - self.last_status_refresh < self.args.poll_seconds:
            return
        self.last_status_refresh = now
        try:
            self.status = self._fetch_json(self.status_url)
            self.fetch_error = ''

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
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            self.fetch_error = str(exc)
            self.status = {}

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

    def _render(self):
        base = Image.new('RGBA', (self.width, self.height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(base)

        if self.mode == MODE_STATUS:
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
