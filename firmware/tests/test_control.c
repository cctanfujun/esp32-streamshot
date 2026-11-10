// SPDX-License-Identifier: Apache-2.0
// Synthetic host-only protocol/HID safety tests. No real USB, BLE or user data.
#include "control.h"
#include "cJSON.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
static const char *B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
static const char *BOOT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
typedef struct {
    uint64_t time;
    int events, input_events, x, y, wheel, buttons, held_key;
    bool fail_press;
} fake_t;

static bool mouse(void *context, uint8_t buttons, int8_t x, int8_t y, int8_t wheel)
{
    fake_t *fake = context;
    assert(x >= -127 && y >= -127);
    fake->events++;
    fake->buttons = buttons;
    fake->x += x; fake->y += y; fake->wheel += wheel;
    if (buttons || x || y || wheel) {
        fake->input_events++;
        if (fake->fail_press) { fake->fail_press = false; return false; }
    }
    return true;
}
static bool keyboard(void *context, uint8_t modifiers, uint8_t usage)
{
    fake_t *fake = context;
    fake->events++;
    fake->held_key = modifiers || usage;
    if (fake->held_key) {
        fake->input_events++;
        if (fake->fail_press) { fake->fail_press = false; return false; }
    }
    return true;
}
static uint64_t clock_ms(void *context) { return ((fake_t *)context)->time; }
static void sleep_ms(void *context, uint32_t ms) { ((fake_t *)context)->time += ms; }
static void init(ss_control_t *control, fake_t *fake)
{
    memset(fake, 0, sizeof(*fake));
    ss_io_t io = {.context = fake, .mouse = mouse, .keyboard = keyboard, .now_ms = clock_ms, .sleep_ms = sleep_ms};
    ss_init(control, io, BOOT);
}
static ss_request_t request(ss_op_t op, unsigned id, uint32_t seq, const char *session)
{
    ss_request_t value = {.op = op, .seq = seq, .ttl_ms = SS_MAX_LEASE_MS};
    snprintf(value.id, sizeof(value.id), "00000000-0000-4000-8000-%012u", id);
    snprintf(value.session_id, sizeof(value.session_id), "%s", session);
    return value;
}
static void arm(ss_control_t *control, fake_t *fake)
{
    init(control, fake);
    ss_connection(control, true);
    ss_request_t value = request(SS_ARM, 1, 1, A);
    assert(ss_execute(control, &value).ok);
    assert(control->armed);
}
static bool packet(const char *op, const char *tail, ss_request_t *output)
{
    char json[SS_MAX_LINE + 1];
    int length = snprintf(json, sizeof(json),
        "{\"v\":1,\"id\":\"%s\",\"session_id\":\"%s\",\"seq\":1,\"op\":\"%s\"%s}", A, B, op, tail);
    assert(length > 0 && (size_t)length < sizeof(json));
    return ss_parse(json, (size_t)length, output);
}

