# YouTube-Pi4-StreamMachine

A standalone YouTube live streaming daemon for the **Raspberry Pi 4** — built for a coin engraving workbench but usable for any close-up or studio stream. Browser-based web UI, live MJPEG focus preview, quality controls, mid-stream camera switching, and an optional YouTube Live Chat Bot. No LCD HAT, no physical buttons, no desktop environment required.

---

## Screenshots

### Stream Control + Live Focus Preview
![Stream Control and Focus Preview](IMG_20260410_153439755_HDR.jpg)
*The left panel controls the stream. The right panel shows a live 5fps MJPEG preview — adjust your lens in real time without clicking a shutter. Here showing an engraved nickel under the HQ camera.*

### Quality Controls + Chat Bot Panel
![Quality Controls and Chat Bot](IMG_20260410_153512234_HDR.jpg)
*Brightness, Contrast, Saturation, Sharpness, Zoom, and White Balance sliders. Below: the YouTube Live Chat Bot configuration panel with headless message, random messages, and keyword triggers.*

### The Bench Setup
![Coin Engraving Bench](IMG_20260410_155323931_HDR.jpg)
*The actual workbench — Leica microscope, HQ camera on a gooseneck arm, engraving tools, polished coins, and the YouTube Studio UI visible on a tablet in the corner. This is what "bench chaos" looks like when it's working.*

---

## Hardware

- **Raspberry Pi 4 Model B**
- **Raspberry Pi HQ Camera (IMX477)** — connected via CSI ribbon to the CAM/DISP 0 port (closest to USB-C power)
- **USB Microscope Camera** — optional, plug-and-play (H264 capable recommended)
- Optional: Bluetooth keyboard for SSH/terminal access without a screen

---

## Features

- **Live MJPEG focus preview** at ~5 fps — adjust your lens while watching. No clicking, no refreshing.
- **Rule-of-thirds grid overlay** toggle for shot framing
- **Camera auto-detection** — detects connected cameras at boot, defaults to HQ cam if available
- **Mid-stream camera switch** — swap cameras during a live stream without disconnecting from YouTube
- **YouTube RTMP streaming** via `rpicam-vid` (HQ cam) or v4l2 H264 passthrough (USB cam)
- **Quality controls** — Brightness, Contrast, Saturation, Sharpness, Zoom, and White Balance — applied live to preview and stream
- **OTR Radio audio** — 12 Old Time Radio stations from the ROKiT Radio Network overlaid on the HQ cam stream
- **Auto-reconnect** — retries the stream up to 5 times on connection loss
- **YouTube Live Chat Bot** *(optional)* — posts headless messages, random timed messages, and keyword-triggered replies
- **Stream key saved locally** — stored in `config.json`, never transmitted anywhere else
- **Auto-starts on boot** via systemd

---

## OS

Flash with **Raspberry Pi OS Lite 64-bit (Bookworm)**. No desktop needed.

---

## Installation

### 1. Install dependencies

```bash
sudo apt update
sudo apt install -y ffmpeg v4l-utils rpicam-apps python3-pip
pip3 install requests
```

### 2. Clone and set up

