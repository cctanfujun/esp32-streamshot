// SPDX-License-Identifier: Apache-2.0
#include "runtime_gate.h"

void ss_link_init(ss_link_mailbox_t *link) { atomic_init(&link->word, 0); }

void ss_link_publish(ss_link_mailbox_t *link, bool usable)
{
    unsigned old = atomic_load_explicit(&link->word, memory_order_relaxed);
    unsigned next;
    do {
        unsigned epoch = old >> 1;
        // Reserve the final generation as permanently unusable. Never wrap an
        // epoch back onto a still-applied lease, even after an extreme run.
        next = epoch >= (UINT_MAX >> 1) - 1 ? UINT_MAX - 1
            : ((epoch + 1) << 1) | (usable ? 1u : 0u);
    } while (!atomic_compare_exchange_weak_explicit(&link->word, &old, next,
                                                  memory_order_release, memory_order_relaxed));
}

bool ss_link_take(const ss_link_mailbox_t *link, unsigned *applied, bool *usable)
{
    unsigned word = atomic_load_explicit(&link->word, memory_order_acquire);
    if (word == *applied) return false;
    *applied = word;
    *usable = (word & 1u) != 0;
    return true;
}

bool ss_link_current(const ss_link_mailbox_t *link, unsigned applied)
{
    return (applied & 1u) && atomic_load_explicit(&link->word, memory_order_acquire) == applied;
}

void ss_progress_init(ss_progress_guard_t *guard)
{
    atomic_init(&guard->completed_ms, 0);
    atomic_init(&guard->started, 0);
    guard->debug_ms = 0;
    guard->debug_grace = false;
}

void ss_progress_complete(ss_progress_guard_t *guard, unsigned now_ms)
{
    atomic_store_explicit(&guard->completed_ms, now_ms, memory_order_release);
    atomic_store_explicit(&guard->started, 1, memory_order_release);
}

bool ss_progress_stalled(ss_progress_guard_t *guard, unsigned now_ms, bool debugger_attached)
{
    if (debugger_attached) {
        guard->debug_ms = now_ms;
        guard->debug_grace = true;
        return false;
    }
    if (guard->debug_grace) {
        if ((unsigned)(now_ms - guard->debug_ms) < SS_PROGRESS_TIMEOUT_MS) return false;
        guard->debug_grace = false;
    }
    if (!atomic_load_explicit(&guard->started, memory_order_acquire)) return false;
    unsigned completed = atomic_load_explicit(&guard->completed_ms, memory_order_acquire);
    unsigned elapsed = now_ms - completed;
    // A producer can publish a timestamp just after the observer sampled now.
    // Treat that small future timestamp as progress, not a wrapped long stall.
    if (elapsed < SS_PROGRESS_TIMEOUT_MS || elapsed > UINT_MAX / 2) return false;
    // Re-read before a reset decision if the producer completed concurrently.
    return completed == atomic_load_explicit(&guard->completed_ms, memory_order_acquire);
}
