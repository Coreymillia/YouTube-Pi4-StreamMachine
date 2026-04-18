#!/usr/bin/env python3
"""
YouTube Companion — Pi Zero 2 W helper for YouTube-side live status.

Provides a tiny local web UI + JSON API that:
- performs Google device-flow OAuth with readonly YouTube scope
- polls the authenticated channel's live broadcast/stream status
- exposes broadcast lifecycle, stream status, and health issues
"""

import html
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('yt-companion')

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PROJECT_DIR, 'config.json')
_TOKEN_PATH = os.path.join(_PROJECT_DIR, 'token.json')
_DEVICE_CODE_URL = 'https://oauth2.googleapis.com/device/code'
_OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
_YT_API = 'https://www.googleapis.com/youtube/v3'
_YT_ANALYTICS_API = 'https://youtubeanalytics.googleapis.com/v2'

_DEFAULTS = {
    'listen_host': '0.0.0.0',
    'listen_port': 8091,
    'oauth_client_id': '',
    'oauth_client_secret': '',
    'poll_interval_seconds': 15,
    'streamer_status_host': '192.168.0.123',
    'streamer_status_port': 8090,
    'streamer_control_token': '',
}

_OAUTH_SCOPE = 'https://www.googleapis.com/auth/youtube.readonly'


