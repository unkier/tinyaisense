#include <stdlib.h>
#include <string.h>

#include "core.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "nvs_flash.h"

#define TAG "config"
#define NAMESPACE "tas"

static SemaphoreHandle_t lock;
static config_t current;

static void read_string(nvs_handle_t nvs, const char *key, char *out, size_t size,
                        const char *fallback) {
    size_t len = size;
    if (nvs_get_str(nvs, key, out, &len) != ESP_OK) strlcpy(out, fallback, size);
}

static void load(void) {
    config_t c = {0};
    nvs_handle_t nvs;
    bool open = nvs_open(NAMESPACE, NVS_READONLY, &nvs) == ESP_OK;
    if (open) {
        read_string(nvs, "ssid", c.ssid, sizeof c.ssid, CONFIG_TAS_WIFI_SSID);
        read_string(nvs, "pass", c.pass, sizeof c.pass, CONFIG_TAS_WIFI_PASSWORD);
        read_string(nvs, "hub", c.hub, sizeof c.hub, CONFIG_TAS_HUB);
        read_string(nvs, "token", c.token, sizeof c.token, "");
        nvs_close(nvs);
    } else {
        strlcpy(c.ssid, CONFIG_TAS_WIFI_SSID, sizeof c.ssid);
        strlcpy(c.pass, CONFIG_TAS_WIFI_PASSWORD, sizeof c.pass);
        strlcpy(c.hub, CONFIG_TAS_HUB, sizeof c.hub);
    }
    c.has_token = c.token[0] != 0;
    xSemaphoreTake(lock, portMAX_DELAY);
    current = c;
    xSemaphoreGive(lock);
}

void config_init(void) {
    lock = xSemaphoreCreateMutex();
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS needs erasing (%s)", esp_err_to_name(err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    load();
}

config_t config_get(void) {
    xSemaphoreTake(lock, portMAX_DELAY);
    config_t c = current;
    xSemaphoreGive(lock);
    return c;
}

// Sets (or, for NULL values, erases) keys, then reloads the settings.
static bool store(const char *const *keys, const char *const *values, size_t count) {
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NAMESPACE, NVS_READWRITE, &nvs);
    bool opened = err == ESP_OK;
    for (size_t i = 0; i < count && err == ESP_OK; i++) {
        if (values[i]) {
            err = nvs_set_str(nvs, keys[i], values[i]);
        } else {
            err = nvs_erase_key(nvs, keys[i]);
            if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
        }
    }
    if (err == ESP_OK) err = nvs_commit(nvs);
    if (opened) nvs_close(nvs);
    if (err != ESP_OK) ESP_LOGE(TAG, "storing %s failed: %s", keys[0], esp_err_to_name(err));
    load();
    return err == ESP_OK;
}

bool config_set_wifi(const char *ssid, const char *pass) {
    static const char *const keys[] = {"ssid", "pass"};
    const char *values[] = {ssid, pass};
    return store(keys, values, 2);
}

bool config_set_hub(const char *hub) {
    static const char *const keys[] = {"hub"};
    return store(keys, &hub, 1);
}

bool config_set_token(const char *token) {
    static const char *const keys[] = {"token"};
    return store(keys, &token, 1);
}

bool config_factory_reset(void) {
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NAMESPACE, NVS_READWRITE, &nvs);
    if (err == ESP_OK) {
        err = nvs_erase_all(nvs);
        if (err == ESP_OK) err = nvs_commit(nvs);
        nvs_close(nvs);
    }
    if (err != ESP_OK) ESP_LOGE(TAG, "factory reset failed: %s", esp_err_to_name(err));
    load();
    return err == ESP_OK;
}

bool config_parse_hub(const char *hub, char *host, size_t host_size, uint16_t *port) {
    const char *colon = strrchr(hub, ':');
    if (!colon || colon == hub || (size_t)(colon - hub) >= host_size) return false;
    char *end;
    unsigned long p = strtoul(colon + 1, &end, 10);
    if (!colon[1] || *end || p == 0 || p > 65535) return false;
    memcpy(host, hub, (size_t)(colon - hub));
    host[colon - hub] = 0;
    *port = (uint16_t)p;
    return true;
}

bool config_hub_valid(const char *hub) {
    char host[HUB_MAX + 1];
    uint16_t port;
    return !strcmp(hub, HUB_AUTO) || config_parse_hub(hub, host, sizeof host, &port);
}
