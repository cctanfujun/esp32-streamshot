// SPDX-License-Identifier: Apache-2.0
#include "control.h"

#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"

enum field { V, ID, SESSION, SEQ, OP, DX, DY, BUTTON, USAGE, MODIFIERS, TTL, FIELD_COUNT };
static const char *const names[] = {
    "v", "id", "session_id", "seq", "op", "dx", "dy", "button", "usage", "modifiers", "ttl_ms"
};
#define BIT(f) (1u << (f))
#define BASE (BIT(V) | BIT(ID) | BIT(SESSION) | BIT(SEQ) | BIT(OP))

static bool uuid(const cJSON *item, char output[SS_ID_SIZE])
{
    if (!cJSON_IsString(item) || strlen(item->valuestring) != 36) return false;
    for (size_t i = 0; i < 36; i++) {
        char c = item->valuestring[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (c != '-') return false;
        } else if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    memcpy(output, item->valuestring, SS_ID_SIZE);
    return true;
}

static bool integer(const cJSON *item, int minimum, int maximum, int *value)
{
    if (!cJSON_IsNumber(item) || !isfinite(item->valuedouble)
        || item->valuedouble < minimum || item->valuedouble > maximum
        || floor(item->valuedouble) != item->valuedouble) return false;
    *value = (int)item->valuedouble;
    return true;
}

bool ss_parse(const char *json, size_t length, ss_request_t *request)
{
    if (!json || !request || !length || length > SS_MAX_LINE) return false;
    memset(request, 0, sizeof(*request));
    // This protocol contains only fixed ASCII keys, UUIDs and operation names.
    // No string escapes/text: this also excludes decoded NUL ambiguity in cJSON.
    bool quoted = false;
    unsigned depth = 0;
    for (size_t i = 0; i < length; i++) {
        unsigned char c = (unsigned char)json[i];
        if (c == '\\' || c > 126 || (c < 32 && c != '\t' && c != '\r' && c != '\n')) return false;
        if (c == '"') quoted = !quoted;
        if (!quoted) {
            // Flat packets only: reject recursive JSON before cJSON can spend
            // an embedded task's stack parsing attacker-controlled nesting.
            if (c == '[' || c == ']') return false;
            if (c == '{' && ++depth > 1) return false;
            if (c == '}') { if (!depth) return false; depth--; }
        }
    }
    char line[SS_MAX_LINE + 1];
    memcpy(line, json, length);
    line[length] = 0;
    cJSON *object = cJSON_ParseWithLengthOpts(line, length + 1, NULL, true);
    if (!cJSON_IsObject(object)) { cJSON_Delete(object); return false; }
    const cJSON *fields[FIELD_COUNT] = {0};
    unsigned mask = 0, allowed = BASE;
    bool valid = true;
    cJSON *item;
    cJSON_ArrayForEach(item, object) {
        size_t field;
        for (field = 0; field < FIELD_COUNT; field++) {
            if (strcmp(names[field], item->string) == 0) break;
        }
        if (field == FIELD_COUNT || fields[field]) { valid = false; break; }
        fields[field] = item;
        mask |= BIT(field);
    }
    int number = 0;
    valid = valid && (mask & BASE) == BASE && integer(fields[V], 1, 1, &number)
        && uuid(fields[ID], request->id) && uuid(fields[SESSION], request->session_id)
        && integer(fields[SEQ], 1, INT_MAX, &number) && cJSON_IsString(fields[OP]);
    if (valid) request->seq = (uint32_t)number;
    if (valid) {
        const char *op = fields[OP]->valuestring;
        if (!strcmp(op, "status")) request->op = SS_STATUS;
        else if (!strcmp(op, "arm") || !strcmp(op, "heartbeat")) {
            request->op = !strcmp(op, "arm") ? SS_ARM : SS_HEARTBEAT;
            allowed |= BIT(TTL);
            valid = integer(fields[TTL], 1000, SS_MAX_LEASE_MS, &request->ttl_ms);
        } else if (!strcmp(op, "move")) {
            request->op = SS_MOVE;
            allowed |= BIT(DX) | BIT(DY);
            valid = integer(fields[DX], -2048, 2048, &request->dx)
                && integer(fields[DY], -2048, 2048, &request->dy);
        } else if (!strcmp(op, "click")) {
            request->op = SS_CLICK;
            allowed |= BIT(BUTTON);
            valid = cJSON_IsString(fields[BUTTON]);
            if (valid) {
                const char *button = fields[BUTTON]->valuestring;
                request->button = !strcmp(button, "left") ? 1 : !strcmp(button, "right") ? 2 : !strcmp(button, "middle") ? 4 : 0;
                valid = request->button != 0;
            }
        } else if (!strcmp(op, "scroll")) {
            request->op = SS_SCROLL;
            allowed |= BIT(DY);
            valid = integer(fields[DY], -16, 16, &request->dy);
        } else if (!strcmp(op, "key")) {
            request->op = SS_KEY;
            allowed |= BIT(USAGE) | BIT(MODIFIERS);
            valid = integer(fields[USAGE], 4, 231, &request->usage)
                && integer(fields[MODIFIERS], 0, 255, &request->modifiers);
        } else if (!strcmp(op, "release_all")) request->op = SS_RELEASE_ALL;
        else if (!strcmp(op, "stop")) request->op = SS_STOP;
        else valid = false;
    }
    valid = valid && (mask & ~allowed) == 0;
    cJSON_Delete(object);
    if (!valid) memset(request, 0, sizeof(*request));
    return valid;
}

bool ss_feed(ss_framer_t *framer, uint8_t byte, ss_request_t *request)
{
    if (byte == '\n') {
        bool valid = !framer->overflow && framer->length && ss_parse(framer->line, framer->length, request);
        framer->length = 0;
        framer->overflow = false;
        return valid;
    }
    if (!framer->overflow) {
        if (framer->length == SS_MAX_LINE) { framer->overflow = true; framer->length = 0; }
        else framer->line[framer->length++] = (char)byte;
    }
    return false;
}

void ss_init(ss_control_t *control, ss_io_t io, const char *boot_id)
{
    memset(control, 0, sizeof(*control));
    control->io = io;
    snprintf(control->boot_id, sizeof(control->boot_id), "%s", boot_id);
}

static bool neutral(ss_control_t *control)
{
    if (!control->connected) return true;
    bool mouse = control->io.mouse(control->io.context, 0, 0, 0, 0);
    bool keyboard = control->io.keyboard(control->io.context, 0, 0);
    return mouse && keyboard;
}

static bool latch(ss_control_t *control)
{
    bool released = neutral(control);
    control->armed = false;
    control->latched = true;
    control->expires_ms = 0;
    return released;
}

void ss_connection(ss_control_t *control, bool connected)
{
    latch(control);
    control->connected = connected;
    // On reconnection send neutral reports before accepting a new arm.
    if (connected) (void)neutral(control);
}

void ss_tick(ss_control_t *control)
{
    if (control->armed && control->io.now_ms(control->io.context) >= control->expires_ms) latch(control);
}

static bool same_request(const ss_request_t *a, const ss_request_t *b)
{
    return !strcmp(a->id, b->id) && !strcmp(a->session_id, b->session_id)
        && a->seq == b->seq && a->op == b->op && a->dx == b->dx && a->dy == b->dy
        && a->usage == b->usage && a->modifiers == b->modifiers && a->button == b->button
        && a->ttl_ms == b->ttl_ms;
}

static int8_t chunk(int value)
{
    return (int8_t)(value > 127 ? 127 : value < -127 ? -127 : value);
}

static bool ready(ss_control_t *control)
{
    ss_tick(control);
    return control->connected && control->armed;
}

ss_ack_t ss_execute(ss_control_t *control, const ss_request_t *request)
{
    ss_tick(control);
    ss_ack_t ack = {.seq = request->seq};
    memcpy(ack.id, request->id, SS_ID_SIZE);
    memcpy(ack.session_id, request->session_id, SS_ID_SIZE);
    for (size_t i = 0; i < SS_CACHE_SIZE; i++) {
        ss_cache_entry_t *entry = &control->cache[i];
        if (entry->used && !strcmp(entry->request.id, request->id)
            && !strcmp(entry->request.session_id, request->session_id)) {
            if (same_request(&entry->request, request)) ack = entry->ack;
            else ack.error = "id_conflict";
            goto complete;
        }
    }
    bool sent = true;
    if (request->op == SS_STATUS) {
        if (!strcmp(control->session_id, request->session_id) && request->seq <= control->last_seq) ack.error = "stale_seq";
        else {
            if (!strcmp(control->session_id, request->session_id)) control->last_seq = request->seq;
            ack.ok = true;
        }
    }
    else if (request->op == SS_RELEASE_ALL) {
        sent = neutral(control);
        ack.ok = sent;
        if (!strcmp(control->session_id, request->session_id) && request->seq > control->last_seq) control->last_seq = request->seq;
    } else if (request->op == SS_STOP) {
        latch(control);
        bool known = false;
        for (size_t i = 0; i < control->session_count; i++) {
            if (!strcmp(control->used_sessions[i], request->session_id)) known = true;
        }
        if (!known && control->session_count < SS_CACHE_SIZE) memcpy(control->used_sessions[control->session_count++], request->session_id, SS_ID_SIZE);
        ack.ok = true;
    } else if (request->op == SS_ARM) {
        if (!control->connected) ack.error = "not_connected";
        else {
            for (size_t i = 0; i < control->session_count; i++) {
                if (!strcmp(control->used_sessions[i], request->session_id)) { ack.error = "session_reused"; break; }
            }
            if (!ack.error && control->session_count == SS_CACHE_SIZE) ack.error = "session_limit";
            if (!ack.error) {
                sent = latch(control);
                if (!sent) { ack.error = "hid_failed"; goto record; }
                memcpy(control->session_id, request->session_id, SS_ID_SIZE);
                memcpy(control->used_sessions[control->session_count++], request->session_id, SS_ID_SIZE);
                control->last_seq = request->seq;
                control->latched = false;
                control->armed = true;
                control->expires_ms = control->io.now_ms(control->io.context) + (uint64_t)request->ttl_ms;
                ack.ok = true;
            }
        }
    } else if (strcmp(control->session_id, request->session_id)) ack.error = "wrong_session";
    else if (!control->connected) ack.error = "not_connected";
    else if (!control->armed || control->latched) ack.error = "not_armed";
    else if (request->seq <= control->last_seq) ack.error = "stale_seq";
    else {
        // Consume before attempting HID: an unknown/failed send is never retried.
        control->last_seq = request->seq;
        if (request->op == SS_HEARTBEAT) {
            control->expires_ms = control->io.now_ms(control->io.context) + (uint64_t)request->ttl_ms;
            ack.ok = true;
        } else if (request->op == SS_MOVE) {
            int dx = request->dx, dy = request->dy;
            do {
                int8_t x = chunk(dx), y = chunk(dy);
                if (!ready(control) || !control->io.mouse(control->io.context, 0, x, y, 0)) { sent = false; break; }
                dx -= x; dy -= y;
                control->io.sleep_ms(control->io.context, 10);
            } while (dx || dy);
            ack.ok = sent;
        } else if (request->op == SS_SCROLL) {
            sent = control->io.mouse(control->io.context, 0, 0, 0, (int8_t)request->dy);
            ack.ok = sent;
        } else if (request->op == SS_CLICK || request->op == SS_KEY) {
            if (request->op == SS_CLICK) sent = control->io.mouse(control->io.context, (uint8_t)request->button, 0, 0, 0);
            else sent = control->io.keyboard(control->io.context, (uint8_t)request->modifiers, (uint8_t)request->usage);
            control->io.sleep_ms(control->io.context, 20);
            // Release is attempted even when the press report fails.
            bool released = neutral(control);
            sent = sent && released;
            ack.ok = sent;
        }
    }
record:
    if (!sent) { latch(control); ack.error = "hid_failed"; }
    if (!ack.ok && !ack.error) ack.error = "invalid_request";
    ss_cache_entry_t *entry = &control->cache[control->cache_next++ % SS_CACHE_SIZE];
    entry->used = true;
    entry->request = *request;
    entry->ack = ack;
complete:
    ss_tick(control);
    memcpy(ack.boot_id, control->boot_id, SS_ID_SIZE);
    ack.connected = control->connected;
    ack.armed = control->armed && !strcmp(control->session_id, request->session_id);
    return ack;
}

bool ss_ack_json(const ss_ack_t *ack, char *output, size_t capacity)
{
    // IDs were validated before copying; error strings are fixed firmware codes.
    int written = snprintf(output, capacity,
        "{\"v\":1,\"id\":\"%s\",\"session_id\":\"%s\",\"boot_id\":\"%s\",\"seq\":%lu,\"ok\":%s,\"connected\":%s,\"armed\":%s%s%s%s}\n",
        ack->id, ack->session_id, ack->boot_id, (unsigned long)ack->seq, ack->ok ? "true" : "false",
        ack->connected ? "true" : "false", ack->armed ? "true" : "false",
        ack->error ? ",\"error\":\"" : "", ack->error ? ack->error : "", ack->error ? "\"" : "");
    return written > 0 && (size_t)written < capacity;
}