def _load_cfg():
    cfg = dict(_DEFAULTS)
    try:
        with open(_CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    cfg['listen_host'] = str(cfg.get('listen_host', '0.0.0.0')).strip() or '0.0.0.0'
    cfg['listen_port'] = int(cfg.get('listen_port', 8091))
    cfg['poll_interval_seconds'] = max(5, int(cfg.get('poll_interval_seconds', 15)))
    cfg['streamer_status_host'] = str(cfg.get('streamer_status_host', '192.168.0.123')).strip()
    cfg['streamer_status_port'] = max(1, int(cfg.get('streamer_status_port', 8090)))
    cfg['streamer_control_token'] = str(cfg.get('streamer_control_token', '')).strip()[:120]
    cfg['oauth_client_id'] = str(cfg.get('oauth_client_id', '')).strip()
    cfg['oauth_client_secret'] = str(cfg.get('oauth_client_secret', '')).strip()
    return cfg


def _save_cfg(cfg):
    with open(_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=4)


def _load_token():
    try:
        with open(_TOKEN_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _save_token(tok):
    with open(_TOKEN_PATH, 'w') as f:
        json.dump(tok, f, indent=4)


def _delete_token():
    try:
        os.remove(_TOKEN_PATH)
    except FileNotFoundError:
        pass


def _http_json(url, *, method='GET', params=None, data=None, headers=None, timeout=10):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    body = None
    req_headers = dict(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        req_headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _refresh_access_token(cfg, tok):
    try:
        data = _http_json(
            _OAUTH_TOKEN_URL,
            method='POST',
            data={
                'client_id': cfg['oauth_client_id'],
                'client_secret': cfg['oauth_client_secret'],
                'refresh_token': tok['refresh_token'],
                'grant_type': 'refresh_token',
            },
        )
        tok['access_token'] = data['access_token']
        tok['expires_at'] = time.time() + data.get('expires_in', 3600) - 60
        _save_token(tok)
        return tok
    except Exception as exc:
        log.error('Token refresh failed: %s', exc)
        return None


def _get_access_token(cfg):
    tok = _load_token()
    if not tok:
        return None
    if time.time() >= tok.get('expires_at', 0):
        tok = _refresh_access_token(cfg, tok)
    return tok.get('access_token') if tok else None


_auth_lock = threading.Lock()
_auth_state = {
    'pending': False,
    'user_code': '',
    'verification_url': '',
    'expires_at': 0,
    'interval': 5,
    'error': '',
}


def _auth_public_state():
    with _auth_lock:
        return {
            'pending': _auth_state['pending'],
            'user_code': _auth_state['user_code'],
            'verification_url': _auth_state['verification_url'],
            'expires_at': _auth_state['expires_at'],
            'interval': _auth_state['interval'],
            'error': _auth_state['error'],
        }


def _start_device_auth(cfg):
    try:
        data = _http_json(
            _DEVICE_CODE_URL,
            method='POST',
            data={
                'client_id': cfg['oauth_client_id'],
                'scope': _OAUTH_SCOPE,
            },
        )
        with _auth_lock:
            _auth_state.update({
                'pending': True,
                '_device_code': data['device_code'],
                'user_code': data['user_code'],
                'verification_url': data.get('verification_url', 'https://www.google.com/device'),
                'expires_at': time.time() + data.get('expires_in', 1800),
                'interval': data.get('interval', 5),
                'error': '',
            })
        threading.Thread(target=_poll_device_auth, args=(cfg,), daemon=True).start()
        return True, data['user_code'], data.get('verification_url', 'https://www.google.com/device')
    except Exception as exc:
        log.error('Device auth start failed: %s', exc)
        return False, '', str(exc)


def _poll_device_auth(cfg):
    while True:
        with _auth_lock:
            if not _auth_state['pending']:
                return
            if time.time() >= _auth_state['expires_at']:
                _auth_state['pending'] = False
                _auth_state['error'] = 'Authorization timed out — start again'
                return
            device_code = _auth_state['_device_code']
            interval = _auth_state['interval']

        time.sleep(interval)

        try:
            data = _http_json(
                _OAUTH_TOKEN_URL,
                method='POST',
                data={
                    'client_id': cfg['oauth_client_id'],
                    'client_secret': cfg['oauth_client_secret'],
                    'device_code': device_code,
                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                },
            )
        except urllib.error.HTTPError as exc:
            data = json.loads(exc.read().decode() or '{}')
        except Exception as exc:
            log.error('Device auth poll failed: %s', exc)
            time.sleep(5)
            continue

        err = data.get('error', '')
        if err == 'authorization_pending':
            continue
        if err == 'slow_down':
            with _auth_lock:
                _auth_state['interval'] = min(interval + 5, 30)
            continue
        if err == 'access_denied':
            with _auth_lock:
                _auth_state['pending'] = False
                _auth_state['error'] = 'Access denied — try again'
            return
        if err:
            with _auth_lock:
                _auth_state['pending'] = False
                _auth_state['error'] = err
            return

        data['expires_at'] = time.time() + data.get('expires_in', 3600) - 60
        _save_token(data)
        with _auth_lock:
            _auth_state['pending'] = False
            _auth_state['error'] = ''
        log.info('Device auth complete — token saved')
        return


def _yt_headers(cfg):
    tok = _get_access_token(cfg)
    if not tok:
        return None
    return {'Authorization': f'Bearer {tok}'}


def _api_get(cfg, path, params):
    headers = _yt_headers(cfg)
    if not headers:
        raise RuntimeError('Not authorized')
    return _http_json(f'{_YT_API}/{path}', params=params, headers=headers)


def _analytics_get(cfg, params):
    headers = _yt_headers(cfg)
    if not headers:
        raise RuntimeError('Not authorized')
    return _http_json(f'{_YT_ANALYTICS_API}/reports', params=params, headers=headers)


def _google_error_message(exc):
    try:
        data = json.loads(exc.read().decode() or '{}')
    except Exception:
        return str(exc)
    return ((data.get('error') or {}).get('message') or str(exc)).strip()


def _friendly_analytics_message(message):
    lower = message.lower()
    if 'youtube analytics api has not been used' in lower or 'youtubeanalytics.googleapis.com' in lower:
        return 'Enable YouTube Analytics API for avg view duration'
    if 'insufficient authentication scopes' in lower or 'insufficientpermissions' in lower:
        return 'Reauthorize companion for analytics access'
    if 'forbidden' in lower or 'permission denied' in lower:
        return 'Analytics access denied'
    return 'Average view duration unavailable'


def _rfc3339_date(value):
    if not value:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return value[:10]


def _format_duration(seconds):
    if seconds is None:
        return ''
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def _fetch_video_metrics(cfg, video_id):
    items = _api_get(
        cfg,
        'videos',
        {
            'part': 'statistics,liveStreamingDetails',
            'id': video_id,
            'maxResults': 1,
        },
    ).get('items', [])
    return items[0] if items else None


def _fetch_audience_metrics(cfg, video_id, started_at, ended_at):
    audience = {
        'views': None,
        'average_view_duration_seconds': None,
        'average_view_duration_label': '',
        'concurrent_viewers': None,
        'note': '',
    }
    if not video_id:
        return audience

    video = _fetch_video_metrics(cfg, video_id)
    stats = (video or {}).get('statistics') or {}
    live = (video or {}).get('liveStreamingDetails') or {}
    if 'viewCount' in stats:
        audience['views'] = int(stats.get('viewCount') or 0)
    if 'concurrentViewers' in live:
        audience['concurrent_viewers'] = int(live.get('concurrentViewers') or 0)

    try:
        report = _analytics_get(
            cfg,
            {
                'ids': 'channel==MINE',
                'startDate': _rfc3339_date(started_at),
                'endDate': _rfc3339_date(ended_at),
                'metrics': 'views,averageViewDuration',
                'filters': f'video=={video_id}',
            },
        )
    except urllib.error.HTTPError as exc:
        audience['note'] = _friendly_analytics_message(_google_error_message(exc))
        return audience
    except Exception:
        audience['note'] = 'Average view duration unavailable'
        return audience

    rows = report.get('rows') or []
    if not rows:
        audience['note'] = 'Analytics not ready yet'
        return audience

    row = rows[0]
    if len(row) >= 1:
        audience['views'] = int(row[0])
    if len(row) >= 2:
        audience['average_view_duration_seconds'] = int(round(float(row[1])))
        audience['average_view_duration_label'] = _format_duration(audience['average_view_duration_seconds'])
    return audience


def _broadcast_rank(item):
    status = (item.get('status') or {}).get('lifeCycleStatus', '')
    order = {
        'live': 0,
        'testing': 1,
        'ready': 2,
        'created': 3,
        'complete': 4,
        'revoked': 5,
    }
    snippet = item.get('snippet') or {}
    when = (
        snippet.get('actualStartTime')
        or snippet.get('scheduledStartTime')
        or snippet.get('publishedAt')
        or ''
    )
    return (order.get(status, 99), when)


def _select_broadcast(items):
    if not items:
        return None
    ranked = sorted(items, key=_broadcast_rank)
    return ranked[0]


def _select_stream(items):
    if not items:
        return None
    priority = {'active': 0, 'ready': 1, 'created': 2, 'inactive': 3, 'error': 4}
    return sorted(items, key=lambda i: priority.get(((i.get('status') or {}).get('streamStatus', '')), 99))[0]


_status_lock = threading.Lock()
_status = {
    'authorized': False,
    'updated_at': 0,
    'error': '',
    'broadcast': None,
    'stream': None,
    'audience': None,
    'issues': [],
}


def _status_snapshot():
    with _status_lock:
        return json.loads(json.dumps(_status))


def _poll_once():
    cfg = _load_cfg()
    headers = _yt_headers(cfg)
    with _status_lock:
        _status['authorized'] = bool(headers)
        _status['updated_at'] = int(time.time())
    if not headers:
        with _status_lock:
            _status['error'] = 'Authorize this companion to read YouTube status'
            _status['broadcast'] = None
            _status['stream'] = None
            _status['audience'] = None
            _status['issues'] = []
        return

    try:
        broadcasts = _api_get(
            cfg,
            'liveBroadcasts',
            {
                'part': 'snippet,status,contentDetails',
                'mine': 'true',
                'maxResults': 10,
            },
        ).get('items', [])

        broadcast = _select_broadcast(broadcasts)
        stream = None
        if broadcast:
            bound_stream_id = ((broadcast.get('contentDetails') or {}).get('boundStreamId') or '')
            if bound_stream_id:
                streams = _api_get(
                    cfg,
                    'liveStreams',
                    {
                        'part': 'snippet,status,cdn',
                        'id': bound_stream_id,
                    },
                ).get('items', [])
                stream = streams[0] if streams else None

        if not stream:
            streams = _api_get(
                cfg,
                'liveStreams',
                {
                    'part': 'snippet,status,cdn',
                    'mine': 'true',
                    'maxResults': 10,
                },
            ).get('items', [])
            stream = _select_stream(streams)

        audience = _fetch_audience_metrics(
            cfg,
            (broadcast or {}).get('id', ''),
            ((broadcast or {}).get('snippet') or {}).get('actualStartTime', ''),
            ((broadcast or {}).get('snippet') or {}).get('actualEndTime', ''),
        )
        issues = (((stream or {}).get('status') or {}).get('healthStatus') or {}).get('configurationIssues') or []

        with _status_lock:
            _status['error'] = ''
            _status['broadcast'] = {
                'id': (broadcast or {}).get('id', ''),
                'title': ((broadcast or {}).get('snippet') or {}).get('title', ''),
                'life_cycle_status': ((broadcast or {}).get('status') or {}).get('lifeCycleStatus', ''),
                'privacy_status': ((broadcast or {}).get('status') or {}).get('privacyStatus', ''),
                'scheduled_start_time': ((broadcast or {}).get('snippet') or {}).get('scheduledStartTime', ''),
                'actual_start_time': ((broadcast or {}).get('snippet') or {}).get('actualStartTime', ''),
                'actual_end_time': ((broadcast or {}).get('snippet') or {}).get('actualEndTime', ''),
                'bound_stream_id': ((broadcast or {}).get('contentDetails') or {}).get('boundStreamId', ''),
            } if broadcast else None
            _status['stream'] = {
                'id': (stream or {}).get('id', ''),
                'title': ((stream or {}).get('snippet') or {}).get('title', ''),
                'stream_status': ((stream or {}).get('status') or {}).get('streamStatus', ''),
                'health_status': (((stream or {}).get('status') or {}).get('healthStatus') or {}).get('status', ''),
                'health_last_update': (((stream or {}).get('status') or {}).get('healthStatus') or {}).get('lastUpdateTimeSeconds', ''),
                'resolution': ((stream or {}).get('cdn') or {}).get('resolution', ''),
                'frame_rate': ((stream or {}).get('cdn') or {}).get('frameRate', ''),
                'ingestion_type': ((stream or {}).get('cdn') or {}).get('ingestionType', ''),
            } if stream else None
            _status['audience'] = audience
            _status['issues'] = issues
    except Exception as exc:
        log.error('YouTube poll failed: %s', exc)
        with _status_lock:
            _status['error'] = str(exc)


def _poll_loop():
    while True:
        _poll_once()
        cfg = _load_cfg()
        time.sleep(cfg['poll_interval_seconds'])


_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>YouTube Companion</title>
  <style>
    body{font-family:monospace;background:#0d0d0d;color:#ddd;margin:0;padding:18px}
    h1{margin:0 0 14px 0;font-size:24px;color:#6ee7ff}
    .wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
    .card{background:#171717;border:1px solid #2a2a2a;border-radius:8px;padding:14px}
    .row{margin:6px 0}
    .label{color:#8ab4c9}
    input{width:100%;box-sizing:border-box;background:#101010;color:#fff;border:1px solid #333;border-radius:4px;padding:8px;margin-top:4px}
    button{background:#15364a;color:#dff8ff;border:1px solid #3aa3d1;border-radius:6px;padding:9px 12px;cursor:pointer;font-family:monospace}
    .warn{color:#ffd166}
    .bad{color:#ff8a80}
    .good{color:#8be28b}
    .code{font-size:24px;font-weight:bold;letter-spacing:2px;color:#fff}
    ul{padding-left:20px}
  </style>
</head>
<body>
  <h1>YouTube Companion</h1>
  <div class="wrap">
    <div class="card">
      <div class="row"><span class="label">OAuth Client ID</span><input id="client-id" value="__CLIENT_ID__" placeholder="Google OAuth client ID"></div>
      <div class="row"><span class="label">OAuth Client Secret</span><input id="client-secret" type="password" value="__CLIENT_SECRET__" placeholder="Google OAuth client secret"></div>
      <div class="row"><span class="label">Poll interval (seconds)</span><input id="poll-seconds" value="__POLL_SECONDS__" placeholder="15"></div>
      <div class="row"><span class="label">Streamer Pi address / hostname</span><input id="streamer-host" value="__STREAMER_HOST__" placeholder="192.168.0.123"></div>
      <div class="row"><span class="label">Streamer Pi port</span><input id="streamer-port" value="__STREAMER_PORT__" placeholder="8090"></div>
      <div class="row"><span class="label">Streamer control token</span><input id="streamer-control-token" type="password" value="__STREAMER_CONTROL_TOKEN__" placeholder="Needed only for Pi shutdown"></div>
      <div class="row" style="margin-top:12px"><button onclick="saveSettings()">Save Settings</button> <button onclick="startAuth()">Start Device Auth</button> <button onclick="clearToken()">Clear Token</button></div>
      <div class="row" id="save-msg"></div>
    </div>
    <div class="card">
      <div class="row"><span class="label">Authorized</span> <span id="authorized"></span></div>
      <div class="row"><span class="label">Auth Pending</span> <span id="pending"></span></div>
      <div class="row"><span class="label">Verification URL</span> <span id="verify-url"></span></div>
      <div class="row"><span class="label">User Code</span></div>
      <div class="row code" id="user-code">-</div>
      <div class="row warn" id="auth-error"></div>
    </div>
    <div class="card">
      <div class="row"><span class="label">Broadcast</span> <span id="broadcast-title"></span></div>
      <div class="row"><span class="label">Lifecycle</span> <span id="life-cycle"></span></div>
      <div class="row"><span class="label">Privacy</span> <span id="privacy"></span></div>
      <div class="row"><span class="label">Started</span> <span id="started"></span></div>
      <div class="row"><span class="label">Ended</span> <span id="ended"></span></div>
    </div>
    <div class="card">
      <div class="row"><span class="label">Stream Status</span> <span id="stream-status"></span></div>
      <div class="row"><span class="label">Health</span> <span id="health-status"></span></div>
      <div class="row"><span class="label">Video</span> <span id="video-mode"></span></div>
      <div class="row"><span class="label">Last Update</span> <span id="health-updated"></span></div>
      <div class="row bad" id="status-error"></div>
    </div>
    <div class="card">
      <div class="row"><span class="label">Views</span> <span id="audience-views"></span></div>
      <div class="row"><span class="label">Avg View Duration</span> <span id="audience-avd"></span></div>
      <div class="row"><span class="label">Concurrent Viewers</span> <span id="audience-ccv"></span></div>
      <div class="row warn" id="audience-note"></div>
    </div>
    <div class="card" style="grid-column:1/-1">
      <div class="row"><span class="label">Configuration Issues</span></div>
      <ul id="issues"></ul>
    </div>
  </div>
  <script>
    function text(id, value) {
      document.getElementById(id).textContent = value || '-';
    }
    function esc(s) {
      return String(s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    }
    function saveSettings() {
      const body = 'oauth_client_id=' + encodeURIComponent(document.getElementById('client-id').value)
        + '&oauth_client_secret=' + encodeURIComponent(document.getElementById('client-secret').value)
        + '&poll_interval_seconds=' + encodeURIComponent(document.getElementById('poll-seconds').value)
        + '&streamer_status_host=' + encodeURIComponent(document.getElementById('streamer-host').value)
        + '&streamer_status_port=' + encodeURIComponent(document.getElementById('streamer-port').value)
        + '&streamer_control_token=' + encodeURIComponent(document.getElementById('streamer-control-token').value);
      fetch('/settings', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body})
        .then(r => r.json()).then(d => text('save-msg', d.ok ? 'Saved' : d.msg || 'Save failed'));
    }
    function startAuth() {
      fetch('/auth/start', {method:'POST'}).then(r => r.json()).then(d => {
        text('save-msg', d.ok ? 'Device auth started' : d.msg || 'Auth failed');
        refresh();
      });
    }
    function clearToken() {
      fetch('/auth/clear', {method:'POST'}).then(r => r.json()).then(d => {
        text('save-msg', d.ok ? 'Token cleared' : d.msg || 'Clear failed');
        refresh();
      });
    }
    function refresh() {
      Promise.all([fetch('/status').then(r=>r.json()), fetch('/auth_status').then(r=>r.json())]).then(([s,a]) => {
        text('authorized', s.authorized ? 'yes' : 'no');
        text('pending', a.pending ? 'yes' : 'no');
        text('verify-url', a.verification_url || '');
        text('user-code', a.user_code || '-');
        text('auth-error', a.error || '');
        text('status-error', s.error || '');
        text('broadcast-title', s.broadcast ? s.broadcast.title : '');
        text('life-cycle', s.broadcast ? s.broadcast.life_cycle_status : '');
        text('privacy', s.broadcast ? s.broadcast.privacy_status : '');
        text('started', s.broadcast ? s.broadcast.actual_start_time : '');
        text('ended', s.broadcast ? s.broadcast.actual_end_time : '');
        text('stream-status', s.stream ? s.stream.stream_status : '');
        text('health-status', s.stream ? s.stream.health_status : '');
        text('video-mode', s.stream ? ((s.stream.resolution || '-') + ' / ' + (s.stream.frame_rate || '-')) : '');
        text('health-updated', s.stream ? s.stream.health_last_update : '');
        text('audience-views', s.audience && s.audience.views != null ? s.audience.views : '');
        text('audience-avd', s.audience ? s.audience.average_view_duration_label : '');
        text('audience-ccv', s.audience && s.audience.concurrent_viewers != null ? s.audience.concurrent_viewers : '');
        text('audience-note', s.audience ? s.audience.note : '');
        const issues = document.getElementById('issues');
        issues.innerHTML = '';
        (s.issues || []).forEach(issue => {
          const li = document.createElement('li');
          li.innerHTML = '<span class=\"' + (issue.severity === 'error' ? 'bad' : 'warn') + '\">' + esc(issue.type) + '</span> — ' + esc(issue.description || issue.reason || '');
          issues.appendChild(li);
        });
        if (!issues.children.length) {
          const li = document.createElement('li');
          li.textContent = 'No active configuration issues';
          issues.appendChild(li);
        }
      });
    }
    setInterval(refresh, 5000);
    refresh();
  </script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            self._serve_dashboard()
        elif path == '/status':
            self._json(_status_snapshot())
        elif path == '/auth_status':
            st = _auth_public_state()
            st['authorized'] = bool(_load_token())
            self._json(st)
        else:
            self._send(404, b'Not found', 'text/plain')

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        form = urllib.parse.parse_qs(body)
        get = lambda k, d='': form.get(k, [d])[0]

        if path == '/settings':
            cfg = _load_cfg()
            cfg['oauth_client_id'] = get('oauth_client_id').strip()
            cfg['oauth_client_secret'] = get('oauth_client_secret').strip()
            cfg['streamer_status_host'] = get('streamer_status_host', cfg['streamer_status_host']).strip()
            cfg['streamer_control_token'] = get('streamer_control_token', cfg['streamer_control_token']).strip()[:120]
            try:
                cfg['poll_interval_seconds'] = max(5, int(get('poll_interval_seconds', cfg['poll_interval_seconds'])))
                cfg['streamer_status_port'] = max(1, int(get('streamer_status_port', cfg['streamer_status_port'])))
            except ValueError:
                self._json({'ok': False, 'msg': 'Poll interval and streamer port must be numbers'})
                return
            _save_cfg(cfg)
            _poll_once()
            self._json({'ok': True})
        elif path == '/auth/start':
            cfg = _load_cfg()
            if not cfg.get('oauth_client_id') or not cfg.get('oauth_client_secret'):
                self._json({'ok': False, 'msg': 'Save client ID and secret first'})
                return
            ok, code, msg = _start_device_auth(cfg)
            self._json({'ok': ok, 'user_code': code, 'msg': msg})
        elif path == '/auth/clear':
            _delete_token()
            with _auth_lock:
                _auth_state['pending'] = False
                _auth_state['error'] = ''
            _poll_once()
            self._json({'ok': True})
        else:
            self._send(404, b'Not found', 'text/plain')

    def _serve_dashboard(self):
        cfg = _load_cfg()
        body = (
            _HTML
            .replace('__CLIENT_ID__', html.escape(cfg.get('oauth_client_id', '')))
            .replace('__CLIENT_SECRET__', html.escape(cfg.get('oauth_client_secret', '')))
            .replace('__POLL_SECONDS__', str(cfg.get('poll_interval_seconds', 15)))
            .replace('__STREAMER_HOST__', html.escape(cfg.get('streamer_status_host', '')))
            .replace('__STREAMER_PORT__', str(cfg.get('streamer_status_port', 8090)))
            .replace('__STREAMER_CONTROL_TOKEN__', html.escape(cfg.get('streamer_control_token', '')))
        ).encode()
        self._send(200, body, 'text/html; charset=utf-8')

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self._send(200, body, 'application/json')


def main():
    cfg = _load_cfg()
    _poll_once()
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer((cfg['listen_host'], cfg['listen_port']), _Handler)
    log.info('YouTube Companion listening on http://%s:%d', cfg['listen_host'], cfg['listen_port'])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