static void parsing(void)
{
    ss_request_t value;
    assert(packet("move", ",\"dx\":-2048,\"dy\":2048", &value));
    assert(value.dx == -2048 && value.dy == 2048 && value.seq == 1);
    assert(packet("scroll", ",\"dy\":-16", &value));
    assert(packet("key", ",\"usage\":231,\"modifiers\":255", &value));
    assert(packet("click", ",\"button\":\"left\"", &value));
    assert(packet("arm", ",\"ttl_ms\":1000", &value));
    assert(packet("heartbeat", ",\"ttl_ms\":30000", &value));
    assert(packet("status", "", &value));
    assert(packet("release_all", "", &value));
    assert(packet("stop", "", &value));
    const char *bad[][2] = {
        {"move", ",\"dx\":2049,\"dy\":0"}, {"move", ",\"dx\":0"},
        {"move", ",\"dx\":0.5,\"dy\":0"}, {"move", ",\"dx\":true,\"dy\":0"},
        {"scroll", ",\"dy\":-17"}, {"key", ",\"usage\":3,\"modifiers\":0"},
        {"key", ",\"usage\":232,\"modifiers\":0"}, {"key", ",\"usage\":4,\"modifiers\":256"},
        {"key", ",\"usage\":4"}, {"click", ",\"button\":\"double\""},
        {"arm", ",\"ttl_ms\":999"}, {"heartbeat", ",\"ttl_ms\":30001"},
        {"arm", ""}, {"heartbeat", ""},
        {"status", ",\"v\":1"}, {"status", ",\"seq\":0"},
        {"status", ",\"seq\":2147483648"}, {"status", ",\"text\":\"private\""},
        {"status", ",\"dx\":0"}, {"shell", ""}, {"status", ",\"ttl_ms\":1000"},
        {"status", ",\"unknown\":{\"nested\":[[]]}"},
    };
    for (size_t i = 0; i < sizeof(bad)/sizeof(bad[0]); i++) assert(!packet(bad[i][0], bad[i][1], &value));
    assert(!ss_parse("[]", 2, &value));
    assert(!ss_parse("{}{}", 4, &value));
    assert(!ss_parse("{}\0", 3, &value));
    assert(!packet("sta\\u0074us", "", &value));
    char boundary[300];
    for (unsigned long seq = 2147483647UL; seq <= 2147483648UL; seq++) {
        snprintf(boundary, sizeof(boundary), "{\"v\":1,\"id\":\"%s\",\"session_id\":\"%s\",\"seq\":%lu,\"op\":\"status\"}", A, B, seq);
        assert(ss_parse(boundary, strlen(boundary), &value) == (seq == 2147483647UL));
    }
    snprintf(boundary, sizeof(boundary), "{\"v\":1,\"id\":\"%s\",\"session_id\":\"%s\",\"seq\":0,\"op\":\"status\"}", A, B);
    assert(!ss_parse(boundary, strlen(boundary), &value));
    char too_long[SS_MAX_LINE + 1];
    memset(too_long, ' ', sizeof(too_long));
    assert(!ss_parse(too_long, sizeof(too_long), &value));
    puts("parse bounds, exact fields, types, escapes, duplicate keys: passed");
}

static void framing(void)
{
    ss_framer_t framer = {0};
    ss_request_t value;
    for (size_t i = 0; i < SS_MAX_LINE + 20; i++) assert(!ss_feed(&framer, 'x', &value));
    assert(!ss_feed(&framer, '\n', &value));
    char json[300];
    snprintf(json, sizeof(json), "{\"v\":1,\"id\":\"%s\",\"session_id\":\"%s\",\"seq\":1,\"op\":\"status\"}\r\n", A, B);
    int parsed = 0;
    for (size_t i = 0; i < strlen(json); i++) if (ss_feed(&framer, (uint8_t)json[i], &value)) parsed++;
    assert(parsed == 1 && value.op == SS_STATUS);
    assert(framer.length == 0 && !framer.overflow);
    puts("overlong-line discard, fragmented NDJSON and resynchronization: passed");
}

