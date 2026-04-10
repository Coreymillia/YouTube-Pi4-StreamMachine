# YouTube-Pi

A standalone YouTube live streaming daemon for **Raspberry Pi 4** with a browser-based web UI, live MJPEG focus preview, and support for both a USB camera and the Raspberry Pi HQ Camera (IMX477).

No LCD HAT, no physical buttons, and no desktop environment required. Control everything from any device on your network via a web browser.

---

## Hardware

- **Raspberry Pi 4 Model B**
- **Raspberry Pi HQ Camera (IMX477)** — connected via CSI ribbon to the CAM/DISP 0 port (closest to USB-C power)
- **USB Microscope Camera** (HY-3307 / Z-Star Venus, H264, 720p30) — optional, plug-and-play
- Optional: Bluetooth keyboard for terminal access

---

## Features

- **Live MJPEG focus preview** at ~5 fps — open in any browser, adjust the lens while watching in real time. No clicking required.
- **Rule-of-thirds grid overlay** toggle for framing your shot
- **Camera auto-detection** — the web UI detects which cameras are connected at boot and defaults to the best available one (HQ cam preferred)
- **YouTube RTMP streaming** via `rpicam-vid` (HQ cam) or `v4l2` H264 passthrough (USB cam)
- **OTR Radio audio** — 12 live Old Time Radio stations from the ROKiT Radio Network overlaid on the stream (or silent for USB cam)
- **Auto-reconnect** — retries the stream up to 5 times on connection loss
- **Stream key saved locally** — stored in `config.json`, never leaves the Pi
- **Auto-starts on boot** via systemd — service is ready before you even open a browser

---

## OS

Flash the Pi with **Raspberry Pi OS Lite 64-bit (Bookworm)**. No desktop needed.

---

## Installation

### 1. Install dependencies

```bash
sudo apt update
sudo apt install -y ffmpeg v4l-utils rpicam-apps python3-pip
```

### 2. Clone and set up

```bash
git clone https://github.com/Coreymillia/YouTube-Pi.git /home/coreymillia/youtube-studio
cd /home/coreymillia/youtube-studio
cp config.example.json config.json
```

### 3. Enable the systemd service

```bash
sudo cp systemd/youtube-studio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable youtube-studio.service
sudo systemctl start youtube-studio.service
```

The service will auto-start on every boot.

---

## Web UI

Open a browser on any device on the same network and go to:

```
http://<pi-ip>:8090
```

| Endpoint | Description |
|---|---|
| `http://<pi-ip>:8090/` | Main dashboard — camera select, stream control, settings |
| `http://<pi-ip>:8090/preview` | Live MJPEG feed for focus adjustment |
| `http://<pi-ip>:8090/status` | JSON status — running state, uptime, available cameras |

### Dashboard sections

**Stream Control**
- Shows live/idle status and stream uptime
- Camera selector (USB Microscope or HQ Camera)
- OTR audio station selector
- Start / Stop stream buttons

**Focus Preview**
- Live MJPEG feed from the selected camera at 5 fps
- Rule-of-thirds grid toggle
- Refresh Preview button to force a camera switch
- Adjusting the lens while watching this feed is all you need — no snapshots, no refreshing

**Settings**
- YouTube stream key input (password field, stored locally in `config.json`)
- Changes saved instantly via the Save button

---

## Camera notes

### HQ Camera (IMX477)

- Connect the ribbon cable to the **CAM/DISP 0** port on the Pi 4 — the one closest to the USB-C power port
- Insert the ribbon with the **blue contacts facing toward the HDMI ports**
- The Pi detects CSI cameras at **boot time** — a reboot is required after connecting
- Streams at **1920×1080 @ 30fps** using `rpicam-vid` hardware encoder
- Preview runs at **640×480 @ 5fps** for a smooth focus feed without taxing the CPU

### USB Microscope Camera

- Plug and play — detected automatically without a reboot
- Streams at **1280×720 @ 30fps** using v4l2 H264 passthrough (no re-encoding)
- Audio is silent (YouTube requires an audio track — a silent AAC stream is injected automatically)
- The HQ cam is better for focus adjustment since USB microscope cameras typically have fixed focus rings

---

## Audio

The HQ Camera stream overlays a live audio feed from the **ROKiT Radio Network OTR (Old Time Radio)** streams. You can select the station from the web UI dropdown. Available stations:

- 1940s Radio *(default)*
- American Comedy
- American Classics
- Jazz Central
- Comedy Gold
- Mystery Radio
- Crime & Suspense
- Crime Radio
- Adventure Stories
- Drama Radio
- Nostalgia Lane
- Science Fiction

The USB Microscope stream uses silent AAC audio to satisfy YouTube's audio track requirement.

---

## Getting a YouTube stream key

1. Go to [YouTube Studio → Go Live](https://studio.youtube.com/)
2. Select **Stream** tab
3. Copy your stream key
4. Paste it into the Settings section of the web UI and click Save

> Stream keys expire per session unless you enable the **persistent stream key** option in YouTube Studio settings.

---

## Configuration

Edit via the web UI or directly in `config.json`:

| Field | Default | Description |
|---|---|---|
| `youtube_stream_key` | `""` | YouTube RTMP stream key |
| `otr_station_url` | 1940s Radio | OTR audio station URL for the HQ cam stream |

---

## Service management

```bash
sudo systemctl status youtube-studio    # check status
sudo systemctl restart youtube-studio   # restart
sudo journalctl -u youtube-studio -f    # live logs
```

---

## Network

The server binds to `0.0.0.0:8090` and works on Wi-Fi and Ethernet automatically. If you switch from Wi-Fi to Ethernet, the IP shown in the web UI header will update on the next service restart.

---

## Project structure

```
YouTube-Pi/
├── youtube_studio.py        # Main daemon — web UI, MJPEG preview, stream control
├── config.example.json      # Config template (copy to config.json)
├── systemd/
│   └── youtube-studio.service   # systemd unit file
└── README.md
```