```bash
git clone https://github.com/Coreymillia/YouTube-Pi4-StreamMachine.git /home/coreymillia/youtube-studio
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

The service auto-starts on every boot. Check it with:

```bash
sudo systemctl status youtube-studio
sudo journalctl -u youtube-studio -f    # live logs
```

---

## Web UI

Open a browser on any device on the same network:

```
http://<pi-ip>:8090
```

| Endpoint | Description |
|---|---|
| `/` | Main dashboard |
| `/preview` | Raw MJPEG feed |
| `/status` | JSON status (running, uptime, cams, quality) |
| `/bot_status` | JSON chat bot status |

---

## Dashboard Sections

### Stream Control
- Live/idle status dot and uptime timer
- Camera selector (auto-populated from detected cameras)
- OTR audio station selector
- **Start Stream** / **Stop Stream** buttons
- **↺ Switch Camera** button — appears only while streaming, lets you hot-swap cameras mid-stream (~3s pipeline restart, YouTube connection stays alive)

### Focus Preview
- Live 5fps MJPEG feed from the selected camera
- Toggle rule-of-thirds grid overlay
- Refresh Preview button
- Switching the camera dropdown automatically updates the preview

### Quality Controls
All settings apply to both the live preview and the stream. Hit **Apply to Preview** to commit.

| Slider | Range | Notes |
|---|---|---|
| Brightness | -100 → +100 | 0 = no change. Lift the image if your scene is dark. |
| Contrast | 0 → 200 | 100 = neutral. For shiny metal/coins, keep near neutral — high contrast blows out highlights. |
| Saturation | 0 → 200 | 100 = neutral. Lower this if your LED lighting causes a color cast. |
| Sharpness | 0 → 200 | 100 = neutral. Don't push too high — creates artifacts on metal edges. |
| Zoom (x) | 1.0× → 4.0× | Center crop using the sensor ROI. 1.0 = full frame. |
| White Bal. | dropdown | See table below — the single most impactful setting for LED-lit setups. |

**White Balance modes:**

| Mode | Best for |
|---|---|
| Auto | Camera guesses — can be thrown off by LEDs |
| **Tungsten** | **LED ring lights and artificial lighting — try this first** |
| Fluorescent | Shop/fluorescent overhead lighting |
| Indoor | Mixed indoor light |
| Daylight | Natural window light |
| Cloudy | Overcast outdoor |
| Custom | Reserved for manual gains |

> **Tip for coin engraving / polished metal:** Start with **Tungsten** white balance, drop Saturation to ~70–80, and leave Contrast near 100. The bright LED on shiny silver/copper already creates natural contrast — you don't need to add more.

### Settings
- YouTube stream key (password field, saved to `config.json` on the Pi)

---

## Camera Notes

### HQ Camera (IMX477)

- Ribbon to **CAM/DISP 0** port — closest to the USB-C power port
- Blue contacts on the ribbon face **toward the HDMI ports**
- CSI cameras are detected at **boot** — plug in before powering on
- Streams at **1920×1080 @ 30fps** via `rpicam-vid` hardware encoder
- Preview at **640×480 @ 5fps**

### USB Microscope Camera

- Plug and play — no reboot required
- Streams at **1280×720 @ 30fps** via v4l2 H264 passthrough (no CPU re-encoding)
- Audio: silent AAC (YouTube requires an audio track)

---

## Audio

The HQ Camera stream overlays live audio from the **ROKiT Radio Network OTR** streams. Pick a station in the UI:

1940s Radio *(default)* · American Comedy · American Classics · Jazz Central · Comedy Gold · Mystery Radio · Crime & Suspense · Crime Radio · Adventure Stories · Drama Radio · Nostalgia Lane · Science Fiction

USB cam uses silent AAC to satisfy YouTube's audio requirement.

---

## Getting a YouTube Stream Key

1. Go to [YouTube Studio → Go Live](https://studio.youtube.com/)
2. **Stream** tab → copy your Stream Key
3. Paste into the Settings panel → Save Settings

> Enable **persistent stream key** in YouTube Studio if you don't want a new key every session.

---

## YouTube Live Chat Bot (Optional)

The chat bot posts messages to your YouTube Live chat automatically — useful when you're streaming headless with no keyboard or screen and can't read or respond to chat yourself.

### What it does

- Posts a **headless message** when your stream goes live (e.g. *"Hey chat! I'm headless — no screen or keyboard, so I can't see your messages. Stream on!"*)
- Posts a **random message** from your list at a configurable interval (e.g. every 10 minutes)
- Responds to **keyword triggers** — if someone types a keyword, the bot replies with a preset response
- Everything toggleable — the bot only runs when **Enable Bot** is checked AND a stream is live

### Google Cloud Setup (one-time)

The bot uses the YouTube Data API v3 with OAuth2. Google requires a verified project to post to chat. This is free.

**Step 1 — Create a Google Cloud project**
1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. In the left menu: **APIs & Services → Library**
4. Search for **YouTube Data API v3** → click it → click **Enable**

**Step 2 — Create OAuth2 credentials**
1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth 2.0 Client ID**
3. If prompted to configure the consent screen first:
   - User type: **External**
   - Fill in app name (anything), your email, save and continue through the rest
   - On the **Scopes** screen you can skip adding scopes here
   - On the **Test users** screen, add your YouTube account email
4. Back at Create Credentials → OAuth 2.0 Client ID:
   - Application type: **TV and Limited Input devices** ← this is critical, do NOT choose "Web application"
   - Name it anything (e.g. `YouTube-Pi Bot`)
   - Click **Create**
5. Copy the **Client ID** and **Client Secret**

> **Why "TV and Limited Input devices"?** Google does not allow IP addresses as OAuth redirect URIs for web apps. The TV/device flow uses a code you enter on a second device — no redirect URI needed. This is exactly what headless Pi setups require.

**Step 3 — Authorize in the web UI**
1. Open the YouTube Studio web UI → scroll to the **YouTube Live Chat Bot** panel
2. Expand **Google API Setup**
3. Paste your **Client ID** and **Client Secret** → click **Save Credentials**
4. Click **↺ Start Authorization**
5. A code box appears — go to **[google.com/device](https://www.google.com/device)** on any phone or laptop
6. Enter the code shown in the UI
7. Sign in with the Google account that owns your YouTube channel
8. Grant the permissions → the UI will show ✓ Authorized

Tokens are saved to `token.json` on the Pi. You won't need to re-authorize unless you revoke access.

**Step 4 — Configure the bot**
- **Headless Message** — posted once when stream goes live
- **Random Messages** — add as many as you want; one is picked randomly every N minutes
- **Keyword Triggers** — add a keyword + response pair; bot replies when it spots the keyword in any chat message (case-insensitive)
- **Interval** — how often (in minutes) a random message is posted (0 = disabled)
- Click **Save Bot Settings**, then enable the toggle

### Bot config in `config.json`

```json
"chat_bot": {
  "enabled": true,
  "oauth_client_id": "...",
  "headless_message": "Hey chat! I'm headless — no screen or keyboard here.",
  "random_messages": ["Stream on!", "Thanks for watching!"],
  "keyword_triggers": { "hello": "Hey! 👋", "link": "Check the description!" },
  "random_interval_minutes": 10
}
```

OAuth tokens are stored separately in `token.json` (gitignored — never committed).

---

## Network

Binds to `0.0.0.0:8090`. Works on Wi-Fi and Ethernet. Ethernet is detected automatically — no config change needed. If you switch interfaces, restart the service and the IP in the UI header updates.

---

## Service Management

```bash
sudo systemctl status youtube-studio     # check running state
sudo systemctl restart youtube-studio    # restart daemon
sudo systemctl stop youtube-studio       # stop
sudo journalctl -u youtube-studio -f     # live log tail
```

---

## Configuration Reference

All settings are stored in `config.json` on the Pi. Most are managed via the web UI.

| Field | Default | Description |
|---|---|---|
| `youtube_stream_key` | `""` | YouTube RTMP stream key |
| `otr_station_url` | 1940s Radio URL | Audio station for HQ cam stream |
| `quality.brightness` | `0.0` | -1.0 to 1.0 |
| `quality.contrast` | `1.0` | 0.0 to 2.0 (1.0 = neutral) |
| `quality.saturation` | `1.0` | 0.0 to 2.0 (1.0 = neutral) |
| `quality.sharpness` | `1.0` | 0.0 to 2.0 (1.0 = neutral) |
| `quality.zoom` | `1.0` | 1.0 to 4.0× center crop |
| `quality.awb` | `"auto"` | White balance mode |
| `chat_bot.*` | — | See Chat Bot section above |

---

## Project Structure

```
YouTube-Pi4-StreamMachine/
├── youtube_studio.py            # Entire daemon — web server, preview, stream, bot
├── config.example.json          # Copy to config.json and edit
├── systemd/
│   └── youtube-studio.service  # systemd unit — auto-start on boot
└── README.md
```

> `config.json` and `token.json` are gitignored and stay on the Pi only.
