import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import { randomUUID } from 'node:crypto';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FastVisionAdvisor, readFocusEvidence, reviewFocus } from '../skills/esp32-streamshot/scripts/fast-vision.mjs';

const encode = value => JSON.stringify(value);
const frame = () => ({ summary: { screen: { frame_id: randomUUID(), session_id: randomUUID() } },
  images: [{ mimeType: 'image/jpeg', data: '/9j/2Q==' }] });
const focused = encode({ status: 'target_focused', visible_focus: 'Touch' });
const unclear = { status: 'unclear', visible_focus: null };

test('visible focus requires a label that matches the claim and a strict result', () => {
  assert.deepEqual(readFocusEvidence(focused, 'Ｔｏｕｃｈ'), JSON.parse(focused));
  assert.deepEqual(readFocusEvidence(focused, 'Switch Control'), unclear);
  assert.deepEqual(readFocusEvidence('{wrong', 'Touch'), unclear);
  assert.deepEqual(readFocusEvidence(encode({ ...JSON.parse(focused), action: 'tap' }), 'Touch'), unclear);
  assert.deepEqual(readFocusEvidence(encode({ status: 'focus_elsewhere', visible_focus: 'Touch' }), 'Touch'), unclear);
  assert.deepEqual(readFocusEvidence(encode({ status: 'no_visible_focus', visible_focus: 'Touch' }), 'Touch'), unclear);
  assert.deepEqual(readFocusEvidence(encode({ status: 'no_visible_focus', visible_focus: null }), 'Touch'),
    { status: 'no_visible_focus', visible_focus: null });
});

