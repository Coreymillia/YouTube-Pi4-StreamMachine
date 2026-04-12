/**
 * YouTubeCYD — stream health dashboard for YouTube-Pi
 *
 * Hardware: ESP32 "Cheap Yellow Display" (ESP32-2432S028)
 * - 320x240 ILI9341 TFT
 * - XPT2046 touch
 *
 * Displays live stream health from YouTube-Pi /status on port 8090.
 */

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <Arduino_GFX_Library.h>
#include <XPT2046_Touchscreen.h>

#include "Portal.h"
#include "Status.h"

#define GFX_BL 21
Arduino_DataBus *bus = new Arduino_HWSPI(2, 15, 14, 13, 12);
Arduino_GFX    *gfx = new Arduino_ILI9341(bus, GFX_NOT_DEFINED, 1);

#define XPT2046_IRQ  36
#define XPT2046_CS   33
#define XPT2046_CLK  25
#define XPT2046_MOSI 32
#define XPT2046_MISO 39
SPIClass touchSPI(VSPI);
XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_IRQ);

#define W 320
#define H 240
#define HDR_H 28
#define BTN_H 18

#define C_BG       0x0000
#define C_CARD     0x1082
#define C_DIM      0x528A
#define C_WHITE    0xFFFF
#define C_RED      0xF800
#define C_GREEN    0x07E0
#define C_YELLOW   0xFFE0
#define C_CYAN     0x07FF
#define C_BLUE     0x039F
#define C_ORANGE   0xFD20

struct Button {
    const char* label;
    int x;
    int y;
    int w;
    int h;
    uint16_t bg;
};

static Button btnStart   = {"GO",   96,  5, 34, BTN_H, 0x0320};
static Button btnStop    = {"STOP", 136, 5, 48, BTN_H, 0x6000};
static Button btnRefresh = {"REF",  190, 5, 40, BTN_H, 0x0018};
static Button btnPortal  = {"PORT", 236, 5, 48, BTN_H, 0x4208};

static char g_action_msg[64] = "Connecting...";
static unsigned long g_action_until = 0;
static unsigned long last_fetch_ms = 0;
static bool touch_was_down = false;
static bool screen_dirty = true;
static bool action_was_visible = false;

static void setAction(const char* msg, unsigned long ms = 2500) {
    strlcpy(g_action_msg, msg, sizeof(g_action_msg));
    g_action_until = millis() + ms;
    screen_dirty = true;
}

static void mapTouch(uint16_t rx, uint16_t ry, int& sx, int& sy) {
    sx = map(rx, 200, 3800, 0, W);
    sy = map(ry, 200, 3800, 0, H);
    sx = constrain(sx, 0, W - 1);
    sy = constrain(sy, 0, H - 1);
}

static void drawButton(const Button& b) {
    gfx->fillRoundRect(b.x, b.y, b.w, b.h, 6, b.bg);
    gfx->drawRoundRect(b.x, b.y, b.w, b.h, 6, C_WHITE);
    gfx->setTextColor(C_WHITE, b.bg);
    gfx->setTextSize(1);
    int tx = b.x + (b.w - strlen(b.label) * 6) / 2;
    gfx->setCursor(tx, b.y + 6);
    gfx->print(b.label);
}

static void fmtUptime(char* out, size_t out_sz, uint32_t s) {
    snprintf(out, out_sz, "%02lu:%02lu:%02lu",
             (unsigned long)(s / 3600),
             (unsigned long)((s % 3600) / 60),
             (unsigned long)(s % 60));
}

static void drawWrapped(const char* text, int x, int y, int maxChars, uint16_t fg, uint16_t bg, int maxLines = 2) {
    gfx->setTextColor(fg, bg);
    gfx->setTextSize(1);
    if (!text || !*text) {
        gfx->setCursor(x, y);
        gfx->print("-");
        return;
    }
    int len = strlen(text);
    int pos = 0;
    for (int line = 0; line < maxLines && pos < len; line++) {
        int take = min(maxChars, len - pos);
        char buf[64];
        strncpy(buf, text + pos, take);
        buf[take] = '\0';
        gfx->setCursor(x, y + line * 11);
        gfx->print(buf);
        pos += take;
    }
}

