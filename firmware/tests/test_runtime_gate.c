// SPDX-License-Identifier: Apache-2.0
// Actual control state machine plus concurrent host-only callback publications.
#include "control.h"
#include "runtime_gate.h"
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

static const char *A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
static const char *B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
static const char *BOOT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
typedef struct {
    ss_link_mailbox_t link;
    unsigned applied, input_reports, reports;
    uint64_t now;
    bool reconnect_during_move;
} fake_t;

static bool mouse(void *context, uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel)
{
    fake_t *fake = context;
    if (!ss_link_current(&fake->link, fake->applied)) return false;
    fake->reports++;
    if (buttons || dx || dy || wheel) {
        fake->input_reports++;
        if (fake->reconnect_during_move) {
            fake->reconnect_during_move = false;
            ss_link_publish(&fake->link, false);
            ss_link_publish(&fake->link, true);
        }
    }
    return ss_link_current(&fake->link, fake->applied);
}

static bool keyboard(void *context, uint8_t modifiers, uint8_t usage)
{
    fake_t *fake = context;
    if (!ss_link_current(&fake->link, fake->applied)) return false;
    fake->reports++;
    if (modifiers || usage) fake->input_reports++;
    return ss_link_current(&fake->link, fake->applied);
}

static uint64_t now_ms(void *context) { return ((fake_t *)context)->now; }
static void sleep_ms(void *context, uint32_t duration) { ((fake_t *)context)->now += duration; }

static void consume(fake_t *fake, ss_control_t *control)
{
    bool usable;
    if (ss_link_take(&fake->link, &fake->applied, &usable)) ss_connection(control, usable);
}

static ss_request_t request(ss_op_t op, const char *session, unsigned seq)
{
    ss_request_t value = {.op = op, .seq = seq, .ttl_ms = 30000, .button = 1, .usage = 4};
    snprintf(value.id, sizeof(value.id), "00000000-0000-4000-8000-%012u", seq);
    snprintf(value.session_id, sizeof(value.session_id), "%s", session);
    return value;
}

static void init(fake_t *fake, ss_control_t *control)
{
    memset(fake, 0, sizeof(*fake));
    ss_link_init(&fake->link);
    ss_io_t io = {.context = fake, .mouse = mouse, .keyboard = keyboard,
                  .sleep_ms = sleep_ms, .now_ms = now_ms};
    ss_init(control, io, BOOT);
    ss_link_publish(&fake->link, true);
    // Publication has no access to I/O or control. Only the consumer sends
    // neutral reports, on its own task after returning from the BT callback.
    assert(fake->reports == 0 && !control->connected);
    consume(fake, control);
    assert(control->connected && !control->armed && fake->reports == 2);
    ss_request_t arm = request(SS_ARM, A, 1);
    assert(ss_execute(control, &arm).ok && control->armed);
}

static void coalesced_reconnect_revokes_old_lease(void)
{
    fake_t fake;
    ss_control_t control;
    init(&fake, &control);
    unsigned reports = fake.reports;
    ss_link_publish(&fake.link, false);
    ss_link_publish(&fake.link, true);
    assert(control.armed); // The consumer has not run yet.
    assert(fake.reports == reports); // Neither callback sends anything.
    assert(!ss_link_current(&fake.link, fake.applied));
    consume(&fake, &control);
    assert(control.connected && !control.armed && control.latched);
    ss_request_t old = request(SS_ARM, A, 2);
    assert(!strcmp(ss_execute(&control, &old).error, "session_reused"));
    ss_request_t fresh = request(SS_ARM, B, 3);
    assert(ss_execute(&control, &fresh).ok && control.armed);
    puts("coalesced disconnect/reconnect revokes old arm; fresh session required: passed");
}

