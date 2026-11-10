// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SS_MAX_LINE 512
#define SS_ID_SIZE 37
#define SS_CACHE_SIZE 64
#define SS_MAX_LEASE_MS 30000

typedef enum {
    SS_STATUS, SS_ARM, SS_HEARTBEAT, SS_MOVE, SS_CLICK, SS_SCROLL, SS_KEY,
    SS_RELEASE_ALL, SS_STOP
} ss_op_t;

typedef struct {
    char id[SS_ID_SIZE];
    char session_id[SS_ID_SIZE];
    uint32_t seq;
    ss_op_t op;
    int dx, dy, usage, modifiers, button, ttl_ms;
} ss_request_t;

typedef struct {
    char id[SS_ID_SIZE];
    char session_id[SS_ID_SIZE];
    char boot_id[SS_ID_SIZE];
    uint32_t seq;
    bool ok, connected, armed;
    const char *error;
} ss_ack_t;

typedef struct {
    void *context;
    bool (*mouse)(void *, uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel);
    bool (*keyboard)(void *, uint8_t modifiers, uint8_t usage);
    void (*sleep_ms)(void *, uint32_t milliseconds);
    uint64_t (*now_ms)(void *);
} ss_io_t;

typedef struct {
    bool used;
    ss_request_t request;
    ss_ack_t ack;
} ss_cache_entry_t;

typedef struct {
    bool connected, armed, latched;
    char session_id[SS_ID_SIZE];
    char boot_id[SS_ID_SIZE];
    uint32_t last_seq;
    uint64_t expires_ms;
    size_t cache_next;
    size_t session_count;
    char used_sessions[SS_CACHE_SIZE][SS_ID_SIZE];
    ss_cache_entry_t cache[SS_CACHE_SIZE];
    ss_io_t io;
} ss_control_t;

typedef struct {
    char line[SS_MAX_LINE];
    size_t length;
    bool overflow;
} ss_framer_t;

// No input strings/content appear in errors. Callers serialize access to state.
bool ss_parse(const char *json, size_t length, ss_request_t *request);
bool ss_feed(ss_framer_t *framer, uint8_t byte, ss_request_t *request);
void ss_init(ss_control_t *control, ss_io_t io, const char *boot_id);
void ss_connection(ss_control_t *control, bool connected);
void ss_tick(ss_control_t *control);
ss_ack_t ss_execute(ss_control_t *control, const ss_request_t *request);
bool ss_ack_json(const ss_ack_t *ack, char *output, size_t capacity);
