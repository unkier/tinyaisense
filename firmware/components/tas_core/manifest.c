#include <stdio.h>
#include <stdlib.h>

#include "core.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "tas_kinds.h"

static tas_arena_t arena; // lives as long as the firmware runs
static tas_manifest_t manifest;
static char device_id[13];

static tas_json_t *exclusive_groups(const tas_board_t *board) {
    tas_json_t *groups = tas_json_array(&arena);
    for (size_t g = 0; g < board->exclusive_count; g++) {
        tas_json_t *ids = tas_json_array(&arena);
        for (size_t i = 0; i < board->exclusive[g].count; i++) {
            ids = tas_json_push(ids, tas_json_string(&arena, board->exclusive[g].ids[i]));
        }
        groups = tas_json_push(groups, ids);
    }
    return groups;
}

const tas_manifest_t *manifest_build(const tas_board_t *board) {
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_efuse_mac_get_default(mac));
    snprintf(device_id, sizeof device_id, "%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
             mac[3], mac[4], mac[5]);
    tas_wire_cap_t *caps = tas_arena_alloc(&arena, board->cap_count * sizeof *caps);
    if (!caps) abort();
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_kind_module_t *module = tas_kind_module(board->caps[i].kind);
        caps[i] = (tas_wire_cap_t){
            .id = tas_str(board->caps[i].id),
            .kind = tas_str(module->name),
            .params = module->params ? module->params(board, &board->caps[i], &arena) : NULL,
        };
        if (module->params && !caps[i].params) abort();
    }
    manifest = (tas_manifest_t){
        .protocol = TAS_PROTOCOL,
        .device_id = tas_str(device_id),
        .board = tas_str(board->name),
        .firmware = tas_str(esp_app_get_description()->version),
        .caps = caps,
        .cap_count = board->cap_count,
        .exclusive = exclusive_groups(board),
    };
    if (!manifest.exclusive) abort();
    ESP_LOGI("manifest", "device %s, firmware %s", device_id, manifest.firmware.ptr);
    return &manifest;
}