static void unarmed_and_taps(void)
{
    ss_control_t control;
    fake_t fake;
    init(&control, &fake);
    ss_request_t value = request(SS_STATUS, 1, 1, A);
    ss_ack_t ack = ss_execute(&control, &value);
    assert(ack.ok && !ack.armed && !ack.connected && fake.events == 0);
    value = request(SS_KEY, 2, 2, A); value.usage = 4;
    assert(!ss_execute(&control, &value).ok && fake.events == 0);
    arm(&control, &fake);
    for (ss_op_t op = SS_CLICK; op <= SS_KEY; op++) {
        value = request(op, (unsigned)op + 10, (uint32_t)op + 10, A);
        value.button = 1; value.usage = 4; value.modifiers = 2; value.dy = 1;
        assert(ss_execute(&control, &value).ok);
        assert(fake.buttons == 0 && fake.held_key == 0);
    }
    char json[384];
    ack = ss_execute(&control, &(ss_request_t){.op=SS_STATUS, .seq=100,
        .id="00000000-0000-4000-8000-000000000100", .session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"});
    assert(ss_ack_json(&ack, json, sizeof(json)));
    cJSON *object = cJSON_Parse(json);
    assert(cJSON_GetObjectItem(object, "v")->valueint == 1);
    assert(!strcmp(cJSON_GetObjectItem(object, "boot_id")->valuestring, BOOT));
    assert(!cJSON_GetObjectItem(object, "op") && !cJSON_GetObjectItem(object, "usage"));
    cJSON_Delete(object);
    assert(!ss_ack_json(&ack, json, 8));
    puts("boot unarmed, status, tap auto-release and ack redaction: passed");
}

static void deduplication_and_sequence(void)
{
    ss_control_t control;
    fake_t fake;
    arm(&control, &fake);
    ss_request_t click = request(SS_CLICK, 2, 2, A); click.button = 1;
    assert(ss_execute(&control, &click).ok);
    int events = fake.events;
    assert(ss_execute(&control, &click).ok && fake.events == events);
    ss_request_t conflict = click; conflict.button = 2;
    assert(!strcmp(ss_execute(&control, &conflict).error, "id_conflict") && fake.events == events);
    for (unsigned i = 3; i < 80; i++) {
        ss_request_t heartbeat = request(SS_HEARTBEAT, i, i, A);
        assert(ss_execute(&control, &heartbeat).ok);
    }
    assert(fake.events == events);
    assert(!strcmp(ss_execute(&control, &click).error, "stale_seq") && fake.events == events);
    ss_request_t foreign = request(SS_CLICK, 90, 90, B); foreign.button = 1;
    assert(!strcmp(ss_execute(&control, &foreign).error, "wrong_session") && fake.events == events);
    puts("dedupe, payload conflict, cache eviction/old seq and wrong session: passed");
}

static void stop_disconnect_expiry_and_failure(void)
{
    ss_control_t control;
    fake_t fake;
    arm(&control, &fake);
    ss_request_t stop = request(SS_STOP, 2, 2, A);
    assert(ss_execute(&control, &stop).ok && !control.armed);
    ss_request_t reused = request(SS_ARM, 3, 3, A);
    assert(!strcmp(ss_execute(&control, &reused).error, "session_reused"));
    ss_request_t fresh = request(SS_ARM, 4, 1, B);
    assert(ss_execute(&control, &fresh).ok);
    // A cached arm for A cannot claim that A is armed while B is active.
    ss_request_t original_arm = request(SS_ARM, 1, 1, A);
    assert(!ss_execute(&control, &original_arm).armed);
    ss_connection(&control, false);
    assert(!control.armed && !control.connected && fake.buttons == 0 && fake.held_key == 0);
    ss_connection(&control, true);
    ss_request_t heartbeat = request(SS_HEARTBEAT, 5, 2, B);
    assert(!ss_execute(&control, &heartbeat).ok);
    arm(&control, &fake);
    fake.time = control.expires_ms;
    ss_tick(&control);
    assert(!control.armed && fake.buttons == 0 && fake.held_key == 0);
    heartbeat = request(SS_HEARTBEAT, 6, 2, A);
    assert(!ss_execute(&control, &heartbeat).ok);
    arm(&control, &fake);
    fake.fail_press = true;
    ss_request_t key = request(SS_KEY, 7, 2, A); key.usage = 4;
    assert(!strcmp(ss_execute(&control, &key).error, "hid_failed"));
    assert(!control.armed && fake.held_key == 0 && fake.buttons == 0);
    int events = fake.events;
    assert(!ss_execute(&control, &key).ok && fake.events == events);
    ss_request_t release = request(SS_RELEASE_ALL, 8, 1, B);
    assert(ss_execute(&control, &release).ok);
    init(&control, &fake); // A reboot loses dedupe but never restores arm.
    assert(!ss_execute(&control, &key).ok && fake.events == 0);
    // Stop is safe before any arm and still burns that session ID.
    ss_connection(&control, true);
    assert(ss_execute(&control, &stop).ok);
    reused = request(SS_ARM, 99, 3, A);
    assert(!strcmp(ss_execute(&control, &reused).error, "session_reused"));
    puts("stop latch/new session, disconnect/reconnect, expiry, failed press release, reboot: passed");
}

static void movement_and_deadline(void)
{
    ss_control_t control;
    fake_t fake;
    arm(&control, &fake);
    ss_request_t move = request(SS_MOVE, 2, 2, A); move.dx = 2048; move.dy = -2048;
    assert(ss_execute(&control, &move).ok);
    assert(fake.x == 2048 && fake.y == -2048 && fake.buttons == 0);
    arm(&control, &fake);
    fake.time = control.expires_ms - 5;
    assert(!ss_execute(&control, &move).ok);
    assert(fake.x > 0 && fake.x < 2048 && !control.armed && fake.buttons == 0);
    puts("bounded relative reports and deadline checked during movement: passed");
}

int main(void)
{
    parsing(); framing(); unarmed_and_taps(); deduplication_and_sequence();
    stop_disconnect_expiry_and_failure(); movement_and_deadline();
    puts("All 6 host safety suites passed (ASan + UBSan).");
    return 0;
}