static void drawDashboard() {
    gfx->fillRect(0, 0, W, HDR_H, C_BLUE);
    gfx->setTextColor(C_WHITE, C_BLUE);
    gfx->setTextSize(2);
    gfx->setCursor(8, 7);
    gfx->print("YT CYD");
    drawButton(btnStart);
    drawButton(btnStop);
    drawButton(btnRefresh);
    drawButton(btnPortal);
    gfx->setTextSize(1);
    gfx->fillRect(286, 5, 30, 18, C_BLUE);
    gfx->setCursor(286, 11);
    gfx->print(yt_status.api_online ? "api:ok" : "api:down");

    uint16_t statusCol = C_DIM;
    const char* statusTxt = "IDLE";
    if (yt_status.running && yt_status.retries > 0) {
        statusCol = C_YELLOW;
        statusTxt = "RECOVER";
    } else if (yt_status.running) {
        statusCol = C_RED;
        statusTxt = "LIVE";
    }

    gfx->fillRoundRect(8, 36, 96, 52, 8, statusCol);
    gfx->drawRoundRect(8, 36, 96, 52, 8, C_WHITE);
    gfx->setTextColor(C_WHITE, statusCol);
    gfx->setTextSize(2);
    gfx->setCursor(20, 51);
    gfx->print(statusTxt);

    gfx->fillRoundRect(112, 36, 200, 52, 8, C_CARD);
    gfx->drawRoundRect(112, 36, 200, 52, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setTextSize(1);
    gfx->setCursor(122, 46);
    gfx->print("CAM / AUDIO");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setCursor(122, 60);
    gfx->print(strlen(yt_status.cam_name) ? yt_status.cam_name : "No camera");
    gfx->setCursor(122, 72);
    gfx->print(strlen(yt_status.audio_name) ? yt_status.audio_name : "No audio");

    gfx->fillRoundRect(8, 96, 148, 44, 8, C_CARD);
    gfx->drawRoundRect(8, 96, 148, 44, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(18, 106);
    gfx->print("UPTIME / RETRIES");
    gfx->setTextColor(C_WHITE, C_CARD);
    char up[16];
    fmtUptime(up, sizeof(up), yt_status.uptime_s);
    gfx->setCursor(18, 120);
    gfx->print(up);
    gfx->setCursor(92, 120);
    gfx->print("r:");
    gfx->print(yt_status.retries);

    gfx->fillRoundRect(164, 96, 148, 44, 8, C_CARD);
    gfx->drawRoundRect(164, 96, 148, 44, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(174, 106);
    gfx->print("ETH / RTMP");
    gfx->setTextColor(yt_status.eth_carrier ? C_GREEN : C_YELLOW, C_CARD);
    gfx->setCursor(174, 120);
    gfx->print(yt_status.eth_carrier ? "eth:up " : "eth:down ");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->print(yt_status.eth_oper);
    gfx->setCursor(174, 120);
    gfx->print(yt_status.eth_carrier ? "eth:up " : "eth:down ");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->print(yt_status.eth_oper);
    gfx->setCursor(174, 132);
    gfx->print("rtmp:");
    gfx->print(strlen(yt_status.rtmp_state) ? yt_status.rtmp_state : "-");

    gfx->fillRoundRect(8, 148, 148, 44, 8, C_CARD);
    gfx->drawRoundRect(8, 148, 148, 44, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(18, 158);
    gfx->print("LAN TRAFFIC");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setCursor(18, 172);
    gfx->print("tx:");
    gfx->print(yt_status.tx_kbps);
    gfx->print(" kbps");
    gfx->setCursor(18, 184);
    gfx->print("rx:");
    gfx->print(yt_status.rx_kbps);
    gfx->print(" kbps");

    gfx->fillRoundRect(164, 148, 148, 44, 8, C_CARD);
    gfx->drawRoundRect(164, 148, 148, 44, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(174, 158);
    gfx->print("SYSTEM");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setCursor(174, 172);
    gfx->print("temp:");
    gfx->print(yt_status.temp_c, 1);
    gfx->print(" C");
    gfx->setCursor(174, 184);
    gfx->print("thr:");
    gfx->print(strlen(yt_status.throttled) ? yt_status.throttled : "-");

    gfx->fillRoundRect(8, 198, 304, 16, 6, C_CARD);
    gfx->drawRoundRect(8, 198, 304, 16, 6, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(14, 203);
    gfx->print("MSG:");
    gfx->setTextColor(yt_status.msg_enabled ? C_WHITE : C_DIM, C_CARD);
    drawWrapped(yt_status.msg_enabled ? yt_status.msg_text : "off", 44, 203, 44, yt_status.msg_enabled ? C_WHITE : C_DIM, C_CARD, 1);

    gfx->fillRect(0, 220, W, 20, C_BG);
    gfx->setTextColor(C_DIM, C_BG);
    gfx->setCursor(8, 226);
    if (g_action_until > millis()) {
        gfx->print(g_action_msg);
    } else if (strlen(yt_status.error)) {
        drawWrapped(yt_status.error, 8, 226, 50, C_ORANGE, C_BG, 1);
    } else {
        gfx->print(WiFi.localIP());
        gfx->print(" -> ");
        gfx->print(pt_pi_ip);
        gfx->print(":");
        gfx->print(pt_pi_port);
    }
}

static bool inButton(const Button& b, int tx, int ty) {
    return tx >= b.x && tx <= (b.x + b.w) && ty >= b.y && ty <= (b.y + b.h);
}

void setup() {
    Serial.begin(115200);
    pinMode(GFX_BL, OUTPUT);
    digitalWrite(GFX_BL, HIGH);
    gfx->begin();
    gfx->fillScreen(C_BG);
    gfx->setTextColor(C_CYAN, C_BG);
    gfx->setTextSize(2);
    gfx->setCursor(26, 90);
    gfx->print("YOUTUBE CYD");
    gfx->setTextSize(1);
    gfx->setCursor(26, 118);
    gfx->print("Connecting to WiFi...");

    touchSPI.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
    ts.begin(touchSPI);
    ts.setRotation(1);

    ptConnect();
    ytFetchStatus(pt_pi_ip, pt_pi_port);
    setAction("Connected");
    gfx->fillScreen(C_BG);
    drawDashboard();
}

void loop() {
    unsigned long now = millis();

    if (now - last_fetch_ms >= 2000) {
        last_fetch_ms = now;
        ytFetchStatus(pt_pi_ip, pt_pi_port);
        screen_dirty = true;
    }

    bool action_visible = g_action_until > now;
    if (action_visible != action_was_visible) {
        action_was_visible = action_visible;
        screen_dirty = true;
    }

    if (screen_dirty) {
        drawDashboard();
        screen_dirty = false;
    }

    bool touched = ts.tirqTouched() && ts.touched();
    if (touched && !touch_was_down) {
        TS_Point p = ts.getPoint();
        int sx, sy;
        mapTouch(p.x, p.y, sx, sy);

        if (inButton(btnStart, sx, sy)) {
            char body[24];
            snprintf(body, sizeof(body), "cam_idx=%d", yt_status.start_cam_idx);
            if (ytPost(pt_pi_ip, pt_pi_port, "/start", body)) setAction("Start sent");
            else setAction("Start failed");
        } else if (inButton(btnStop, sx, sy)) {
            if (ytPost(pt_pi_ip, pt_pi_port, "/stop", "")) setAction("Stop sent");
            else setAction("Stop failed");
        } else if (inButton(btnRefresh, sx, sy)) {
            if (ytFetchStatus(pt_pi_ip, pt_pi_port)) setAction("Refreshed");
            else setAction("Refresh failed");
        } else if (inButton(btnPortal, sx, sy)) {
            setAction("Opening portal...");
            delay(500);
            ptClearSettings();
            ESP.restart();
        }
    }
    touch_was_down = touched;
    delay(20);
}
