// Status.h — poll YouTubeCompanion status and optional auth actions
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>

struct CompanionStatus {
    bool api_online = false;
    bool authorized = false;
    bool auth_pending = false;
    uint32_t updated_at = 0;
    char api_error[128] = "";
    char auth_error[96] = "";
    char user_code[24] = "";
    char verification_url[64] = "";
    char broadcast_title[72] = "";
    char life_cycle[20] = "";
    char privacy_status[20] = "";
    char started_at[32] = "";
    char ended_at[32] = "";
    char stream_status[20] = "";
    char health_status[20] = "";
    char resolution[16] = "";
    char frame_rate[16] = "";
    int issue_count = 0;
    char issue_type[32] = "";
    char issue_text[128] = "";
};

static CompanionStatus comp_status;

static bool _ytCompGetJson(const char* host, uint16_t port, const char* path, JsonDocument& doc, int timeout_ms = 3500) {
    char url[160];
    snprintf(url, sizeof(url), "http://%s:%u%s", host, port, path);
    HTTPClient http;
    http.begin(url);
    http.setTimeout(timeout_ms);
    int code = http.GET();
    if (code != 200) {
        http.end();
        return false;
    }

    String body = http.getString();
    http.end();
    return !deserializeJson(doc, body);
}

static bool ytCompFetchStatus(const char* host, uint16_t port) {
    JsonDocument status_doc;
    JsonDocument auth_doc;

    bool got_status = _ytCompGetJson(host, port, "/status", status_doc);
    bool got_auth = _ytCompGetJson(host, port, "/auth_status", auth_doc);

    comp_status.api_online = got_status || got_auth;
    if (!comp_status.api_online) {
        strlcpy(comp_status.api_error, "Companion offline", sizeof(comp_status.api_error));
        return false;
    }

    if (got_status) {
        comp_status.authorized = status_doc["authorized"] | false;
        comp_status.updated_at = status_doc["updated_at"] | 0;
        strlcpy(comp_status.api_error, status_doc["error"] | "", sizeof(comp_status.api_error));

        JsonObject broadcast = status_doc["broadcast"].as<JsonObject>();
        strlcpy(comp_status.broadcast_title, broadcast["title"] | "", sizeof(comp_status.broadcast_title));
        strlcpy(comp_status.life_cycle, broadcast["life_cycle_status"] | "", sizeof(comp_status.life_cycle));
        strlcpy(comp_status.privacy_status, broadcast["privacy_status"] | "", sizeof(comp_status.privacy_status));
        strlcpy(comp_status.started_at, broadcast["actual_start_time"] | "", sizeof(comp_status.started_at));
        strlcpy(comp_status.ended_at, broadcast["actual_end_time"] | "", sizeof(comp_status.ended_at));

        JsonObject stream = status_doc["stream"].as<JsonObject>();
        strlcpy(comp_status.stream_status, stream["stream_status"] | "", sizeof(comp_status.stream_status));
        strlcpy(comp_status.health_status, stream["health_status"] | "", sizeof(comp_status.health_status));
        strlcpy(comp_status.resolution, stream["resolution"] | "", sizeof(comp_status.resolution));
        strlcpy(comp_status.frame_rate, stream["frame_rate"] | "", sizeof(comp_status.frame_rate));

        comp_status.issue_count = 0;
        comp_status.issue_type[0] = '\0';
        comp_status.issue_text[0] = '\0';
        JsonArray issues = status_doc["issues"].as<JsonArray>();
        if (!issues.isNull()) {
            comp_status.issue_count = (int)issues.size();
            if (comp_status.issue_count > 0) {
                JsonObject issue = issues[0].as<JsonObject>();
                strlcpy(comp_status.issue_type, issue["type"] | "", sizeof(comp_status.issue_type));
                const char* desc = issue["description"] | issue["reason"] | "";
                strlcpy(comp_status.issue_text, desc, sizeof(comp_status.issue_text));
            }
        }
    }

    if (got_auth) {
        comp_status.auth_pending = auth_doc["pending"] | false;
        strlcpy(comp_status.auth_error, auth_doc["error"] | "", sizeof(comp_status.auth_error));
        strlcpy(comp_status.user_code, auth_doc["user_code"] | "", sizeof(comp_status.user_code));
        strlcpy(comp_status.verification_url, auth_doc["verification_url"] | "", sizeof(comp_status.verification_url));
        if (!got_status) {
            comp_status.authorized = auth_doc["authorized"] | false;
        }
    }

    return true;
}

static bool ytCompPostAction(const char* host, uint16_t port, const char* path, char* msg, size_t msg_sz) {
    char url[160];
    snprintf(url, sizeof(url), "http://%s:%u%s", host, port, path);

    HTTPClient http;
    http.begin(url);
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    http.setTimeout(4000);
    int code = http.POST("");
    if (code != 200) {
        http.end();
        if (msg && msg_sz) strlcpy(msg, "Request failed", msg_sz);
        return false;
    }

    String body = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, body)) {
        if (msg && msg_sz) strlcpy(msg, "Bad response", msg_sz);
        return false;
    }

    bool ok = doc["ok"] | false;
    const char* reply = doc["msg"] | (ok ? "OK" : "Failed");
    if (msg && msg_sz) strlcpy(msg, reply, msg_sz);
    return ok;
}
