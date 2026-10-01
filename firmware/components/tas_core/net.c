#include <string.h>

#include "core.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "lwip/netdb.h"
#include "mdns.h"

#define TAG "net"
#define UP_BIT BIT0
#define MDNS_TIMEOUT_MS 3000
#define MDNS_MANY 4 // hubs to hear from when the first to answer isn't ours

static EventGroupHandle_t events;
static esp_netif_t *netif;

// Joins the configured WiFi, if there is one.
static void join(void) {
    config_t c = config_get();
    status_set_wifi(c.ssid[0] != 0, false);
    if (!c.ssid[0]) return;
    // Neither field needs a NUL: a 32-byte SSID or a 64-digit key fills it.
    wifi_config_t wc = {0};
    memcpy(wc.sta.ssid, c.ssid, strnlen(c.ssid, sizeof wc.sta.ssid));
    memcpy(wc.sta.password, c.pass, strnlen(c.pass, sizeof wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    esp_err_t err = esp_wifi_connect();
    if (err) ESP_LOGW(TAG, "connect: %s", esp_err_to_name(err));
}

static void went_down(void) {
    xEventGroupClearBits(events, UP_BIT);
    status_set_wifi(config_get().ssid[0] != 0, false);
    ctrl_kick(CTRL_KICK_WIFI_DOWN);
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        join();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *e = data;
        ESP_LOGW(TAG, "WiFi lost, reason %d", e->reason);
        went_down();
        // Keep trying. A failed attempt ends in another disconnect, a few seconds later.
        if (config_get().ssid[0]) esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "WiFi up, " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(events, UP_BIT);
        status_set_wifi(true, true);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
        went_down();
    }
}

void net_start(void) {
    events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    netif = esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    // The default modem sleep batches receives to the DTIM period: tens of ms of latency.
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(mdns_init());
}

bool net_up(void) {
    return xEventGroupGetBits(events) & UP_BIT;
}

int net_rssi(void) {
    wifi_ap_record_t ap;
    return esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
}

void net_ip(char *buf, size_t size) {
    esp_netif_ip_info_t info = {0};
    esp_netif_get_ip_info(netif, &info);
    snprintf(buf, size, IPSTR, IP2STR(&info.ip));
}

static bool static_hub(const char *setting, struct sockaddr_in *hub) {
    char host[HUB_MAX + 1];
    uint16_t port;
    if (!config_parse_hub(setting, host, sizeof host, &port)) {
        ESP_LOGE(TAG, "hub \"%s\" isn't host:port", setting);
        return false;
    }
    *hub = (struct sockaddr_in){.sin_family = AF_INET, .sin_port = htons(port)};
    if (inet_aton(host, &hub->sin_addr)) return true;
    // A name: lwIP's DNS gives up on each server after about 7 s, past the watchdog's 10 s if
    // there are two. It always gives up, so the caller's watchdog can sit this one out.
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM}, *found = NULL;
    esp_task_wdt_delete(NULL);
    int err = getaddrinfo(host, NULL, &hints, &found);
    esp_task_wdt_add(NULL);
    if (err || !found) {
        ESP_LOGW(TAG, "can't resolve %s", host);
        return false;
    }
    hub->sin_addr = ((struct sockaddr_in *)found->ai_addr)->sin_addr;
    freeaddrinfo(found);
    return true;
}

static bool our_major(const mdns_result_t *r) {
    char major[8];
    snprintf(major, sizeof major, "%d", TAS_PROTOCOL);
    for (size_t i = 0; i < r->txt_count; i++) {
        if (!strcmp(r->txt[i].key, "proto")) {
            return r->txt[i].value && !strcmp(r->txt[i].value, major);
        }
    }
    return false;
}

// Asks for hubs until `max` answer or the timeout. Whether one of ours did, in `hub`.
static bool mdns_hub(struct sockaddr_in *hub, size_t max, bool *answered) {
    mdns_result_t *results = NULL;
    esp_err_t err = mdns_query_ptr("_tinyaisense", "_tcp", MDNS_TIMEOUT_MS, max, &results);
    core_breathe();
    if (err) ESP_LOGW(TAG, "mDNS query: %s", esp_err_to_name(err));
    *answered = results != NULL;
    bool found = false;
    for (const mdns_result_t *r = results; r && !found; r = r->next) {
        if (!our_major(r) || !r->port) continue;
        *hub = (struct sockaddr_in){.sin_family = AF_INET, .sin_port = htons(r->port)};
        for (const mdns_ip_addr_t *a = r->addr; a && !found; a = a->next) {
            if (a->addr.type != ESP_IPADDR_TYPE_V4) continue;
            hub->sin_addr.s_addr = a->addr.u_addr.ip4.addr;
            found = true;
        }
        esp_ip4_addr_t ip;
        if (!found && r->hostname && mdns_query_a(r->hostname, MDNS_TIMEOUT_MS, &ip) == ESP_OK) {
            hub->sin_addr.s_addr = ip.addr;
            found = true;
        }
        core_breathe();
    }
    mdns_query_results_free(results);
    return found;
}

// The first hub found whose `proto` is ours. The query ends as soon as one hub answers, so it's
// quick; only if that one isn't ours does it ask again, to hear from more.
static bool find_over_mdns(struct sockaddr_in *hub) {
    bool answered;
    if (mdns_hub(hub, 1, &answered)) return true;
    if (answered && mdns_hub(hub, MDNS_MANY, &answered)) return true;
    ESP_LOGW(TAG, "no hub%s found over mDNS", answered ? " of our protocol" : "");
    return false;
}

bool net_find_hub(struct sockaddr_in *hub) {
    config_t c = config_get();
    bool found = strcmp(c.hub, HUB_AUTO) ? static_hub(c.hub, hub) : find_over_mdns(hub);
    if (found) {
        char ip[16];
        inet_ntoa_r(hub->sin_addr, ip, sizeof ip);
        ESP_LOGI(TAG, "hub at %s:%u", ip, ntohs(hub->sin_port));
    }
    return found;
}