function harness({ injection, model = 'gpt-6-luna', timeoutMs = 200, phase = 'final_answer',
  startDelay = 0, answerDelay = 0 } = {}) {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.kill = () => {};
  const call = [];
  let threadId;
  const publish = message => child.stdout.write(encode({ jsonrpc: '2.0', ...message }) + '\n');
  child.stdin = new Writable({ write(chunk, _, done) {
    const { method, params, id } = JSON.parse(chunk);
    call.push([method, params]);
    if (method === 'thread/start') {
      threadId = randomUUID();
      const send = () => publish({ id, result: { model, modelProvider: 'openai', reasoningEffort: 'low',
        thread: { id: threadId, ephemeral: true } } });
      if (startDelay) setTimeout(send, startDelay);
      else send();
    }
    if (method === 'turn/start') {
      const turnId = randomUUID();
      publish({ id, result: { turn: { id: turnId } } });
      if (injection === 'request') publish({ id: 'unexpected', method: 'item/commandExecution/requestApproval', params: {} });
      else if (injection === 'item') publish({ method: 'item/started', params: { threadId, turnId,
        item: { type: 'mcpToolCall' } } });
      else if (injection !== 'hang') {
        const send = () => {
          publish({ method: 'item/completed', params: { threadId, turnId,
            item: { type: 'agentMessage', phase, text: focused } } });
          publish({ method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
        };
        if (answerDelay) setTimeout(send, answerDelay);
        else send();
      }
    }
    if (method === 'thread/unsubscribe') publish({ id, result: {} });
    done();
  }});
  return { client: new FastVisionAdvisor(child, '', timeoutMs), call };
}

test('a single in-memory image enters an isolated ephemeral turn and yields only bound evidence', async () => {
  const { client, call } = harness();
  const source = frame();
  const result = await client.inspect(source, 'Touch');
  assert.equal(result.frame_id, source.summary.screen.frame_id);
  assert.equal(result.session_id, source.summary.screen.session_id);
  assert.equal(result.status, 'target_focused');
  assert.deepEqual(call.map(([m]) => m), ['thread/start', 'turn/start', 'thread/unsubscribe']);
  assert.equal(call[0][1].ephemeral, true);
  assert.equal(call[0][1].approvalPolicy, 'never');
  assert.deepEqual(call[0][1].environments, []);
  assert.deepEqual(call[1][1].environments, []);
  assert.equal(call[1][1].sandboxPolicy.networkAccess, false);
  assert.equal(call[1][1].serviceTierForTurn, 'default');
  assert.equal(call[1][1].input[1].url, 'data:image/jpeg;base64,/9j/2Q==');
  assert.equal(Object.hasOwn(result, 'actions'), false);
});

test('missing, diagnostic, expired and non-JPEG observations never start a model turn', async () => {
  const { client, call } = harness();
  for (const change of [
    f => { f.images = []; },
    f => { f.images[0].data = 'eW8='; },
    f => { f.summary.screen_status = 'captured_observe_again'; },
    f => { f.summary.screen.usable_for_control = false; },
    f => { f.summary.screen.observation_age_ms = 31000; },
  ]) {
    const f = frame();
    change(f);
    await assert.rejects(client.inspect(f, 'Touch'), /fast_vision_invalid_observation/);
  }
  assert.deepEqual(call, []);
});

test('route changes and unexpected tools stop the advisor; unsupported output abstains', async () => {
  const changed = harness({ model: 'gpt-other' });
  await assert.rejects(changed.client.inspect(frame(), 'Touch'), /fast_vision_route_changed/);
  assert.equal(changed.call.length, 1);
  for (const injection of ['request', 'item']) {
    await assert.rejects(harness({ injection }).client.inspect(frame(), 'Touch'), /fast_vision_unexpected_tool/);
  }
  const partial = await harness({ phase: 'commentary' }).client.inspect(frame(), 'Touch');
  assert.equal(partial.status, 'unclear');
  assert.equal(partial.visible_focus, null);
});

test('a stalled turn times out without retry', async () => {
  const { client, call } = harness({ injection: 'hang', timeoutMs: 100 });
  await assert.rejects(client.inspect(frame(), 'Touch'), /fast_vision_timeout/);
  await assert.rejects(client.inspect(frame(), 'Touch'), /fast_vision_unavailable/);
  assert.deepEqual(call.map(([m]) => m), ['thread/start', 'turn/start']);
});

test('thread startup and image reasoning share the caller deadline', async () => {
  const { client, call } = harness({ timeoutMs: 500, startDelay: 70, answerDelay: 80 });
  await assert.rejects(client.inspect(frame(), 'Touch', { maxWaitMs: 115 }), /fast_vision_timeout/);
  assert.deepEqual(call.map(([method]) => method), ['thread/start', 'turn/start']);
});

function displayHarness(view, observed = true) {
  const trace = [];
  const result = { opaque: true };
  const session = {
    async inspect(received, callback) {
      assert.equal(received, result);
      trace.push('inspect');
      await callback(view);
      return { observed };
    },
    act() { assert.fail('review should not send input'); },
    screen() { assert.fail('review should not fetch another frame'); },
  };
  const display = shown => {
    assert.equal(shown, view);
    trace.push('display');
  };
  return { session, result, display, trace };
}

test('focus review emits one view and only returns frame-bound advisory evidence', async () => {
  const view = frame();
  const { session, result, display, trace } = displayHarness(view);
  const advisor = { async inspect(observed, target, { maxWaitMs }) {
    assert.equal(observed, view);
    assert.equal(target, 'Touch');
    assert.ok(maxWaitMs <= 6000 && maxWaitMs > 5000);
    trace.push('advisor');
    return { ...view.summary.screen, ...JSON.parse(focused), actions: [{ op: 'focus_activate' }] };
  } };
  const review = await reviewFocus(session, result, { target: 'Touch', display, advisor });
  assert.deepEqual(trace, ['inspect', 'display', 'advisor']);
  assert.deepEqual(review, { route: 'advisor', ...view.summary.screen, ...JSON.parse(focused) });
});

test('timeout, uncertain result and wrong frame fall back to the same displayed image', async () => {
  for (const [reason, inspect] of [
    ['advisor_timeout', async () => { throw new Error('fast_vision_timeout'); }],
    ['advisor_unavailable', async () => { throw new Error('other_error'); }],
    ['no_visible_focus', async view => ({ ...view.summary.screen, status: 'no_visible_focus', visible_focus: null })],
    ['evidence_mismatch', async () => ({ ...frame().summary.screen, ...JSON.parse(focused) })],
  ]) {
    const { session, result, display, trace } = displayHarness(frame());
    const review = await reviewFocus(session, result, { target: 'Touch', display, advisor: { inspect } });
    assert.deepEqual(review, { route: 'main', reason });
    assert.deepEqual(trace, ['inspect', 'display']);
  }
});

test('cached evidence cannot be uploaded by focus review or become usable input', async () => {
  const view = frame();
  view.summary.screen.usable_for_control = false;
  view.summary.screen.capture_origin = 'cached';
  const { session, result, display, trace } = displayHarness(view, false);
  const advisor = { async inspect() { trace.push('unexpected-advisor'); } };
  assert.deepEqual(await reviewFocus(session, result, { target: 'Touch', display, advisor }),
    { route: 'blocked', reason: 'fresh_observation_required' });
  assert.deepEqual(trace, ['inspect', 'display']);
});

test('review skips advisor upload when the session has no usable lifetime left', async () => {
  const view = frame();
  view.summary.client = { observation_remaining_ms: 0 };
  const { session, result, display, trace } = displayHarness(view, false);
  const advisor = { async inspect() { trace.push('unexpected-advisor'); } };
  assert.deepEqual(await reviewFocus(session, result, { target: 'Touch', display, advisor }),
    { route: 'blocked', reason: 'fresh_observation_required' });
  assert.deepEqual(trace, ['inspect', 'display']);
});

test('the session can block candidates received after the frame is unusable', async () => {
  const view = frame();
  const { session, result, display } = displayHarness(view, false);
  const advisor = { async inspect() { return { ...view.summary.screen, ...JSON.parse(focused) }; } };
  assert.deepEqual(await reviewFocus(session, result, { target: 'Touch', display, advisor }),
    { route: 'blocked', reason: 'fresh_observation_required' });
});
