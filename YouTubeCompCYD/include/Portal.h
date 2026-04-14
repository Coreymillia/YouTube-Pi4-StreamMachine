// Portal.h — WiFi captive portal + NVS settings for YouTubeCompCYD
#pragma once

#include <Arduino.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#define PT_DEFAULT_COMP_ADDR "192.168.0.129"
#define PT_DEFAULT_COMP_PORT 8091

static char     pt_wifi_ssid[64] = "";
static char     pt_wifi_pass[64] = "";
static char     pt_comp_host[64] = PT_DEFAULT_COMP_ADDR;
static uint16_t pt_comp_port     = PT_DEFAULT_COMP_PORT;
static bool     pt_has_settings  = false;

static Preferences _pt_prefs;
static WebServer   _pt_server(80);
static DNSServer   _pt_dns;

static void ptLoadSettings() {
    _pt_prefs.begin("ytcompcyd", true);
    pt_has_settings = _pt_prefs.getBool("configured", false);
    strlcpy(pt_wifi_ssid, _pt_prefs.getString("ssid", "").c_str(), sizeof(pt_wifi_ssid));
    strlcpy(pt_wifi_pass, _pt_prefs.getString("wpass", "").c_str(), sizeof(pt_wifi_pass));
    strlcpy(pt_comp_host, _pt_prefs.getString("host", PT_DEFAULT_COMP_ADDR).c_str(), sizeof(pt_comp_host));
    pt_comp_port = (uint16_t)_pt_prefs.getUInt("port", PT_DEFAULT_COMP_PORT);
    _pt_prefs.end();
}

static void ptSaveSettings(const char* ssid, const char* wpass, const char* host, uint16_t port) {
    _pt_prefs.begin("ytcompcyd", false);
    _pt_prefs.putBool("configured", true);
    _pt_prefs.putString("ssid", ssid);
    _pt_prefs.putString("wpass", wpass);
    _pt_prefs.putString("host", host);
    _pt_prefs.putUInt("port", port);
    _pt_prefs.end();
    strlcpy(pt_wifi_ssid, ssid, sizeof(pt_wifi_ssid));
    strlcpy(pt_wifi_pass, wpass, sizeof(pt_wifi_pass));
    strlcpy(pt_comp_host, host, sizeof(pt_comp_host));
    pt_comp_port = port;
    pt_has_settings = true;
}

static void ptClearSettings() {
    _pt_prefs.begin("ytcompcyd", false);
    _pt_prefs.clear();
    _pt_prefs.end();
    pt_has_settings = false;
    pt_wifi_ssid[0] = '\0';
    pt_wifi_pass[0] = '\0';
    strlcpy(pt_comp_host, PT_DEFAULT_COMP_ADDR, sizeof(pt_comp_host));
    pt_comp_port = PT_DEFAULT_COMP_PORT;
}

static bool ptProbeApiReachable(uint8_t attempts = 5, uint16_t delay_ms = 1000) {
    char url[160];
    snprintf(url, sizeof(url), "http://%s:%u/status", pt_comp_host, pt_comp_port);
    for (uint8_t attempt = 0; attempt < attempts; attempt++) {
        HTTPClient http;
        http.begin(url);
        http.setTimeout(2500);
        int code = http.GET();
        http.end();
        if (code == 200) return true;
        delay(delay_ms);
    }
    return false;
}

static const char PT_HTML[] PROGMEM = R"html(
<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:monospace;background:#0d0d0d;color:#6ee7ff;padding:20px}
h2{color:#6ee7ff;border-bottom:1px solid #333;padding-bottom:8px}
label{display:block;margin-top:14px;color:#aaa;font-size:13px}
input{width:100%;padding:8px;margin:4px 0;box-sizing:border-box;
      background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:4px;
      font-family:monospace;font-size:14px}
button{margin-top:20px;width:100%;padding:12px;background:#11334a;color:#d8f8ff;
       border:1px solid #3aa3d1;border-radius:6px;font-size:16px;cursor:pointer;
       font-family:monospace}
</style></head><body>
<h2>YouTubeCompCYD Setup</h2>
<form method="POST" action="/save">
<label>WiFi SSID</label>
<input name="ssid" value="{SSID}">
<label>WiFi Password</label>
<input name="wpass" type="password">
<label>YouTube Companion Address / Hostname</label>
<input name="host" value="{HOST}">
<label>YouTube Companion Port</label>
<input name="port" type="number" value="{PORT}">
<button type="submit">Save and Connect</button>
</form></body></html>
)html";

static void _ptHandleRoot() {
    String page = PT_HTML;
    page.replace("{SSID}", pt_wifi_ssid);
    page.replace("{HOST}", pt_comp_host);
    char port_buf[8];
    snprintf(port_buf, sizeof(port_buf), "%u", pt_comp_port);
    page.replace("{PORT}", port_buf);
    _pt_server.send(200, "text/html", page);
}

static void _ptHandleSave() {
    String ssid = _pt_server.arg("ssid");
    String wpass = _pt_server.arg("wpass");
    String host = _pt_server.arg("host");
    String port = _pt_server.arg("port");
    ptSaveSettings(ssid.c_str(), wpass.c_str(), host.c_str(), (uint16_t)port.toInt());
    _pt_server.send(
        200,
        "text/html",
        "<html><body style='background:#0d0d0d;color:#6ee7ff;font-family:monospace;padding:20px'>"
        "<h2>Saved. Rebooting...</h2></body></html>"
    );
    delay(1500);
    ESP.restart();
}

static void ptRunPortal() {
    WiFi.mode(WIFI_AP);
    WiFi.softAP("YouTubeCompCYD-Setup");
    _pt_dns.start(53, "*", WiFi.softAPIP());
    _pt_server.on("/", _ptHandleRoot);
    _pt_server.on("/save", HTTP_POST, _ptHandleSave);
    _pt_server.onNotFound(_ptHandleRoot);
    _pt_server.begin();
    while (true) {
        _pt_dns.processNextRequest();
        _pt_server.handleClient();
        delay(2);
    }
}

static bool ptConnect() {
    ptLoadSettings();
    if (!pt_has_settings || strlen(pt_wifi_ssid) == 0) ptRunPortal();
    WiFi.mode(WIFI_STA);
    WiFi.begin(pt_wifi_ssid, pt_wifi_pass);
    unsigned long started = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
        delay(250);
    }
    if (WiFi.status() != WL_CONNECTED) ptRunPortal();
    if (!ptProbeApiReachable()) ptRunPortal();
    return true;
}
