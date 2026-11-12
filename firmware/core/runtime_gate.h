// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <limits.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

// Callbacks must not acquire a control lock or call a blocking HID API.
// Keep the generation and usable flag in one lock-free publication.
_Static_assert(UINT_MAX == UINT32_MAX && ATOMIC_INT_LOCK_FREE == 2,
               "Runtime gates require lock-free 32-bit unsigned atomics");
typedef struct { atomic_uint word; } ss_link_mailbox_t;

void ss_link_init(ss_link_mailbox_t *link);
void ss_link_publish(ss_link_mailbox_t *link, bool usable);
bool ss_link_take(const ss_link_mailbox_t *link, unsigned *applied, bool *usable);
bool ss_link_current(const ss_link_mailbox_t *link, unsigned applied);

#define SS_PROGRESS_TIMEOUT_MS 3000u
typedef struct {
    atomic_uint completed_ms, started;
    // Only the independent observer accesses these debugger-grace fields.
    unsigned debug_ms;
    bool debug_grace;
} ss_progress_guard_t;

void ss_progress_init(ss_progress_guard_t *guard);
void ss_progress_complete(ss_progress_guard_t *guard, unsigned now_ms);
bool ss_progress_stalled(ss_progress_guard_t *guard, unsigned now_ms, bool debugger_attached);
