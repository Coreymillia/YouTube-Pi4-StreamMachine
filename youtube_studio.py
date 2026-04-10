#!/usr/bin/env python3
"""
YouTube Studio — Raspberry Pi 4 YouTube live streaming daemon.

No LCD HAT, no physical buttons, no GPIO required.
Control via web browser on any device on the LAN.

Web UI:  http://<pi-ip>:8090/
Preview: http://<pi-ip>:8090/preview   (live MJPEG ~5 fps, use for focus)
Status:  http://<pi-ip>:8090/status    (JSON)
"""

import os, sys, time, subprocess, threading, json, socket, logging
import collections
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote_plus

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

def _preview_worker(cam):
    global _prev_frame, _prev_proc, _prev_cam_name
    log.info('Preview starting → %s', cam['name'])
    _prev_cam_name = cam['name']

    pw, ph, pfps = cam['prev_w'], cam['prev_h'], cam['prev_fps']

    if cam['type'] == 'usb':
        dev = cam.get('device') or _find_usb_video_device()
        cmd = [
            'ffmpeg', '-loglevel', 'quiet',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', f'{pw}x{ph}', '-framerate', str(pfps),
            '-i', dev,
            '-f', 'mjpeg', '-q:v', '5',
            'pipe:1',
        ]
    else:  # csi
        cmd = [
            'rpicam-vid', '-t', '0',
            '--codec', 'mjpeg',
            '--width', str(pw), '--height', str(ph),
            '--framerate', str(pfps),
            '--nopreview', '-o', '-',
        ]

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
    _prev_thread = threading.Thread(target=_preview_worker, args=(cam,), daemon=True)
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

def _build_stream_cmds(cam, rtmp_url, otr_url):
    w, h, fps = cam['stream_w'], cam['stream_h'], cam['stream_fps']
    if cam['type'] == 'usb':
        dev = cam.get('device') or _find_usb_video_device()
        subprocess.run(
            ['v4l2-ctl', '-d', dev, '--set-ctrl=h264_i_frame_period=60'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        libcam_cmd = None
        ffmpeg_cmd = [
            'ffmpeg', '-loglevel', 'warning',
            '-f', 'v4l2', '-input_format', 'h264',
            '-video_size', f'{w}x{h}', '-framerate', str(fps),
            '-i', dev,
            '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
            '-map', '0:v', '-map', '1:a',
            '-c:v', 'copy',
            '-b:v', '2500k',
            '-c:a', 'aac', '-b:a', '128k',
            '-f', 'flv', rtmp_url,
        ]
    else:  # csi
        libcam_cmd = [
            'rpicam-vid', '-t', '0',
            '--codec', 'h264',
            '--profile', 'high', '--level', '4.1',
            '--width', str(w), '--height', str(h),
            '--framerate', str(fps),
            '--bitrate', '4000000',
            '--intra', str(fps * 2),
            '--inline', '--nopreview',
            '-o', '-',
        ]
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

def _stream_worker(cam, rtmp_url, otr_url):
    global _stream_proc_main, _stream_proc_libcam, _stream_state

    MAX_RETRIES = 5
    retries = 0

    def _launch():
        libcam_cmd, ffmpeg_cmd = _build_stream_cmds(cam, rtmp_url, otr_url)
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
    rtmp_url = f'{_YT_RTMP_BASE}/{stream_key}'

    _stream_stop_evt.clear()
    _stream_thread = threading.Thread(
        target=_stream_worker, args=(cam, rtmp_url, otr_url), daemon=True
    )
    _stream_thread.start()
    return True, 'Stream starting…'

def stop_stream():
    _stream_stop_evt.set()
    with _stream_lock:
        _terminate(_stream_proc_libcam)
        _terminate(_stream_proc_main)

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
.status-bar{{padding:10px 18px;background:#111;border-top:1px solid #222;font-size:12px;color:#666}}
#status-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:#444;margin-right:6px}}
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
  </div>

</div>

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
    document.getElementById('footer').textContent =
      'YouTube Studio · http://{ip}:{port}' + (s.running ? '  ·  streaming to YouTube' : '');
  }}).catch(function(){{}});
}}

setInterval(updateStatus, 2000);
updateStatus();
</script>
</body>
</html>"""


def _render_dashboard():
    cfg        = _load_cfg()
    ip         = _get_local_ip()
    stream_key = cfg.get('youtube_stream_key', '')
    otr_url    = cfg.get('otr_station_url', _OTR_DEFAULT)
    cam_names  = json.dumps({str(i): c['short'] for i, c in enumerate(_CAMERAS)})
    return _DASHBOARD_HTML.format(
        ip=ip, port=_PORT,
        stream_key=stream_key,
        cam_opts=_camera_options(0),
        otr_opts=_otr_options(otr_url),
        cam_names_json=cam_names,
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