static void callback_edges_gate_every_report(void)
{
    fake_t fake;
    ss_control_t control;
    init(&fake, &control);
    ss_link_publish(&fake.link, false); // Includes a CONTROL suspend edge.
    ss_request_t key = request(SS_KEY, A, 2);
    assert(!ss_execute(&control, &key).ok);
    assert(fake.input_reports == 0 && !control.armed);

    init(&fake, &control);
    fake.reconnect_during_move = true;
    ss_request_t move = request(SS_MOVE, A, 2);
    move.dx = 2048;
    assert(!ss_execute(&control, &move).ok);
    assert(fake.input_reports == 1 && !control.armed);
    // Latest state is usable again, but its different epoch denied the
    // remaining 16 chunks before the consumer could acquire its mutex.
    consume(&fake, &control);
    assert(control.connected && !control.armed);

    init(&fake, &control);
    fake.reconnect_during_move = true;
    ss_request_t scroll = request(SS_SCROLL, A, 2);
    scroll.dy = 1;
    assert(!ss_execute(&control, &scroll).ok);
    assert(fake.input_reports == 1 && !control.armed);
    puts("suspend/reconnect gates each report and rejects a single in-flight scroll: passed");
}

static void *publish_many(void *context)
{
    ss_link_mailbox_t *link = context;
    for (unsigned i = 0; i < 10000; i++) ss_link_publish(link, (i & 1u) != 0);
    return NULL;
}

static void concurrent_producers_and_epoch_exhaustion(void)
{
    ss_link_mailbox_t link;
    ss_link_init(&link);
    pthread_t threads[4];
    for (unsigned i = 0; i < 4; i++) assert(!pthread_create(&threads[i], NULL, publish_many, &link));
    for (unsigned i = 0; i < 4; i++) assert(!pthread_join(threads[i], NULL));
    unsigned applied = 0;
    bool usable = false;
    assert(ss_link_take(&link, &applied, &usable));
    assert((applied >> 1) == 40000 && usable);
    assert(!ss_link_take(&link, &applied, &usable));
    atomic_store(&link.word, UINT_MAX - 3);
    ss_link_publish(&link, true);
    assert(ss_link_take(&link, &applied, &usable) && !usable);
    unsigned terminal = applied;
    ss_link_publish(&link, true);
    assert(!ss_link_take(&link, &applied, &usable) && applied == terminal);
    assert(!ss_link_current(&link, applied));
    puts("concurrent packed publications do not lose epochs; exhaustion stays unusable: passed");
}

static void progress_timeout_and_wrap(void)
{
    ss_progress_guard_t guard;
    ss_progress_init(&guard);
    assert(!ss_progress_stalled(&guard, 100000, false)); // Startup not ready.
    ss_progress_complete(&guard, 100000);
    assert(!ss_progress_stalled(&guard, 102999, false));
    assert(ss_progress_stalled(&guard, 103000, false));
    ss_progress_complete(&guard, 103001);
    assert(!ss_progress_stalled(&guard, 103000, false)); // Concurrent future feed.
    assert(!ss_progress_stalled(&guard, 106000, false));
    assert(ss_progress_stalled(&guard, 106001, false));

    ss_progress_init(&guard);
    ss_progress_complete(&guard, UINT_MAX - 1500);
    assert(!ss_progress_stalled(&guard, 1498, false));
    assert(ss_progress_stalled(&guard, 1499, false));

    ss_progress_init(&guard);
    ss_progress_complete(&guard, 1000);
    assert(!ss_progress_stalled(&guard, 10000, true));
    assert(!ss_progress_stalled(&guard, 12999, false));
    assert(ss_progress_stalled(&guard, 13000, false));
    ss_progress_complete(&guard, 13001);
    assert(!ss_progress_stalled(&guard, 13001, false));
    puts("independent progress gate covers startup, 3s stall, wrap and debugger grace: passed");
}

int main(void)
{
    coalesced_reconnect_revokes_old_lease();
    callback_edges_gate_every_report();
    concurrent_producers_and_epoch_exhaustion();
    progress_timeout_and_wrap();
    puts("All 4 runtime-gate host suites passed (ASan + UBSan).");
    return 0;
}
