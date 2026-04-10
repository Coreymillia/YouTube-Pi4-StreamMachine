#!/usr/bin/env python3
"""
YouTube Studio — Raspberry Pi 4 YouTube live streaming daemon.

No LCD HAT, no physical buttons, no GPIO required.
Control via web browser on any device on the LAN.

Web UI:  http://<pi-ip>:8090/
Preview: http://<pi-ip>:8090/preview   (live MJPEG ~5 fps, use for focus)
Status:  http://<pi-ip>:8090/status    (JSON)
"""

import os, sys, time, subprocess, threading, json, socket, logging, html
import collections, random
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote_plus, urlencode

try:
    import requests as _http
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('yt-studio')

_PROJECT_DIR  = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH  = os.path.join(_PROJECT_DIR, 'config.json')
_PORT         = 8090
_YT_RTMP_BASE = 'rtmp://a.rtmp.youtube.com/live2'

# ── OTR radio stations ────────────────────────────────────────────────────────
_OTR_STATIONS = [
    ('1940s Radio',       'http://149.255.60.195:8256/stream'),
    ('American Comedy',   'http://149.255.60.193:8162/stream'),
    ('American Classics', 'http://149.255.60.194:8043/stream'),
    ('Jazz Central',      'http://149.255.60.195:8027/stream'),
    ('Comedy Gold',       'http://149.255.60.195:8150/stream'),
    ('Mystery Radio',     'http://149.255.60.195:8168/stream'),
    ('Crime & Suspense',  'http://149.255.60.193:8168/stream'),
    ('Crime Radio',       'http://149.255.60.194:8039/stream'),
    ('Adventure Stories', 'http://149.255.60.195:8162/stream'),
    ('Drama Radio',       'http://149.255.60.195:8174/stream'),
    ('Nostalgia Lane',    'http://149.255.60.195:8180/stream'),
    ('Science Fiction',   'http://149.255.60.194:8110/stream'),
]
_OTR_DEFAULT = 'http://149.255.60.195:8256/stream'

def _otr_name(url):
    for name, u in _OTR_STATIONS:
        if u == url:
            return name
    return 'Custom'

# ── Quality defaults ─────────────────────────────────────────────────────────
# Stored in config.json under 'quality'. All values are floats in UI-friendly
# ranges that map directly to rpicam-vid flags and ffmpeg eq filter.
#   brightness : -1.0 → +1.0   (0.0  = no change)
#   contrast   :  0.0 → 2.0    (1.0  = no change)
#   saturation :  0.0 → 2.0    (1.0  = no change)
#   sharpness  :  0.0 → 2.0    (1.0  = no change)
#   zoom       :  1.0 → 4.0    (1.0  = full sensor, 2.0 = 2× center crop)
_QUALITY_DEFAULTS = {
    'brightness': 0.0,
    'contrast':   1.0,
    'saturation': 1.0,
    'sharpness':  1.0,
    'zoom':       1.0,
}

def _get_quality(cfg):
    q = dict(_QUALITY_DEFAULTS)
    q.update(cfg.get('quality', {}))
    # clamp to valid ranges
    q['brightness'] = max(-1.0, min(1.0,  float(q['brightness'])))
    q['contrast']   = max(0.0,  min(2.0,  float(q['contrast'])))
    q['saturation'] = max(0.0,  min(2.0,  float(q['saturation'])))
    q['sharpness']  = max(0.0,  min(2.0,  float(q['sharpness'])))
    q['zoom']       = max(1.0,  min(4.0,  float(q['zoom'])))
    return q

def _csi_quality_args(q):
    """Extra rpicam-vid flags for CSI cameras."""
    args = [
        '--brightness', f"{q['brightness']:.3f}",
        '--contrast',   f"{q['contrast']:.3f}",
        '--saturation', f"{q['saturation']:.3f}",
        '--sharpness',  f"{q['sharpness']:.3f}",
    ]
    z = q['zoom']
    if z > 1.01:
        w = 1.0 / z
        x = (1.0 - w) / 2.0
        args += ['--roi', f'{x:.4f},{x:.4f},{w:.4f},{w:.4f}']
    return args

def _usb_quality_filter(q):
    """ffmpeg eq filter string for USB cameras."""
    # ffmpeg eq: brightness -1..1, contrast 0..2, saturation 0..3
    sat = q['saturation'] * 1.5   # map 0-2 → 0-3
    return (
        f"eq=brightness={q['brightness']:.3f}"
        f":contrast={q['contrast']:.3f}"
        f":saturation={sat:.3f}"
    )

# ── Camera profiles ───────────────────────────────────────────────────────────
# 'type' usb  → v4l2 H264 passthrough via ffmpeg
# 'type' csi  → rpicam-vid H264 pipe into ffmpeg (HQ cam, Pi cam, etc.)
_CAMERAS = [
    {'name': 'USB Microscope', 'short': 'USB-CAM',
     'type': 'usb', 'device': None,
     'stream_w': 1280, 'stream_h': 720,  'stream_fps': 30,
     'prev_w':    640, 'prev_h':   480,  'prev_fps':    5},
    {'name': 'HQ Camera',      'short': 'HQ-CAM',
     'type': 'csi',
     'stream_w': 1920, 'stream_h': 1080, 'stream_fps': 30,
     'prev_w':    640, 'prev_h':   480,  'prev_fps':    5},
]

# ── Config helpers ────────────────────────────────────────────────────────────
def _load_cfg():
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(cfg):
    with open(_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=4)

def _get_local_ip():
    """Try up to 10s for a real routable IP (DHCP may not be ready at boot)."""
    for _ in range(10):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith('127.'):
                return ip
        except Exception:
            pass
        time.sleep(1)
    return '0.0.0.0'

_available_cams = []   # list of indices detected at startup

