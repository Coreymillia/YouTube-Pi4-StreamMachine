/**
 * YouTubeCompCYD — CYD dashboard for YouTubeCompanion.
 *
 * Hardware: ESP32 "Cheap Yellow Display" (ESP32-2432S028)
 * - 320x240 ILI9341 TFT
 * - XPT2046 touch
 *
 * Polls YouTubeCompanion /status and /auth_status on port 8091 and shows
 * YouTube-side broadcast, stream, and auth state on a dedicated CYD page set.
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

enum ScreenMode {
    SCREEN_STATUS,
    SCREEN_AUTH,
};

static Button btnStatus  = {"STAT",  70,  5, 38, BTN_H, 0x0210};
static Button btnAuth    = {"AUTH", 114,  5, 42, BTN_H, 0x4208};
static Button btnRefresh = {"REF",  208,  5, 34, BTN_H, 0x0018};
static Button btnPortal  = {"PORT", 248,  5, 42, BTN_H, 0x4208};
static Button btnStart   = {"START", 18, 210, 56, 18, 0x0320};
static Button btnClear   = {"CLEAR", 82, 210, 56, 18, 0x6000};

static ScreenMode current_screen = SCREEN_STATUS;
static char g_action_msg[64] = "Connecting...";
static unsigned long g_action_until = 0;
static unsigned long last_fetch_ms = 0;
static bool touch_was_down = false;
static bool screen_dirty = true;
static bool action_was_visible = false;

static void setAction(const char* msg, unsigned long ms = 2200) {
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
        if (take > 0 && pos + take < len) {
            int split = take;
            while (split > 12 && text[pos + split] != ' ') split--;
            if (split > 12) take = split;
        }
        char buf[96];
        strncpy(buf, text + pos, take);
        buf[take] = '\0';
        while (buf[0] == ' ') memmove(buf, buf + 1, strlen(buf));
        gfx->setCursor(x, y + line * 11);
        gfx->print(buf);
        pos += take;
    }
}

static bool inButton(const Button& b, int tx, int ty) {
    return tx >= b.x && tx <= (b.x + b.w) && ty >= b.y && ty <= (b.y + b.h);
}

static uint16_t statusColor() {
    if (!comp_status.api_online) return C_RED;
    if (comp_status.auth_pending) return C_YELLOW;
    if (comp_status.authorized) return C_GREEN;
    return C_ORANGE;
}

static const char* statusLabel() {
    if (!comp_status.api_online) return "OFFLINE";
    if (comp_status.auth_pending) return "PENDING";
    if (comp_status.authorized) return "READY";
    return "NO AUTH";
}

static void drawHeader() {
    gfx->fillRect(0, 0, W, HDR_H, C_BLUE);
    gfx->setTextColor(C_WHITE, C_BLUE);
    gfx->setTextSize(2);
    gfx->setCursor(8, 7);
    gfx->print("YTC");
    drawButton(btnStatus);
    drawButton(btnAuth);
    drawButton(btnRefresh);
    drawButton(btnPortal);
    gfx->fillCircle(304, 14, 4, comp_status.api_online ? C_GREEN : C_RED);
    gfx->drawCircle(304, 14, 4, C_WHITE);
}

static void drawStatusScreen() {
    gfx->fillScreen(C_BG);
    drawHeader();

    uint16_t state_col = statusColor();
    gfx->fillRoundRect(8, 36, 96, 52, 8, state_col);
    gfx->drawRoundRect(8, 36, 96, 52, 8, C_WHITE);
    gfx->setTextColor(C_WHITE, state_col);
    gfx->setTextSize(1);
    gfx->setCursor(26, 47);
    gfx->print("COMPANION");
    gfx->setTextSize(2);
    gfx->setCursor(16, 62);
    gfx->print(statusLabel());

    gfx->fillRoundRect(112, 36, 200, 52, 8, C_CARD);
    gfx->drawRoundRect(112, 36, 200, 52, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setTextSize(1);
    gfx->setCursor(122, 46);
    gfx->print("BROADCAST");
    gfx->setTextColor(C_WHITE, C_CARD);
    drawWrapped(strlen(comp_status.broadcast_title) ? comp_status.broadcast_title : "No active broadcast",
                122, 58, 28, C_WHITE, C_CARD, 2);
    gfx->setTextColor(C_DIM, C_CARD);
    gfx->setCursor(122, 79);
    gfx->print(strlen(comp_status.life_cycle) ? comp_status.life_cycle : "idle");
    if (strlen(comp_status.privacy_status)) {
        gfx->print(" / ");
        gfx->print(comp_status.privacy_status);
    }

    gfx->fillRoundRect(8, 96, 148, 50, 8, C_CARD);
    gfx->drawRoundRect(8, 96, 148, 50, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(18, 106);
    gfx->print("STREAM");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setCursor(18, 120);
    gfx->print("state: ");
    gfx->print(strlen(comp_status.stream_status) ? comp_status.stream_status : "-");
    gfx->setCursor(18, 132);
    gfx->print("health: ");
    gfx->print(strlen(comp_status.health_status) ? comp_status.health_status : "-");

    gfx->fillRoundRect(164, 96, 148, 50, 8, C_CARD);
    gfx->drawRoundRect(164, 96, 148, 50, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(174, 106);
    gfx->print("VIDEO / TIME");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setCursor(174, 120);
    gfx->print(strlen(comp_status.resolution) ? comp_status.resolution : "-");
    gfx->print(" / ");
    gfx->print(strlen(comp_status.frame_rate) ? comp_status.frame_rate : "-");
    gfx->setCursor(174, 132);
    gfx->print("start: ");
    gfx->print(strlen(comp_status.started_at) ? comp_status.started_at : "-");

    gfx->fillRoundRect(8, 154, 304, 46, 8, C_CARD);
    gfx->drawRoundRect(8, 154, 304, 46, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(18, 164);
    gfx->print("ISSUES");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setCursor(66, 164);
    gfx->print(comp_status.issue_count);
    if (comp_status.issue_count == 0) {
        drawWrapped("No active YouTube configuration issues", 18, 178, 45, C_WHITE, C_CARD, 1);
    } else {
        drawWrapped(comp_status.issue_type, 18, 176, 20, C_ORANGE, C_CARD, 1);
        drawWrapped(comp_status.issue_text, 18, 187, 45, C_WHITE, C_CARD, 1);
    }

    gfx->fillRect(0, 220, W, 20, C_BG);
    gfx->setTextColor(C_DIM, C_BG);
    gfx->setCursor(8, 226);
    if (g_action_until > millis()) {
        drawWrapped(g_action_msg, 8, 226, 50, C_CYAN, C_BG, 1);
    } else if (strlen(comp_status.api_error)) {
        drawWrapped(comp_status.api_error, 8, 226, 50, C_ORANGE, C_BG, 1);
    } else {
        gfx->print(WiFi.localIP());
        gfx->print(" -> ");
        gfx->print(pt_comp_host);
        gfx->print(":");
        gfx->print(pt_comp_port);
    }
}

static void drawAuthScreen() {
    gfx->fillScreen(C_BG);
    drawHeader();

    gfx->fillRoundRect(8, 36, 148, 54, 8, C_CARD);
    gfx->drawRoundRect(8, 36, 148, 54, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setTextSize(1);
    gfx->setCursor(18, 46);
    gfx->print("AUTH");
    gfx->setTextColor(comp_status.authorized ? C_GREEN : C_WHITE, C_CARD);
    gfx->setCursor(18, 60);
    gfx->print("authorized: ");
    gfx->print(comp_status.authorized ? "yes" : "no");
    gfx->setTextColor(comp_status.auth_pending ? C_YELLOW : C_WHITE, C_CARD);
    gfx->setCursor(18, 74);
    gfx->print("pending: ");
    gfx->print(comp_status.auth_pending ? "yes" : "no");

    gfx->fillRoundRect(164, 36, 148, 54, 8, C_CARD);
    gfx->drawRoundRect(164, 36, 148, 54, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(174, 46);
    gfx->print("DEVICE FLOW");
    gfx->setTextColor(C_WHITE, C_CARD);
    drawWrapped(strlen(comp_status.verification_url) ? comp_status.verification_url : "google.com/device",
                174, 60, 20, C_WHITE, C_CARD, 2);

    gfx->fillRoundRect(8, 98, 304, 58, 8, C_CARD);
    gfx->drawRoundRect(8, 98, 304, 58, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(18, 108);
    gfx->print("USER CODE");
    gfx->setTextColor(C_WHITE, C_CARD);
    gfx->setTextSize(3);
    gfx->setCursor(18, 124);
    gfx->print(strlen(comp_status.user_code) ? comp_status.user_code : "---");
    gfx->setTextSize(1);

    gfx->fillRoundRect(8, 164, 304, 36, 8, C_CARD);
    gfx->drawRoundRect(8, 164, 304, 36, 8, C_DIM);
    gfx->setTextColor(C_CYAN, C_CARD);
    gfx->setCursor(18, 174);
    gfx->print("DETAIL");
    if (strlen(comp_status.auth_error)) {
        drawWrapped(comp_status.auth_error, 64, 174, 40, C_ORANGE, C_CARD, 2);
    } else if (comp_status.authorized) {
        drawWrapped("Authorized. Companion will poll YouTube automatically.", 64, 174, 40, C_WHITE, C_CARD, 2);
    } else if (comp_status.auth_pending) {
        drawWrapped("Open google.com/device and enter the code above.", 64, 174, 40, C_WHITE, C_CARD, 2);
    } else {
        drawWrapped("Press START after saving client ID and secret in the web UI.", 64, 174, 40, C_WHITE, C_CARD, 2);
    }

    drawButton(btnStart);
    drawButton(btnClear);
    gfx->setTextColor(C_DIM, C_BG);
    gfx->setCursor(150, 216);
    gfx->print(pt_comp_host);
    gfx->print(":");
    gfx->print(pt_comp_port);
}

void setup() {
    Serial.begin(115200);
    pinMode(GFX_BL, OUTPUT);
    digitalWrite(GFX_BL, HIGH);
    gfx->begin();
    gfx->fillScreen(C_BG);
    gfx->setTextColor(C_CYAN, C_BG);
    gfx->setTextSize(2);
    gfx->setCursor(16, 90);
    gfx->print("YOUTUBE COMP CYD");
    gfx->setTextSize(1);
    gfx->setCursor(26, 118);
    gfx->print("Connecting to WiFi...");

    touchSPI.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
    ts.begin(touchSPI);
    ts.setRotation(1);

    ptConnect();
    ytCompFetchStatus(pt_comp_host, pt_comp_port);
    setAction("Connected");
    drawStatusScreen();
}

void loop() {
    unsigned long now = millis();

    if (now - last_fetch_ms >= 3000) {
        last_fetch_ms = now;
        ytCompFetchStatus(pt_comp_host, pt_comp_port);
        screen_dirty = true;
    }

    bool action_visible = g_action_until > now;
    if (action_visible != action_was_visible) {
        action_was_visible = action_visible;
        screen_dirty = true;
    }

    if (screen_dirty) {
        if (current_screen == SCREEN_AUTH) drawAuthScreen();
        else drawStatusScreen();
        screen_dirty = false;
    }

    bool touched = ts.tirqTouched() && ts.touched();
    if (touched && !touch_was_down) {
        TS_Point p = ts.getPoint();
        int sx, sy;
        mapTouch(p.x, p.y, sx, sy);

        if (inButton(btnStatus, sx, sy)) {
            current_screen = SCREEN_STATUS;
            screen_dirty = true;
        } else if (inButton(btnAuth, sx, sy)) {
            current_screen = SCREEN_AUTH;
            screen_dirty = true;
        } else if (inButton(btnRefresh, sx, sy)) {
            if (ytCompFetchStatus(pt_comp_host, pt_comp_port)) setAction("Refreshed");
            else setAction("Refresh failed");
        } else if (inButton(btnPortal, sx, sy)) {
            setAction("Opening portal...");
            delay(500);
            ptClearSettings();
            ESP.restart();
        } else if (current_screen == SCREEN_AUTH && inButton(btnStart, sx, sy)) {
            char msg[64];
            if (ytCompPostAction(pt_comp_host, pt_comp_port, "/auth/start", msg, sizeof(msg))) setAction(msg);
            else setAction(msg);
            ytCompFetchStatus(pt_comp_host, pt_comp_port);
        } else if (current_screen == SCREEN_AUTH && inButton(btnClear, sx, sy)) {
            char msg[64];
            if (ytCompPostAction(pt_comp_host, pt_comp_port, "/auth/clear", msg, sizeof(msg))) setAction("Token cleared");
            else setAction(msg);
            ytCompFetchStatus(pt_comp_host, pt_comp_port);
        }
    }

    touch_was_down = touched;
    delay(20);
}
