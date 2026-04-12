// Status.h — poll YouTube-Pi /status and send simple control commands
#pragma once

#include <Arduino.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

struct YtStatus {
    bool api_online = false;
    bool running = false;
    uint32_t uptime_s = 0;
    int retries = 0;
    char cam_name[24] = "";
    char audio_name[24] = "";
    char error[128] = "";
    char rtmp_state[16] = "";
    bool eth_carrier = false;
    char eth_oper[16] = "";
    uint64_t tx_bytes = 0;
    uint64_t rx_bytes = 0;
    int tx_kbps = 0;
    int rx_kbps = 0;
    float temp_c = 0.0f;
    char throttled[16] = "";
    bool msg_enabled = false;
    char msg_text[121] = "";
    bool audio_silent = false;
    int start_cam_idx = 1;
};

static YtStatus yt_status;
static uint64_t _yt_prev_tx = 0;
static uint64_t _yt_prev_rx = 0;
static unsigned long _yt_prev_sample_ms = 0;

static bool ytFetchStatus(const char* host, uint16_t port) {
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%u/status", host, port);

    HTTPClient http;
    http.begin(url);
    http.setTimeout(3000);
    int code = http.GET();
    if (code != 200) {
        http.end();
        yt_status.api_online = false;
        return false;
    }

    String body = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, body)) {
        yt_status.api_online = false;
        return false;
    }

    yt_status.api_online = true;
    yt_status.running = doc["running"] | false;
    yt_status.uptime_s = doc["uptime_s"] | 0;
    yt_status.retries = doc["retries"] | 0;
    strlcpy(yt_status.cam_name, doc["cam_name"] | "", sizeof(yt_status.cam_name));
    strlcpy(yt_status.audio_name, doc["audio_name"] | "", sizeof(yt_status.audio_name));
    strlcpy(yt_status.error, doc["error"] | "", sizeof(yt_status.error));
    strlcpy(yt_status.rtmp_state, doc["rtmp_state"] | "", sizeof(yt_status.rtmp_state));
    yt_status.audio_silent = doc["audio_silent"] | false;

    JsonObject eth = doc["eth0"].as<JsonObject>();
    yt_status.eth_carrier = eth["carrier"] | false;
    strlcpy(yt_status.eth_oper, eth["operstate"] | "unknown", sizeof(yt_status.eth_oper));
    yt_status.tx_bytes = eth["tx_bytes"] | 0ULL;
    yt_status.rx_bytes = eth["rx_bytes"] | 0ULL;

    JsonObject sys = doc["system"].as<JsonObject>();
    yt_status.temp_c = sys["temp_c"] | 0.0f;
    strlcpy(yt_status.throttled, sys["throttled"] | "", sizeof(yt_status.throttled));

    JsonObject msg = doc["stream_message"].as<JsonObject>();
    yt_status.msg_enabled = msg["enabled"] | false;
    strlcpy(yt_status.msg_text, msg["text"] | "", sizeof(yt_status.msg_text));

    yt_status.start_cam_idx = 1;
    JsonArray cams = doc["available_cams"].as<JsonArray>();
    if (!cams.isNull() && cams.size() > 0) {
        yt_status.start_cam_idx = cams[0].as<int>();
        for (JsonVariant v : cams) {
            if (v.as<int>() == 1) {
                yt_status.start_cam_idx = 1;
                break;
            }
        }
    }

    unsigned long now = millis();
    if (_yt_prev_sample_ms > 0 && now > _yt_prev_sample_ms) {
        unsigned long dt = now - _yt_prev_sample_ms;
        uint64_t tx_delta = (yt_status.tx_bytes >= _yt_prev_tx) ? (yt_status.tx_bytes - _yt_prev_tx) : 0;
        uint64_t rx_delta = (yt_status.rx_bytes >= _yt_prev_rx) ? (yt_status.rx_bytes - _yt_prev_rx) : 0;
        yt_status.tx_kbps = (int)((tx_delta * 8ULL) / dt);
        yt_status.rx_kbps = (int)((rx_delta * 8ULL) / dt);
    } else {
        yt_status.tx_kbps = 0;
        yt_status.rx_kbps = 0;
    }
    _yt_prev_tx = yt_status.tx_bytes;
    _yt_prev_rx = yt_status.rx_bytes;
    _yt_prev_sample_ms = now;
    return true;
}

static bool ytPost(const char* host, uint16_t port, const char* path, const char* body) {
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%u%s", host, port, path);
    HTTPClient http;
    http.begin(url);
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    http.setTimeout(4000);
    int code = http.POST(body ? body : "");
    http.end();
    return code == 200;
}