def _detect_cameras():
    """Returns list of available camera indices."""
    available = []
    # CSI cameras via rpicam
    try:
        result = subprocess.run(
            ['rpicam-hello', '--list-cameras'],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout + result.stderr
        if 'imx477' in output:
            available.append(1)  # HQ Camera
        elif 'Available cameras' in output and 'No cameras' not in output:
            available.append(1)  # any CSI cam maps to HQ slot for now
    except Exception:
        pass
    # USB cam via v4l2
    try:
        dev = _find_usb_video_device()
        out = subprocess.check_output(
            ['v4l2-ctl', '-d', dev, '--info'],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        )
        if 'usb' in out.lower():
            available.append(0)
    except Exception:
        pass
    return available
    """Return the first /dev/videoN whose bus info reports 'usb'."""
    import glob as _g
    for dev in sorted(_g.glob('/dev/video*')):
        try:
            out = subprocess.check_output(
                ['v4l2-ctl', '-d', dev, '--info'],
                stderr=subprocess.DEVNULL, text=True, timeout=2,
            )
            if 'usb' in out.lower():
                return dev
        except Exception:
            continue
    return '/dev/video0'

def _terminate(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

# ── Live MJPEG preview ────────────────────────────────────────────────────────
_prev_lock     = threading.Lock()
_prev_frame    = None       # latest JPEG bytes
_prev_clients  = 0          # number of browser tabs watching
_prev_proc     = None       # capture subprocess
_prev_cam_name = None       # name of camera being previewed
_prev_stop_evt = threading.Event()

def _parse_mjpeg_frames(proc, on_frame):
    """Read raw MJPEG from proc.stdout and call on_frame(jpeg_bytes) per frame."""
    SOI = b'\xff\xd8'
    EOI = b'\xff\xd9'
    buf = bytearray()
    while not _prev_stop_evt.is_set():
        try:
            chunk = proc.stdout.read(8192)
        except Exception:
            break
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            start = buf.find(SOI)
            if start == -1:
                buf.clear()
                break
            end = buf.find(EOI, start + 2)
            if end == -1:
                if start > 0:
                    del buf[:start]
                break
            frame = bytes(buf[start:end + 2])
            del buf[:end + 2]
            on_frame(frame)

def _preview_worker(cam, quality):
    global _prev_frame, _prev_proc, _prev_cam_name
    log.info('Preview starting → %s', cam['name'])
    _prev_cam_name = cam['name']

    pw, ph, pfps = cam['prev_w'], cam['prev_h'], cam['prev_fps']

    if cam['type'] == 'usb':
        dev = cam.get('device') or _find_usb_video_device()
        eq  = _usb_quality_filter(quality)
        cmd = [
            'ffmpeg', '-loglevel', 'quiet',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', f'{pw}x{ph}', '-framerate', str(pfps),
            '-i', dev,
            '-vf', eq,
            '-f', 'mjpeg', '-q:v', '5',
            'pipe:1',
        ]
    else:  # csi
        cmd = (
            ['rpicam-vid', '-t', '0',
             '--codec', 'mjpeg',
             '--width', str(pw), '--height', str(ph),
             '--framerate', str(pfps),
             '--nopreview', '-o', '-']
            + _csi_quality_args(quality)
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error('Preview launch failed: %s', exc)
        return

    with _prev_lock:
        _prev_proc = proc

    def _on_frame(jpeg):
        global _prev_frame
        with _prev_lock:
            _prev_frame = jpeg

    _parse_mjpeg_frames(proc, _on_frame)
    _terminate(proc)
    log.info('Preview stopped → %s', cam['name'])

_prev_thread = None

def start_preview(cam):
    global _prev_thread, _prev_frame
    stop_preview()
    _prev_stop_evt.clear()
    with _prev_lock:
        _prev_frame = None
    quality = _get_quality(_load_cfg())
    _prev_thread = threading.Thread(target=_preview_worker, args=(cam, quality), daemon=True)
    _prev_thread.start()

def stop_preview():
    global _prev_proc
    _prev_stop_evt.set()
    with _prev_lock:
        proc = _prev_proc
        _prev_proc = None
    _terminate(proc)

# ── YouTube stream ─────────────────────────────────────────────────────────────
_stream_lock  = threading.Lock()
_stream_state = {
    'running':    False,
    'cam_name':   '',
    'start_time': None,
    'retries':    0,
    'error':      '',
}
_stream_proc_main  = None   # ffmpeg process
_stream_proc_libcam = None  # rpicam-vid process (CSI only)
_stream_stop_evt   = threading.Event()

def _build_stream_cmds(cam, rtmp_url, otr_url, quality):
    w, h, fps = cam['stream_w'], cam['stream_h'], cam['stream_fps']
    if cam['type'] == 'usb':
        dev = cam.get('device') or _find_usb_video_device()
        subprocess.run(
            ['v4l2-ctl', '-d', dev, '--set-ctrl=h264_i_frame_period=60'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        eq = _usb_quality_filter(quality)
        libcam_cmd = None
        ffmpeg_cmd = [
            'ffmpeg', '-loglevel', 'warning',
            '-f', 'v4l2', '-input_format', 'h264',
            '-video_size', f'{w}x{h}', '-framerate', str(fps),
            '-i', dev,
            '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
            '-map', '0:v', '-map', '1:a',
            '-vf', eq,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', '2500k',
            '-c:a', 'aac', '-b:a', '128k',
            '-f', 'flv', rtmp_url,
        ]
    else:  # csi
        libcam_cmd = (
            ['rpicam-vid', '-t', '0',
             '--codec', 'h264',
             '--profile', 'high', '--level', '4.1',
             '--width', str(w), '--height', str(h),
             '--framerate', str(fps),
             '--bitrate', '4000000',
             '--intra', str(fps * 2),
             '--inline', '--nopreview',
             '-o', '-']
            + _csi_quality_args(quality)
        )
        ffmpeg_cmd = [
            'ffmpeg', '-loglevel', 'warning',
            '-f', 'h264', '-i', 'pipe:0',
            '-i', otr_url,
            '-map', '0:v', '-map', '1:a',
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '128k',
            '-f', 'flv', rtmp_url,
        ]
    return libcam_cmd, ffmpeg_cmd

def _stream_worker(cam, rtmp_url, otr_url, quality):
    global _stream_proc_main, _stream_proc_libcam, _stream_state

    MAX_RETRIES = 5
    retries = 0

    def _launch():
        libcam_cmd, ffmpeg_cmd = _build_stream_cmds(cam, rtmp_url, otr_url, quality)
        if libcam_cmd:
            lc = subprocess.Popen(libcam_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ff = subprocess.Popen(ffmpeg_cmd, stdin=lc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            lc.stdout.close()
            return lc, ff
        return None, subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    lc, ff = _launch()
    with _stream_lock:
        _stream_proc_libcam = lc
        _stream_proc_main   = ff

    log.info('Stream started → %s  RTMP: %s', cam['name'], rtmp_url[-20:])

    with _stream_lock:
        _stream_state['running']    = True
        _stream_state['cam_name']   = cam['name']
        _stream_state['start_time'] = time.monotonic()
        _stream_state['retries']    = 0
        _stream_state['error']      = ''

    while not _stream_stop_evt.is_set():
        time.sleep(2)
        if ff.poll() is not None and not _stream_stop_evt.is_set():
            retries += 1
            with _stream_lock:
                _stream_state['retries'] = retries
            if retries > MAX_RETRIES:
                log.error('Stream failed after %d retries', MAX_RETRIES)
                with _stream_lock:
                    _stream_state['error'] = f'Failed after {MAX_RETRIES} retries'
                break
            log.warning('Stream dropped — reconnect %d/%d', retries, MAX_RETRIES)
            _terminate(lc)
            _terminate(ff)
            time.sleep(5)
            lc, ff = _launch()
            with _stream_lock:
                _stream_proc_libcam = lc
                _stream_proc_main   = ff

    _terminate(lc)
    _terminate(ff)
    with _stream_lock:
        _stream_state['running']    = False
        _stream_state['start_time'] = None
        _stream_proc_main           = None
        _stream_proc_libcam         = None
    log.info('Stream stopped')

_stream_thread = None

def start_stream(cam_idx):
    global _stream_thread
    cfg = _load_cfg()
    stream_key = cfg.get('youtube_stream_key', '').strip()
    if not stream_key:
        return False, 'No stream key set. Add it in Settings.'
    if _stream_state['running']:
        return False, 'Stream already running.'

    cam = dict(_CAMERAS[cam_idx])
    if cam['type'] == 'usb':
        cam['device'] = _find_usb_video_device()

    otr_url  = cfg.get('otr_station_url', _OTR_DEFAULT).strip() or _OTR_DEFAULT
    quality  = _get_quality(cfg)
    rtmp_url = f'{_YT_RTMP_BASE}/{stream_key}'

    _stream_stop_evt.clear()
    _stream_thread = threading.Thread(
        target=_stream_worker, args=(cam, rtmp_url, otr_url, quality), daemon=True
    )
    _stream_thread.start()
    start_bot()
    return True, 'Stream starting…'

def stop_stream():
    stop_bot()
    _stream_stop_evt.set()
    with _stream_lock:
        _terminate(_stream_proc_libcam)
        _terminate(_stream_proc_main)

# ── YouTube Live Chat Bot ─────────────────────────────────────────────────────
_TOKEN_PATH      = os.path.join(_PROJECT_DIR, 'token.json')
_DEVICE_CODE_URL = 'https://oauth2.googleapis.com/device/code'
_OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
_YT_API          = 'https://www.googleapis.com/youtube/v3'
_OAUTH_SCOPE     = 'https://www.googleapis.com/auth/youtube'

_BOT_DEFAULTS = {
    'enabled':                  False,
    'headless_message':         "Hey chat! \U0001f399\ufe0f I'm headless \u2014 no screen or keyboard here, so I can't see your messages live. Stream on!",
    'random_messages':          [],
    'random_interval_minutes':  10,
    'keyword_triggers':         {},
    'oauth_client_id':          '',
    'oauth_client_secret':      '',
}


def _get_bot_cfg(cfg=None):
    if cfg is None:
        cfg = _load_cfg()
    b = dict(_BOT_DEFAULTS)
    b.update(cfg.get('chat_bot', {}))
    return b


def _save_bot_cfg(bot_cfg):
    cfg = _load_cfg()
    cfg['chat_bot'] = bot_cfg
    _save_cfg(cfg)


# ── OAuth2 token helpers ──────────────────────────────────────────────────────
def _load_token():
    try:
        with open(_TOKEN_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_token(tok):
    with open(_TOKEN_PATH, 'w') as f:
        json.dump(tok, f, indent=4)


def _refresh_access_token(bot_cfg, tok):
    try:
        r = _http.post(_OAUTH_TOKEN_URL, data={
            'client_id':     bot_cfg['oauth_client_id'],
            'client_secret': bot_cfg['oauth_client_secret'],
            'refresh_token': tok['refresh_token'],
            'grant_type':    'refresh_token',
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        tok['access_token'] = data['access_token']
        tok['expires_at']   = time.time() + data.get('expires_in', 3600) - 60
        _save_token(tok)
        return tok
    except Exception as exc:
        log.error('ChatBot: token refresh failed: %s', exc)
        return None


def _get_access_token(bot_cfg):
    tok = _load_token()
    if not tok:
        return None
    if time.time() >= tok.get('expires_at', 0):
        tok = _refresh_access_token(bot_cfg, tok)
    return tok.get('access_token') if tok else None


# ── Device Authorization Flow (headless OAuth, no redirect URI needed) ────────
_dev_auth_lock  = threading.Lock()
_dev_auth_state = {
    'pending':          False,
    'user_code':        '',
    'verification_url': '',
    'expires_at':       0,
    'interval':         5,
    'error':            '',
}


def _start_device_auth(bot_cfg):
    """Request a device/user code from Google and begin background polling."""
    try:
        r = _http.post(_DEVICE_CODE_URL, data={
            'client_id': bot_cfg['oauth_client_id'],
            'scope':     _OAUTH_SCOPE,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        with _dev_auth_lock:
            _dev_auth_state.update({
                'pending':          True,
                '_device_code':     data['device_code'],
                'user_code':        data['user_code'],
                'verification_url': data.get('verification_url', 'https://www.google.com/device'),
                'expires_at':       time.time() + data.get('expires_in', 1800),
                'interval':         data.get('interval', 5),
                'error':            '',
            })
        threading.Thread(target=_poll_device_auth, args=(bot_cfg,), daemon=True).start()
        return True, data['user_code'], data.get('verification_url', 'https://www.google.com/device')
    except Exception as exc:
        log.error('ChatBot: device auth start failed: %s', exc)
        return False, '', str(exc)


def _poll_device_auth(bot_cfg):
    """Poll Google token endpoint until user approves or request expires."""
    while True:
        with _dev_auth_lock:
            if not _dev_auth_state['pending']:
                return
            if time.time() >= _dev_auth_state['expires_at']:
                _dev_auth_state['pending'] = False
                _dev_auth_state['error']   = 'Authorization timed out — start again'
                return
            device_code = _dev_auth_state['_device_code']
            interval    = _dev_auth_state['interval']

        time.sleep(interval)

        try:
            r = _http.post(_OAUTH_TOKEN_URL, data={
                'client_id':     bot_cfg['oauth_client_id'],
                'client_secret': bot_cfg['oauth_client_secret'],
                'device_code':   device_code,
                'grant_type':    'urn:ietf:params:oauth:grant-type:device_code',
            }, timeout=10)
            data = r.json()
            err = data.get('error', '')

            if err == 'authorization_pending':
                continue
            elif err == 'slow_down':
                with _dev_auth_lock:
                    _dev_auth_state['interval'] = min(interval + 5, 30)
                continue
            elif err == 'access_denied':
                with _dev_auth_lock:
                    _dev_auth_state['pending'] = False
                    _dev_auth_state['error']   = 'Access denied — try again'
                return
            elif err:
                with _dev_auth_lock:
                    _dev_auth_state['pending'] = False
                    _dev_auth_state['error']   = err
                return

            # Success
            data['expires_at'] = time.time() + data.get('expires_in', 3600) - 60
            _save_token(data)
            with _dev_auth_lock:
                _dev_auth_state['pending'] = False
                _dev_auth_state['error']   = ''
            with _bot_status_lock:
                _bot_status['authorized'] = True
            log.info('ChatBot: device auth complete — token saved')
            return

        except Exception as exc:
            log.error('ChatBot: device auth poll error: %s', exc)
            time.sleep(5)


# ── Chat Bot class ────────────────────────────────────────────────────────────
_bot_stop_evt    = threading.Event()
_bot_status_lock = threading.Lock()
_bot_status = {
    'running':       False,
    'messages_sent': 0,
    'error':         '',
    'authorized':    False,
}


class _ChatBot:
    def __init__(self, bot_cfg):
        self.cfg            = bot_cfg
        self._live_chat_id  = None
        self._page_token    = None
        self._poll_interval = 8.0
        self._last_random   = time.time()

    def _headers(self):
        tok = _get_access_token(self.cfg)
        if not tok:
            return None
        return {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}

    def _get_live_chat_id(self):
        h = self._headers()
        if not h:
            return None
        try:
            r = _http.get(f'{_YT_API}/liveBroadcasts', headers=h,
                          params={'part': 'snippet', 'broadcastStatus': 'active', 'mine': 'true'},
                          timeout=10)
            r.raise_for_status()
            items = r.json().get('items', [])
            if items:
                return items[0]['snippet']['liveChatId']
        except Exception as exc:
            log.error('ChatBot: getLiveChatId failed: %s', exc)
        return None

    def _poll(self):
        if not self._live_chat_id:
            return []
        h = self._headers()
        if not h:
            return []
        params = {'part': 'snippet,authorDetails', 'liveChatId': self._live_chat_id}
        if self._page_token:
            params['pageToken'] = self._page_token
        try:
            r = _http.get(f'{_YT_API}/liveChatMessages', headers=h, params=params, timeout=10)
            if r.status_code == 403:
                log.warning('ChatBot: 403 — quota or permission, slowing poll')
                self._poll_interval = 30.0
                return []
            r.raise_for_status()
            data = r.json()
            self._page_token    = data.get('nextPageToken')
            self._poll_interval = max(5.0, data.get('pollingIntervalMillis', 5000) / 1000.0)
            return data.get('items', [])
        except Exception as exc:
            log.error('ChatBot: poll failed: %s', exc)
        return []

    def post(self, text):
        if not self._live_chat_id or not text.strip():
            return False
        h = self._headers()
        if not h:
            return False
        try:
            r = _http.post(
                f'{_YT_API}/liveChatMessages', headers=h,
                params={'part': 'snippet'},
                json={'snippet': {
                    'liveChatId':         self._live_chat_id,
                    'type':               'textMessageEvent',
                    'textMessageDetails': {'messageText': text[:200]},
                }},
                timeout=10,
            )
            r.raise_for_status()
            with _bot_status_lock:
                _bot_status['messages_sent'] += 1
            log.info('ChatBot: sent → %s', text[:60])
            return True
        except Exception as exc:
            log.error('ChatBot: post failed: %s', exc)
        return False

    def _handle(self, item):
        try:
            text   = item['snippet']['displayMessage'].lower()
            author = item['authorDetails']['displayName']
        except (KeyError, TypeError):
            return
        for kw, resp in self.cfg.get('keyword_triggers', {}).items():
            if kw.lower() in text:
                log.info('ChatBot: keyword "%s" triggered by %s', kw, author)
                self.post(resp)
                return  # one response per message

    def run(self):
        self._poll_interval = 8.0
        log.info('ChatBot: starting')
        with _bot_status_lock:
            _bot_status['running'] = True
            _bot_status['error']   = ''

        for _ in range(18):   # retry up to ~3 min while stream initialises
            if _bot_stop_evt.is_set():
                break
            self._live_chat_id = self._get_live_chat_id()
            if self._live_chat_id:
                log.info('ChatBot: live chat found → %s…', self._live_chat_id[:20])
                break
            time.sleep(10)

        if not self._live_chat_id:
            err = 'No active broadcast found'
            log.warning('ChatBot: %s', err)
            with _bot_status_lock:
                _bot_status['error']   = err
                _bot_status['running'] = False
            return

        self._poll()  # drain existing messages so we don't replay history

        hm = self.cfg.get('headless_message', '').strip()
        if hm:
            time.sleep(5)
            self.post(hm)

        while not _bot_stop_evt.is_set():
            for item in self._poll():
                if _bot_stop_evt.is_set():
                    break
                self._handle(item)

            rand_msgs = self.cfg.get('random_messages', [])
            interval  = float(self.cfg.get('random_interval_minutes', 10)) * 60
            if rand_msgs and interval > 0 and (time.time() - self._last_random) >= interval:
                self.post(random.choice(rand_msgs))
                self._last_random = time.time()

            _bot_stop_evt.wait(self._poll_interval)

        with _bot_status_lock:
            _bot_status['running'] = False
        log.info('ChatBot: stopped')


_bot_thread = None


def start_bot():
    global _bot_thread
    if not _REQUESTS_OK:
        log.warning('ChatBot: requests library not installed')
        return
    bot_cfg = _get_bot_cfg()
    if not bot_cfg.get('enabled'):
        return
    if not _load_token():
        log.warning('ChatBot: no OAuth token — authorize via web UI first')
        return
    with _bot_status_lock:
        _bot_status['authorized']    = True
        _bot_status['messages_sent'] = 0
    _bot_stop_evt.clear()
    bot = _ChatBot(bot_cfg)
    _bot_thread = threading.Thread(target=bot.run, daemon=True)
    _bot_thread.start()


def stop_bot():
    _bot_stop_evt.set()


# ── HTML dashboard ─────────────────────────────────────────────────────────────
def _otr_options(selected_url):
    opts = []
    for name, url in _OTR_STATIONS:
        sel = ' selected' if url == selected_url else ''
        opts.append(f'<option value="{url}"{sel}>{name}</option>')
    return '\n'.join(opts)

def _camera_options(selected_idx):
    opts = []
    for i, cam in enumerate(_CAMERAS):
        sel = ' selected' if i == selected_idx else ''
        opts.append(f'<option value="{i}"{sel}>{cam["name"]} ({cam["stream_w"]}x{cam["stream_h"]}@{cam["stream_fps"]}fps)</option>')
    return '\n'.join(opts)

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Studio</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0d;color:#e0e0e0;font-family:monospace;font-size:14px}}
h1{{padding:12px 18px;background:#1a0000;color:#ff4444;font-size:18px;letter-spacing:2px;border-bottom:1px solid #333}}
h1 span{{color:#888;font-size:13px;margin-left:10px}}
.layout{{display:flex;flex-wrap:wrap;gap:16px;padding:16px}}
.panel{{background:#141414;border:1px solid #2a2a2a;border-radius:6px;padding:16px;min-width:280px;flex:1}}
h2{{font-size:13px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:14px;border-bottom:1px solid #222;padding-bottom:6px}}
label{{display:block;color:#aaa;font-size:12px;margin-bottom:4px;margin-top:10px}}
input[type=text],input[type=password],select{{width:100%;background:#1e1e1e;color:#fff;border:1px solid #3a3a3a;padding:7px 10px;border-radius:4px;font-family:monospace;font-size:13px}}
input[type=text]:focus,input[type=password]:focus,select:focus{{outline:none;border-color:#ff4444}}
.btn{{display:inline-block;padding:9px 18px;border:none;border-radius:4px;font-family:monospace;font-size:13px;cursor:pointer;margin-top:8px;margin-right:6px;transition:opacity .15s}}
.btn:hover{{opacity:.85}}
.btn-red{{background:#cc2200;color:#fff}}
.btn-green{{background:#1a6b1a;color:#fff}}
.btn-grey{{background:#333;color:#ccc}}
.btn-blue{{background:#1a4b8a;color:#fff}}
.status-bar{{padding:10px 18px;background:#111;border-top:1px solid #222;font-size:12px;color:#666}}
#status-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:#444;margin-right:6px}}
.slider-row{{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:13px}}
.slider-row label{{width:90px;color:#aaa}}
.slider-row input[type=range]{{flex:1;accent-color:#4a9eff}}
.slider-row .sv{{width:36px;text-align:right;color:#eee}}
#status-dot.live{{background:#ff2222;box-shadow:0 0 6px #ff2222}}
#status-txt{{color:#aaa}}
#uptime{{color:#80ff80;margin-left:10px}}
#retries{{color:#ff9900;margin-left:10px}}
.preview-wrap{{position:relative;background:#000;border-radius:4px;overflow:hidden;margin-top:8px}}
.preview-wrap img{{width:100%;display:block;min-height:200px}}
.preview-overlay{{position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none}}
.crosshair-line{{position:absolute;background:rgba(255,255,80,.4)}}
.preview-label{{position:absolute;top:4px;left:4px;background:rgba(0,0,0,.6);color:#ffff50;font-size:11px;padding:2px 6px;border-radius:3px}}
.hint{{font-size:11px;color:#555;margin-top:4px}}
.err{{color:#ff6666;font-size:12px;margin-top:6px}}
.toggle-wrap{{display:inline-flex;align-items:center;cursor:pointer;user-select:none}}
.toggle-wrap input{{position:absolute;opacity:0;width:0;height:0}}
.toggle-slider{{position:relative;display:inline-block;width:38px;height:20px;background:#333;border-radius:10px;transition:background .2s;flex-shrink:0}}
.toggle-slider:before{{content:'';position:absolute;width:16px;height:16px;left:2px;top:2px;background:#888;border-radius:50%;transition:transform .2s,background .2s}}
.toggle-wrap input:checked + .toggle-slider{{background:#1a4b8a}}
.toggle-wrap input:checked + .toggle-slider:before{{transform:translateX(18px);background:#4a9eff}}
.bot-dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#444;flex-shrink:0}}
.bot-dot.bot-live{{background:#ff2222;box-shadow:0 0 5px #ff2222}}
.kw-row{{display:flex;align-items:center;gap:6px;padding:3px 0;border-bottom:1px solid #1e1e1e;font-size:12px}}
.rm-row{{display:flex;align-items:center;gap:6px;padding:3px 0;border-bottom:1px solid #1e1e1e;font-size:12px}}
.rm-btn{{background:none;border:none;color:#cc4444;cursor:pointer;font-size:14px;padding:0 4px;line-height:1}}
details>summary{{cursor:pointer;color:#666;font-size:11px;text-transform:uppercase;letter-spacing:1px;outline:none;list-style:none}}
details>summary::-webkit-details-marker{{display:none}}
details[open]>summary{{color:#aaa}}
</style>
</head>
<body>
<h1>&#9654; YouTube Studio <span>Pi 4 · {ip}:{port}</span></h1>
<div class="layout">

  <!-- Left: controls -->
  <div class="panel" style="max-width:360px">
    <h2>&#127909; Stream Control</h2>
    <div id="stream-status" style="padding:8px;background:#1a1a1a;border-radius:4px;margin-bottom:10px">
      <span id="status-dot"></span><span id="status-txt">Idle</span>
      <span id="uptime"></span><span id="retries"></span>
    </div>

    <label>Camera</label>
    <select id="cam-sel">{cam_opts}</select>

    <label>Audio (OTR Radio)</label>
    <select id="otr-sel">{otr_opts}</select>

    <div style="margin-top:12px">
      <button class="btn btn-green" onclick="startStream()">&#9654; Start Stream</button>
      <button class="btn btn-red"   onclick="stopStream()">&#9632; Stop Stream</button>
    </div>
    <div id="stream-err" class="err"></div>

    <hr style="border:none;border-top:1px solid #222;margin:16px 0">

    <h2>&#9881; Settings</h2>
    <label>YouTube Stream Key</label>
    <input type="password" id="stream-key" value="{stream_key}" placeholder="Paste key from YouTube Studio">
    <div class="hint">YouTube Studio → Go Live → Stream Key</div>

    <div style="margin-top:10px">
      <button class="btn btn-grey" onclick="saveSettings()">&#128190; Save Settings</button>
    </div>
    <div id="save-msg" style="font-size:12px;color:#80ff80;margin-top:6px"></div>
  </div>

   <!-- Right: live preview -->
  <div class="panel" style="flex:2;min-width:300px">
    <h2>&#128247; Focus Preview (live · ~5 fps)</h2>
    <div class="hint" style="margin-bottom:8px">
      Adjust your lens while watching this feed. Switch camera above to refresh preview.
    </div>
    <div class="preview-wrap">
      <img id="prev-img" src="/preview?cam=0" alt="Loading preview…">
      <div class="preview-overlay" id="prev-overlay">
        <!-- Rule-of-thirds lines injected by JS -->
      </div>
      <div class="preview-label" id="prev-label">USB-CAM · 5fps</div>
    </div>
    <div style="margin-top:8px">
      <button class="btn btn-grey" onclick="switchPreview()">&#8635; Refresh Preview</button>
      <button class="btn btn-grey" onclick="toggleGrid()">&#9638; Grid</button>
    </div>

    <!-- Quality Controls -->
    <div style="margin-top:16px;border-top:1px solid #222;padding-top:12px">
      <h2 style="margin-top:0">&#127922; Quality Controls</h2>
      <div id="slider-wrap">
        {sliders_html}
      </div>
      <button class="btn btn-blue" onclick="applyQuality()" style="margin-top:8px">&#10003; Apply to Preview</button>
    </div>
  </div>

</div>

{bot_panel_html}

<div class="status-bar" id="footer">
  YouTube Studio · http://{ip}:{port} · use /status for JSON
</div>

<script>
var camSel  = document.getElementById('cam-sel');
var otrSel  = document.getElementById('otr-sel');
var prevImg = document.getElementById('prev-img');
var prevLbl = document.getElementById('prev-label');
var overlay = document.getElementById('prev-overlay');
var gridOn  = false;
var camNames = {cam_names_json};

function switchPreview() {{
  var idx = camSel.value;
  prevImg.src = '/preview?cam=' + idx + '&t=' + Date.now();
  prevLbl.textContent = camNames[idx] + ' · 5fps';
}}

camSel.addEventListener('change', switchPreview);

function toggleGrid() {{
  gridOn = !gridOn;
  overlay.innerHTML = '';
  if (!gridOn) return;
  var styles = [
    'left:33%;top:0;width:1px;height:100%',
    'left:66%;top:0;width:1px;height:100%',
    'left:0;top:33%;width:100%;height:1px',
    'left:0;top:66%;width:100%;height:1px',
  ];
  styles.forEach(function(s) {{
    var d = document.createElement('div');
    d.className = 'crosshair-line';
    d.style.cssText = s;
    overlay.appendChild(d);
  }});
}}

function startStream() {{
  var idx = camSel.value;
  var otr = otrSel.value;
  document.getElementById('stream-err').textContent = '';
  fetch('/start', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
    body:'cam_idx='+idx+'&otr_url='+encodeURIComponent(otr)}})
  .then(r=>r.json()).then(d=>{{
    if (!d.ok) document.getElementById('stream-err').textContent = d.msg;
  }});
}}

function stopStream() {{
  fetch('/stop', {{method:'POST'}});
}}

function saveSettings() {{
  var key = document.getElementById('stream-key').value;
  var otr = otrSel.value;
  fetch('/settings', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
    body:'youtube_stream_key='+encodeURIComponent(key)+'&otr_station_url='+encodeURIComponent(otr)}})
  .then(r=>r.json()).then(d=>{{
    var el = document.getElementById('save-msg');
    el.textContent = d.ok ? '✓ Saved' : '✗ ' + d.msg;
    setTimeout(function(){{el.textContent=''}}, 3000);
  }});
}}

function updateStatus() {{
  fetch('/status').then(r=>r.json()).then(function(s) {{
    var dot = document.getElementById('status-dot');
    var txt = document.getElementById('status-txt');
    var upt = document.getElementById('uptime');
    var ret = document.getElementById('retries');
    if (s.running) {{
      dot.className = 'live';
      txt.textContent = '● LIVE  ' + s.cam_name;
      var u = s.uptime_s;
      upt.textContent = '  ' + String(Math.floor(u/3600)).padStart(2,'0') + ':'
        + String(Math.floor((u%3600)/60)).padStart(2,'0') + ':'
        + String(u%60).padStart(2,'0');
      ret.textContent = s.retries > 0 ? '  retries:'+s.retries : '';
    }} else {{
      dot.className = '';
      txt.textContent = s.error ? '✗ ' + s.error : 'Idle';
      upt.textContent = '';
      ret.textContent = '';
    }}
    // Auto-select best available camera on first load
    if (!window._camInitDone && s.available_cams && s.available_cams.length > 0) {{
      window._camInitDone = true;
      var best = s.available_cams[0];
      camSel.value = String(best);
      switchPreview();
    }}
    // Populate sliders from status on first load (don't overwrite if user changed them)
    if (!window._qualInitDone && s.quality) {{
      window._qualInitDone = true;
      var q = s.quality;
      setSlider('brightness', Math.round(q.brightness * 100));
      setSlider('contrast',   Math.round(q.contrast   * 100));
      setSlider('saturation', Math.round(q.saturation * 100));
      setSlider('sharpness',  Math.round(q.sharpness  * 100));
      setSlider('zoom',       Math.round(q.zoom       * 10));
    }}
    document.getElementById('footer').textContent =
      'YouTube Studio · http://{ip}:{port}' + (s.running ? '  ·  streaming to YouTube' : '');
  }}).catch(function(){{}});
}}

setInterval(updateStatus, 2000);
updateStatus();

function setSlider(id, val) {{
  var el = document.getElementById('sl-' + id);
  if (el) {{ el.value = val; el.dispatchEvent(new Event('input')); }}
}}

function applyQuality() {{
  var b  = parseInt(document.getElementById('sl-brightness').value);
  var c  = parseInt(document.getElementById('sl-contrast').value);
  var sa = parseInt(document.getElementById('sl-saturation').value);
  var sh = parseInt(document.getElementById('sl-sharpness').value);
  var z  = parseInt(document.getElementById('sl-zoom').value);
  fetch('/quality', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
    body: 'brightness='+(b/100)+'&contrast='+(c/100)+'&saturation='+(sa/100)+'&sharpness='+(sh/100)+'&zoom='+(z/10)
  }}).then(r=>r.json()).then(d=>{{
    if (!d.ok) alert('Quality error: ' + d.msg);
  }});
}}
{bot_panel_js}
</script>
</body>
</html>"""


def _qual_slider_html(id_, label, mn, mx, default):
    """Render a single quality slider row as HTML."""
    return (
        f'<div class="slider-row">'
        f'<label>{label}</label>'
        f'<input type="range" id="sl-{id_}" min="{mn}" max="{mx}" value="{default}" '
        f'oninput="document.getElementById(\'sv-{id_}\').textContent=this.value">'
        f'<span class="sv" id="sv-{id_}">{default}</span>'
        f'</div>'
    )


def _build_bot_panel_html(bot_cfg, auth_status_html, redirect_uri):
    checked       = 'checked' if bot_cfg.get('enabled') else ''
    client_id     = html.escape(bot_cfg.get('oauth_client_id', ''))
    headless_msg  = html.escape(bot_cfg.get('headless_message', ''))
    rand_interval = int(bot_cfg.get('random_interval_minutes', 10))
    return f"""
<div class="layout" style="padding-top:0">
  <div class="panel" style="flex:1;min-width:100%">
    <h2>&#129302; YouTube Live Chat Bot</h2>
    <div style="display:flex;align-items:center;flex-wrap:wrap;gap:14px;margin-bottom:14px">
      <label class="toggle-wrap">
        <input type="checkbox" id="bot-enabled" {checked}>
        <span class="toggle-slider"></span>
        <span style="margin-left:8px;color:#ccc;font-size:13px">Enable Bot</span>
      </label>
      <span class="bot-dot" id="bot-dot"></span>
      <span id="bot-status-txt" style="color:#888;font-size:12px">Not running</span>
      <span id="bot-msgs-sent" style="color:#4a9eff;font-size:12px;margin-left:6px"></span>
    </div>

    <div style="display:flex;flex-wrap:wrap;gap:16px">

      <!-- OAuth + headless message + interval -->
      <div style="flex:1;min-width:260px">
        <details id="oauth-section">
          <summary>&#9658; Google API Setup</summary>
          <div style="margin-top:10px;padding:10px;background:#0d0d0d;border-radius:4px">
            <div class="hint" style="margin-bottom:8px;line-height:1.8">
              1. <a href="https://console.cloud.google.com/apis/credentials" target="_blank" style="color:#4a9eff">Google Cloud Console</a>
              &rarr; Create OAuth 2.0 Client ID<br>
              &nbsp;&nbsp;&nbsp;&nbsp;&#9656; Credentials type: <strong style="color:#ffcc44">TV and Limited Input devices</strong><br>
              2. Enable <strong style="color:#ccc">YouTube Data API v3</strong> on the same project<br>
              3. Paste Client ID &amp; Secret below, click Save, then Start Authorization<br>
              4. On <strong>any phone or laptop</strong>, go to the URL shown and enter the code
            </div>
            <label>Client ID</label>
            <input type="text" id="bot-client-id" value="{client_id}" placeholder="...apps.googleusercontent.com">
            <label style="margin-top:8px">Client Secret</label>
            <input type="password" id="bot-client-secret" placeholder="leave blank to keep existing">
            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
              <button class="btn btn-grey" onclick="saveBotSettings()" style="margin:0">&#128190; Save Credentials</button>
              <button class="btn btn-blue" id="auth-btn" onclick="startDeviceAuth()" style="margin:0">&#128273; Start Authorization</button>
            </div>
            <!-- Device auth code display (hidden until auth starts) -->
            <div id="device-auth-box" style="display:none;margin-top:12px;padding:12px;background:#1a2a1a;border:1px solid #2a5a2a;border-radius:4px;text-align:center">
              <div style="color:#888;font-size:11px;margin-bottom:6px">Go to this URL on any device:</div>
              <a id="dev-verify-url" href="#" target="_blank" style="color:#4a9eff;font-size:13px"></a>
              <div style="margin:10px 0;color:#888;font-size:11px">Enter this code:</div>
              <div id="dev-user-code" style="font-size:28px;font-weight:bold;letter-spacing:6px;color:#80ff80;font-family:monospace"></div>
              <div id="dev-auth-waiting" style="margin-top:8px;color:#888;font-size:11px">&#9203; Waiting for you to approve&hellip;</div>
            </div>
            <div id="auth-status" style="margin-top:8px;font-size:12px">{auth_status_html}</div>
          </div>
        </details>

        <label style="margin-top:14px">Headless Message</label>
        <div class="hint">Posted to chat when your stream goes live</div>
        <textarea id="bot-headless" rows="3" style="width:100%;margin-top:4px;background:#1e1e1e;color:#fff;border:1px solid #3a3a3a;padding:7px;border-radius:4px;font-family:monospace;font-size:12px;resize:vertical">{headless_msg}</textarea>

        <label style="margin-top:10px">Random Message Interval</label>
        <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
          <input type="number" id="bot-interval" value="{rand_interval}" min="0" max="120"
            style="width:70px;background:#1e1e1e;color:#fff;border:1px solid #3a3a3a;padding:7px;border-radius:4px;font-family:monospace;font-size:13px">
          <span class="hint" style="margin:0">minutes &nbsp;(0 = disabled)</span>
        </div>
      </div>

      <!-- Random messages -->
      <div style="flex:1;min-width:220px">
        <label>Random Messages</label>
        <div class="hint">One is picked at random every N minutes while live</div>
        <div id="rand-msg-list" style="margin-top:6px;max-height:200px;overflow-y:auto"></div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <input type="text" id="new-rand-msg" placeholder="Add a message…" style="flex:1;font-size:12px"
            onkeydown="if(event.key==='Enter')addRandMsg()">
          <button class="btn btn-grey" onclick="addRandMsg()" style="margin:0;padding:6px 12px">+ Add</button>
        </div>
      </div>

      <!-- Keyword triggers -->
      <div style="flex:1;min-width:220px">
        <label>Keyword Triggers</label>
        <div class="hint">Bot replies when it spots a keyword in any message</div>
        <div id="kw-list" style="margin-top:6px;max-height:200px;overflow-y:auto"></div>
        <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
          <input type="text" id="new-kw"   placeholder="keyword"  style="flex:1;min-width:70px;font-size:12px">
          <input type="text" id="new-resp" placeholder="response" style="flex:2;min-width:110px;font-size:12px"
            onkeydown="if(event.key==='Enter')addKeyword()">
          <button class="btn btn-grey" onclick="addKeyword()" style="margin:0;padding:6px 12px">+ Add</button>
        </div>
      </div>

    </div>

    <div style="margin-top:14px;display:flex;align-items:center;flex-wrap:wrap;gap:10px">
      <button class="btn btn-blue" onclick="saveBotSettings()">&#128190; Save Bot Settings</button>
      <span id="bot-save-msg" style="font-size:12px;color:#80ff80"></span>
    </div>
  </div>
</div>"""


def _build_bot_panel_js(bot_cfg):
    msgs_json = json.dumps(bot_cfg.get('random_messages', []))
    kw_json   = json.dumps(bot_cfg.get('keyword_triggers', {}))
    return f"""
// ── Chat Bot ─────────────────────────────────────────────────────────────────
var _randMsgs   = {msgs_json};
var _kwTriggers = {kw_json};

function _escH(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function renderRandMsgs() {{
  var el = document.getElementById('rand-msg-list');
  if (!el) return;
  el.innerHTML = '';
  _randMsgs.forEach(function(msg, i) {{
    var d = document.createElement('div');
    d.className = 'rm-row';
    d.innerHTML = '<span style="flex:1;color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _escH(msg) + '</span>'
      + '<button class="rm-btn" onclick="removeRandMsg(' + i + ')">&#10005;</button>';
    el.appendChild(d);
  }});
}}

function renderKeywords() {{
  var el = document.getElementById('kw-list');
  if (!el) return;
  el.innerHTML = '';
  Object.keys(_kwTriggers).forEach(function(kw) {{
    var d = document.createElement('div');
    d.className = 'kw-row';
    d.innerHTML = '<span style="min-width:70px;color:#ffcc44;overflow:hidden;text-overflow:ellipsis">' + _escH(kw) + '</span>'
      + '<span style="color:#888;padding:0 4px">&#8594;</span>'
      + '<span style="flex:1;color:#ccc;overflow:hidden;text-overflow:ellipsis">' + _escH(_kwTriggers[kw]) + '</span>'
      + '<button class="rm-btn" onclick="removeKeyword(' + JSON.stringify(kw) + ')">&#10005;</button>';
    el.appendChild(d);
  }});
}}

function addRandMsg() {{
  var v = document.getElementById('new-rand-msg').value.trim();
  if (!v) return;
  _randMsgs.push(v);
  document.getElementById('new-rand-msg').value = '';
  renderRandMsgs();
}}

function removeRandMsg(i) {{
  _randMsgs.splice(i, 1);
  renderRandMsgs();
}}

function addKeyword() {{
  var kw   = document.getElementById('new-kw').value.trim();
  var resp = document.getElementById('new-resp').value.trim();
  if (!kw || !resp) return;
  _kwTriggers[kw] = resp;
  document.getElementById('new-kw').value   = '';
  document.getElementById('new-resp').value = '';
  renderKeywords();
}}

function removeKeyword(kw) {{
  delete _kwTriggers[kw];
  renderKeywords();
}}

function startDeviceAuth() {{
  saveBotSettings(function() {{
    fetch('/bot_auth_start', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body:''}})
    .then(r => r.json()).then(function(d) {{
      if (!d.ok) {{
        document.getElementById('auth-status').innerHTML = '<span style="color:#ff6666">&#10005; ' + _escH(d.msg) + '</span>';
        return;
      }}
      var box = document.getElementById('device-auth-box');
      var urlEl = document.getElementById('dev-verify-url');
      var codeEl = document.getElementById('dev-user-code');
      urlEl.textContent = d.verification_url;
      urlEl.href        = d.verification_url;
      codeEl.textContent = d.user_code;
      box.style.display = 'block';
      document.getElementById('auth-status').innerHTML = '';
    }});
  }});
}}

function saveBotSettings(cb) {{
  var data = {{
    enabled:                 document.getElementById('bot-enabled').checked ? '1' : '0',
    oauth_client_id:         document.getElementById('bot-client-id').value,
    oauth_client_secret:     document.getElementById('bot-client-secret').value,
    headless_message:        document.getElementById('bot-headless').value,
    random_interval_minutes: document.getElementById('bot-interval').value,
    random_messages:         JSON.stringify(_randMsgs),
    keyword_triggers:        JSON.stringify(_kwTriggers),
  }};
  var body = Object.keys(data).map(function(k) {{
    return encodeURIComponent(k) + '=' + encodeURIComponent(data[k]);
  }}).join('&');
  fetch('/bot_config', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body:body}})
  .then(r => r.json()).then(function(d) {{
    var el = document.getElementById('bot-save-msg');
    el.textContent = d.ok ? '\\u2713 Saved' : '\\u2717 ' + d.msg;
    setTimeout(function() {{ el.textContent = ''; }}, 3000);
    if (cb) cb();
  }});
}}

function updateBotStatus() {{
  fetch('/bot_status').then(r => r.json()).then(function(b) {{
    var dot  = document.getElementById('bot-dot');
    var txt  = document.getElementById('bot-status-txt');
    var mEl  = document.getElementById('bot-msgs-sent');
    var aEl  = document.getElementById('auth-status');
    var box  = document.getElementById('device-auth-box');
    var wait = document.getElementById('dev-auth-waiting');

    if (b.running) {{
      dot.className   = 'bot-dot bot-live';
      txt.textContent = '\\u25cf Bot active';
      mEl.textContent = b.messages_sent > 0 ? b.messages_sent + ' msgs sent' : '';
    }} else {{
      dot.className   = 'bot-dot';
      txt.textContent = b.error ? '\\u2717 ' + b.error
                      : (b.authorized ? 'Authorized \\u00b7 starts when stream goes live' : 'Not authorized');
      mEl.textContent = '';
    }}

    // Device auth polling
    if (b.dev_auth) {{
      if (b.dev_auth.pending && box) {{
        box.style.display = 'block';
        if (wait) wait.textContent = '\\u23f3 Waiting for you to approve\\u2026';
      }} else if (!b.dev_auth.pending && box && box.style.display !== 'none') {{
        if (b.dev_auth.error) {{
          if (wait) wait.innerHTML = '<span style="color:#ff6666">\\u2717 ' + _escH(b.dev_auth.error) + '</span>';
        }} else {{
          box.style.display = 'none';
          if (aEl) aEl.innerHTML = '<span style="color:#80ff80">\\u2713 Authorized!</span>';
        }}
      }}
    }}

    if (b.authorized && aEl && !b.dev_auth.pending) {{
      aEl.innerHTML = '<span style="color:#80ff80">\\u2713 Authorized</span>';
    }}
  }}).catch(function() {{}});
}}

setInterval(updateBotStatus, 4000);
renderRandMsgs();
renderKeywords();
updateBotStatus();"""


def _render_dashboard():
    cfg        = _load_cfg()
    ip         = _get_local_ip()
    stream_key = cfg.get('youtube_stream_key', '')
    otr_url    = cfg.get('otr_station_url', _OTR_DEFAULT)
    cam_names  = json.dumps({str(i): c['short'] for i, c in enumerate(_CAMERAS)})
    q          = _get_quality(cfg)
    sliders_html = ''.join([
        _qual_slider_html('brightness', 'Brightness', -100, 100, round(q['brightness'] * 100)),
        _qual_slider_html('contrast',   'Contrast',     0, 200, round(q['contrast']   * 100)),
        _qual_slider_html('saturation', 'Saturation',   0, 200, round(q['saturation'] * 100)),
        _qual_slider_html('sharpness',  'Sharpness',    0, 200, round(q['sharpness']  * 100)),
        _qual_slider_html('zoom',       'Zoom (x)',     10,  40, round(q['zoom']       * 10)),
    ])
    bot_cfg          = _get_bot_cfg(cfg)
    authorized       = bool(_load_token())
    auth_status_html = (
        '<span style="color:#80ff80">&#10003; Authorized</span>' if authorized
        else '<span style="color:#888">Not yet authorized</span>'
    )
    bot_panel_html = _build_bot_panel_html(bot_cfg, auth_status_html, '')
    bot_panel_js   = _build_bot_panel_js(bot_cfg)
    return _DASHBOARD_HTML.format(
        ip=ip, port=_PORT,
        stream_key=stream_key,
        cam_opts=_camera_options(0),
        otr_opts=_otr_options(otr_url),
        cam_names_json=cam_names,
        sliders_html=sliders_html,
        bot_panel_html=bot_panel_html,
        bot_panel_js=bot_panel_js,
    ).encode()


# ── HTTP handler ───────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log; we use our own

    # ── routing ──────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            self._serve_dashboard()
        elif path == '/preview':
            self._serve_preview()
        elif path == '/status':
            self._serve_status()
        elif path == '/bot_status':
            with _bot_status_lock:
                bs = dict(_bot_status)
            bs['authorized'] = bool(_load_token())
            with _dev_auth_lock:
                bs['dev_auth'] = {
                    'pending':          _dev_auth_state['pending'],
                    'user_code':        _dev_auth_state['user_code'],
                    'verification_url': _dev_auth_state['verification_url'],
                    'error':            _dev_auth_state['error'],
                }
            self._json(bs)
        elif path == '/oauth2callback':
            # No longer used — kept so old bookmarks don't 404
            self._send(200,
                b'<html><body style="background:#0d0d0d;color:#aaa;font-family:monospace;padding:40px;text-align:center">'
                b'<h1>Redirect URI not needed</h1>'
                b'<p>This project now uses the Device Authorization flow.</p>'
                b'<p>Return to YouTube Studio and use the "Start Authorization" button.</p>'
                b'</body></html>',
                'text/html; charset=utf-8')
        else:
            self._send(404, b'Not found', 'text/plain')

    def do_POST(self):
        path   = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length).decode()
        params = parse_qs(body)

        def get(k, d=''):
            v = params.get(k, [d])
            return unquote_plus(v[0]) if v else d

        if path == '/start':
            try:
                cam_idx = int(get('cam_idx', '0'))
            except ValueError:
                cam_idx = 0
            ok, msg = start_stream(cam_idx)
            self._json({'ok': ok, 'msg': msg})

        elif path == '/stop':
            stop_stream()
            self._json({'ok': True, 'msg': 'Stopping…'})

        elif path == '/quality':
            cfg = _load_cfg()
            q   = _get_quality(cfg)
            try:
                q['brightness'] = float(get('brightness', q['brightness']))
                q['contrast']   = float(get('contrast',   q['contrast']))
                q['saturation'] = float(get('saturation', q['saturation']))
                q['sharpness']  = float(get('sharpness',  q['sharpness']))
                q['zoom']       = float(get('zoom',       q['zoom']))
            except ValueError as exc:
                self._json({'ok': False, 'msg': str(exc)})
                return
            cfg['quality'] = _get_quality({'quality': q})  # clamp
            _save_cfg(cfg)
            # Restart preview with new quality so sliders update the live feed
            if _prev_cam_name:
                idx = next((i for i, c in enumerate(_CAMERAS)
                            if c['name'] == _prev_cam_name), 0)
                cam = dict(_CAMERAS[idx])
                if cam['type'] == 'usb':
                    cam['device'] = _find_usb_video_device()
                start_preview(cam)
            log.info('Quality updated → %s', cfg['quality'])
            self._json({'ok': True, 'quality': cfg['quality']})

        elif path == '/bot_auth_start':
            bot_cfg = _get_bot_cfg()
            if not bot_cfg.get('oauth_client_id') or not bot_cfg.get('oauth_client_secret'):
                self._json({'ok': False, 'msg': 'Save Client ID and Secret first'})
                return
            ok, user_code, verify_url = _start_device_auth(bot_cfg)
            if ok:
                self._json({'ok': True, 'user_code': user_code, 'verification_url': verify_url})
            else:
                self._json({'ok': False, 'msg': user_code or verify_url})

        elif path == '/bot_config':
            bot_cfg = _get_bot_cfg()
            bot_cfg['enabled']                 = get('enabled') == '1'
            new_id  = get('oauth_client_id').strip()
            new_sec = get('oauth_client_secret').strip()
            if new_id:
                bot_cfg['oauth_client_id']     = new_id
            if new_sec:
                bot_cfg['oauth_client_secret'] = new_sec
            bot_cfg['headless_message']        = get('headless_message')
            try:
                bot_cfg['random_interval_minutes'] = max(0, int(float(get('random_interval_minutes', '10'))))
            except ValueError:
                pass
            try:
                bot_cfg['random_messages']  = json.loads(get('random_messages', '[]'))
            except (ValueError, json.JSONDecodeError):
                pass
            try:
                bot_cfg['keyword_triggers'] = json.loads(get('keyword_triggers', '{}'))
            except (ValueError, json.JSONDecodeError):
                pass
            _save_bot_cfg(bot_cfg)
            log.info('Bot config saved (enabled=%s)', bot_cfg['enabled'])
            self._json({'ok': True})

        elif path == '/settings':
            cfg = _load_cfg()
            cfg['youtube_stream_key'] = get('youtube_stream_key').strip()
            cfg['otr_station_url']    = get('otr_station_url').strip() or _OTR_DEFAULT
            try:
                _save_cfg(cfg)
                log.info('Settings saved')
                self._json({'ok': True})
            except Exception as exc:
                self._json({'ok': False, 'msg': str(exc)})
        else:
            self._send(404, b'Not found', 'text/plain')

    # ── endpoints ─────────────────────────────────────────────────────────────
    def _serve_dashboard(self):
        body = _render_dashboard()
        self._send(200, body, 'text/html; charset=utf-8')

    def _serve_preview(self):
        """Stream MJPEG to the browser. Starts preview process if needed."""
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        qp = parse_qs(qs)
        try:
            cam_idx = int(qp.get('cam', ['-1'])[0])
        except ValueError:
            cam_idx = -1

        # If no explicit cam requested, use whatever is already running;
        # fallback to first available camera.
        if cam_idx == -1:
            if _prev_cam_name:
                cam_idx = next(
                    (i for i, c in enumerate(_CAMERAS) if c['name'] == _prev_cam_name), 0
                )
            elif _available_cams:
                cam_idx = _available_cams[0]
            else:
                cam_idx = 0

        cam_idx = max(0, min(cam_idx, len(_CAMERAS) - 1))
        cam = dict(_CAMERAS[cam_idx])
        if cam['type'] == 'usb':
            cam['device'] = _find_usb_video_device()

        # Only restart preview if switching to a different camera
        if _prev_cam_name != cam['name'] or _prev_proc is None:
            start_preview(cam)

        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()

        log.info('Preview client connected → %s', cam['name'])
        try:
            while True:
                with _prev_lock:
                    frame = _prev_frame
                if frame:
                    try:
                        self.wfile.write(
                            b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                            + frame + b'\r\n'
                        )
                        self.wfile.flush()
                    except Exception:
                        break
                else:
                    time.sleep(0.05)
        except Exception:
            pass
        log.info('Preview client disconnected')

    def _serve_status(self):
        with _stream_lock:
            s = dict(_stream_state)
        uptime = 0
        if s['start_time']:
            uptime = int(time.monotonic() - s['start_time'])
        self._json({
            'running':        s['running'],
            'cam_name':       s['cam_name'],
            'uptime_s':       uptime,
            'retries':        s['retries'],
            'error':          s['error'],
            'available_cams': _available_cams,
            'preview_cam':    _prev_cam_name or '',
            'quality':        _get_quality(_load_cfg()),
        })

    # ── helpers ───────────────────────────────────────────────────────────────
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self._send(200, body, 'application/json')


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    global _available_cams
    ip = _get_local_ip()
    log.info('YouTube Studio starting on http://%s:%d', ip, _PORT)

    _available_cams = _detect_cameras()
    log.info('Available cameras: %s', [_CAMERAS[i]['name'] for i in _available_cams])

    # Auto-start preview for best available camera (HQ preferred over USB)
    if _available_cams:
        best_idx = _available_cams[0]   # HQ=1 sorts before USB=0? No — prefer CSI
        for idx in _available_cams:
            if _CAMERAS[idx]['type'] == 'csi':
                best_idx = idx
                break
        cam = dict(_CAMERAS[best_idx])
        start_preview(cam)
        log.info('Auto-started preview → %s', cam['name'])
    else:
        log.info('No cameras detected at boot — preview will start on browser connect')

    httpd = ThreadingHTTPServer(('0.0.0.0', _PORT), _Handler)
    log.info('Listening — open http://%s:%d in your browser', ip, _PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info('Shutting down')
        stop_preview()
        stop_stream()


if __name__ == '__main__':
    main()
