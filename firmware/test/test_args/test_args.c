// Reading a stream's `format` arg (docs/protocol/audio.md#format), as a mic or speaker start does.

#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "tas_wire.h"

static const uint32_t FRAME_MS[] = {10, 20}; // what the Atom Echo advertises

static tas_arena_t arena;

// `start` args holding `format`, read for a 16 kHz capability.
static tas_arg_t read_format(const char *format, uint32_t *frame_ms) {
    char json[256];
    snprintf(json, sizeof json, "{\"stream\":3,\"format\":%s}", format);
    tas_json_t *args;
    TEST_ASSERT_EQUAL_INT(TAS_JSON_OK,
                          tas_json_parse((const uint8_t *)json, strlen(json), &arena, &args));
    return tas_arg_format(args, "format", 16000, FRAME_MS, 2, frame_ms);
}

void setUp(void) {}

void tearDown(void) {
    tas_arena_free(&arena);
}

static void test_an_advertised_format_gives_its_frame_duration(void) {
    uint32_t frame_ms = 0;

    TEST_ASSERT_EQUAL_INT(
        TAS_ARG_OK,
        read_format("{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":1,\"frame_ms\":20}",
                    &frame_ms));

    TEST_ASSERT_EQUAL_UINT32(20, frame_ms);
}

static void test_a_format_the_capability_doesnt_advertise_is_bad(void) {
    static const char *const formats[] = {
        "{\"codec\":\"opus\",\"rate\":16000,\"channels\":1,\"frame_ms\":10}",
        "{\"codec\":\"pcm_s16le\",\"rate\":24000,\"channels\":1,\"frame_ms\":10}",
        "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":2,\"frame_ms\":10}",
        "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":1,\"frame_ms\":5}",
    };
    for (size_t i = 0; i < sizeof formats / sizeof *formats; i++) {
        uint32_t frame_ms;
        TEST_ASSERT_EQUAL_INT_MESSAGE(TAS_ARG_BAD, read_format(formats[i], &frame_ms), formats[i]);
    }
}

static void test_a_format_that_breaks_the_contract_is_bad(void) {
    static const char *const formats[] = {
        "\"pcm_s16le\"",
        "{\"rate\":16000,\"channels\":1,\"frame_ms\":10}",
        "{\"codec\":\"pcm_s16le\",\"rate\":16000.0,\"channels\":1,\"frame_ms\":10}",
        "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":true,\"frame_ms\":10}",
        "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":1,\"frame_ms\":[10]}",
        "{\"codec\":1,\"rate\":16000,\"channels\":1,\"frame_ms\":10}",
    };
    for (size_t i = 0; i < sizeof formats / sizeof *formats; i++) {
        uint32_t frame_ms;
        TEST_ASSERT_EQUAL_INT_MESSAGE(TAS_ARG_BAD, read_format(formats[i], &frame_ms), formats[i]);
    }
}

static void test_unknown_members_are_ignored_and_a_missing_format_is_absent(void) {
    uint32_t frame_ms;
    TEST_ASSERT_EQUAL_INT(TAS_ARG_OK,
                          read_format("{\"frame_ms\":10,\"codec\":\"pcm_s16le\",\"channels\":1,"
                                      "\"rate\":16000,\"x\":null}",
                                      &frame_ms));

    tas_json_t *args;
    TEST_ASSERT_EQUAL_INT(TAS_JSON_OK, tas_json_parse((const uint8_t *)"{}", 2, &arena, &args));
    TEST_ASSERT_EQUAL_INT(TAS_ARG_ABSENT, tas_arg_format(args, "format", 16000, FRAME_MS, 2,
                                                         &frame_ms));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_an_advertised_format_gives_its_frame_duration);
    RUN_TEST(test_a_format_the_capability_doesnt_advertise_is_bad);
    RUN_TEST(test_a_format_that_breaks_the_contract_is_bad);
    RUN_TEST(test_unknown_members_are_ignored_and_a_missing_format_is_absent);
    return UNITY_END();
}
