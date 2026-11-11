import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';

const TOOLS = new Set(['streamshot_status', 'streamshot_get_screen', 'streamshot_control',
  'streamshot_act', 'streamshot_reset_control', 'streamshot_stop']);
const MUTATING = new Set(['streamshot_control', 'streamshot_act']);
const MAX_LINE = 3 * 1024 * 1024;
const INVALIDATES = new Set(['streamshot_get_screen', 'streamshot_control', 'streamshot_act',
  'streamshot_reset_control', 'streamshot_stop']);
const elapsed = (start, end = performance.now()) => Math.max(0, Math.round(end - start));

function view(result) {
  const content = Array.isArray(result?.content) ? result.content : [];
  let data;
  try { data = JSON.parse(content.find(block => block.type === 'text')?.text); } catch {}
  const images = content.filter(block => block.type === 'image');
  const metadata = data?.screen ?? data;
  const frame = images.length === 1 && images[0].mimeType === 'image/jpeg' &&
    typeof images[0].data === 'string' && typeof metadata?.frame_id === 'string' &&
    typeof metadata?.session_id === 'string' && Number.isInteger(metadata.width) &&
    Number.isInteger(metadata.height) && Number.isInteger(metadata.geometry_epoch)
    ? { ...metadata } : null;
  const summary = {};
  for (const key of ['ok', 'status', 'error', 'requested', 'acknowledged', 'retryInput',
    'input_ms', 'capture_ms', 'total_ms', 'screen_status', 'screen_error']) {
    if (data?.[key] !== undefined) summary[key] = data[key];
  }
  if (Array.isArray(data?.results)) {
    const failures = data.results.filter(item => item.ok !== true)
      .map(({ step, status, error }) => ({ step, status, ...(error ? { error } : {}) }));
    if (failures.length) summary.failures = failures;
  }
  if (frame) summary.screen = frame;
  return { summary, images, frame, data };
}

export class StreamShotSession {
  static async start({ python = 'python3', serialPort, state, timeoutMs = 30000 } = {}) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 60000) throw new Error('invalid_timeout');
    const entry = fileURLToPath(new URL('./streamshot/streamshot_bridge.py', import.meta.url));
    const args = [entry, ...(state ? ['--state', state] : []), 'mcp'];
    if (serialPort !== undefined && (typeof serialPort !== 'string' ||
        !/^\/dev\/cu\.(?:usbmodem|usbserial)[A-Za-z0-9_.-]*$/.test(serialPort))) throw new Error('invalid_serial_port');
    // /usr/bin/env sets just this explicit selection. This also works in
    // restricted Node REPLs without accessing a global process object.
    const launch = [`STREAMSHOT_SERIAL_PORT=${serialPort ?? ''}`, python, ...args];
    const session = new StreamShotSession(spawn('/usr/bin/env', launch, { stdio: ['pipe', 'pipe', 'pipe'] }), timeoutMs);
    try {
      await session.request('initialize', { protocolVersion: '2025-03-26', capabilities: {},
        clientInfo: { name: 'esp32-streamshot-skill', version: '0.1.0' } });
      session.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
      return session;
    } catch (error) { await session.close(); throw error; }
  }

  constructor(child, timeoutMs = 30000) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 60000) throw new Error('invalid_timeout');
    this.child = child; this.timeoutMs = timeoutMs; this.pending = new Map();
    this.next = 1; this.buffer = ''; this.closed = false; this.uncertain = false;
    this.tail = Promise.resolve();
    this.observation = null; this.generation = 0; this.results = new WeakMap(); this.benchmark = null;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', data => this.consume(data));
    // Drain diagnostics without printing potentially private content.
    child.stderr.on('data', () => {});
    child.on('error', () => this.fail('mcp_process_unavailable', true));
    child.on('exit', () => this.fail('mcp_process_closed'));
    child.stdin.on('error', () => this.fail('mcp_transport_closed', true));
  }

  consume(data) {
    this.buffer += data;
    if (this.buffer.length > MAX_LINE && !this.buffer.includes('\n')) return this.fail('mcp_response_too_large', true);
    let end;
    while ((end = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, end); this.buffer = this.buffer.slice(end + 1);
      if (line.length > MAX_LINE) return this.fail('mcp_response_too_large', true);
      let message;
      try { message = JSON.parse(line); } catch { return this.fail('invalid_mcp_response', true); }
      const pending = this.pending.get(message.id);
      if (!pending) continue;
      clearTimeout(pending.timer); this.pending.delete(message.id);
      if (message.error) pending.reject(new Error('mcp_rpc_error'));
      else if (message.jsonrpc === '2.0' && message.result && typeof message.result === 'object') pending.resolve(message.result);
      else { pending.reject(new Error('invalid_mcp_response')); this.fail('invalid_mcp_response', true); }
    }
  }

  fail(code, terminate = false) {
    this.closed = true;
    this.clearObservation();
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error(code)); }
    this.pending.clear(); this.buffer = '';
    if (terminate && this.child.exitCode === null) this.child.kill('SIGTERM');
  }

  request(method, params, id = `skill-${this.next++}`) {
    if (this.closed) return Promise.reject(new Error('mcp_session_closed'));
    if ((typeof id !== 'string' && !Number.isInteger(id)) || String(id).length > 128 || this.pending.has(id))
      return Promise.reject(new Error('invalid_request_id'));
    let wire;
    try { wire = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'; }
    catch { return Promise.reject(new Error('invalid_request_arguments')); }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (method === 'tools/call' && MUTATING.has(params.name)) {
          this.uncertain = true;
          // EOF would wait behind the running batch. Interrupt only our own
          // MCP child now, before reporting an unknown outcome to the caller.
          this.fail('mcp_request_timed_out', true);
        } else { this.pending.delete(id); reject(new Error('mcp_request_timed_out')); }
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(wire);
    });
  }

  clearObservation() {
    this.observation = null;
    this.generation += 1;
  }

  beginBenchmark() {
    if (this.benchmark) throw new Error('benchmark_already_running');
    this.benchmark = { started_at: new Date().toISOString(), started: performance.now(), calls: [], omitted: 0 };
    return { started_at: this.benchmark.started_at };
  }

  endBenchmark({ completed = false } = {}) {
    if (!this.benchmark) throw new Error('benchmark_not_running');
    const { started_at, started, calls, omitted } = this.benchmark;
    this.benchmark = null;
    return { started_at, wall_clock_ms: elapsed(started), completed: completed === true, calls, omitted };
  }

  trace(name, timing, data) {
    const entry = { tool: name, ...timing };
    for (const key of ['input_ms', 'capture_ms', 'total_ms', 'acknowledged', 'requested', 'screen_status']) {
      if (data?.[key] !== undefined) entry[key] = data[key];
    }
    if (this.benchmark) {
      if (this.benchmark.calls.length < 512) this.benchmark.calls.push(entry);
      else this.benchmark.omitted += 1;
    }
    return entry;
  }

  call(name, args = {}, options = {}) {
    return this.enqueue(name, () => args, options);
  }

  enqueue(name, argumentsForCall, { id } = {}, expectedObservation) {
    if (!TOOLS.has(name)) return Promise.reject(new Error('unsupported_tool'));
    const queuedAt = performance.now();
    const run = async () => {
      if (this.uncertain && MUTATING.has(name)) throw new Error('reset_control_required_after_unknown_input');
      if (expectedObservation && (this.observation !== expectedObservation ||
          performance.now() >= expectedObservation.expiresAt)) {
        throw new Error('observe_again');
      }
      const args = argumentsForCall();
      const started = performance.now();
      const timing = { queue_ms: elapsed(queuedAt, started) };
      if (expectedObservation) timing.decision_ms = elapsed(expectedObservation.inspectedAt, queuedAt);
      if (INVALIDATES.has(name)) this.clearObservation();
      const generation = this.generation;
      try {
        const result = await this.request('tools/call', { name, arguments: args }, id);
        if (name === 'streamshot_reset_control' && result.isError === false) this.uncertain = false;
        const rendered = view(result);
        if (rendered.data?.error === 'control_reset_required' || rendered.data?.status === 'outcome_unknown' ||
            rendered.data?.results?.some(item => item.status === 'outcome_unknown')) this.uncertain = true;
        if (name === 'streamshot_status' && (rendered.data?.active === false ||
            this.observation && rendered.data?.session_id !== this.observation.frame.session_id)) {
          this.clearObservation();
        }
        const receivedAt = performance.now();
        timing.rpc_ms = elapsed(started, receivedAt);
        timing.roundtrip_ms = elapsed(queuedAt, receivedAt);
        const trace = this.trace(name, timing, rendered.data);
        // Keep only metadata. Image ownership remains with the caller/result.
        this.results.set(result, { generation, started, receivedAt, timing, trace, frame: rendered.frame,
          eligible: !!rendered.frame &&
            (name === 'streamshot_get_screen' && !result.isError ||
              name === 'streamshot_act' && rendered.data?.screen_status === 'captured') });
        return result;
      } catch (error) {
        timing.rpc_ms = elapsed(started); timing.roundtrip_ms = elapsed(queuedAt);
        this.trace(name, { ...timing, failed: true });
        if (MUTATING.has(name)) this.uncertain = true;
        throw error;
      }
    };
    const result = this.tail.then(run);
    this.tail = result.catch(() => {});
    return result;
  }

  screen(options = {}) {
    return this.call('streamshot_get_screen', options);
  }

  async inspect(result, display) {
    if (typeof display !== 'function') throw new Error('screen_display_required');
    const context = this.results.get(result);
    if (!context || context.generation !== this.generation) throw new Error('screen_result_superseded');
    this.observation = null;
    const started = performance.now();
    const rendered = view(result);
    // The callback must emit the image for the agent to inspect. Receiving a
    // tool result alone never makes it an observation usable by act().
    try {
      await display({ summary: { ...rendered.summary, client: { ...context.timing } }, images: rendered.images });
    } finally { context.trace.display_ms = elapsed(started); }
    if (context.generation !== this.generation || this.closed) throw new Error('screen_result_superseded');
    const frame = context.frame;
    const age = Math.max(frame?.source_callback_age_ms ?? 0, frame?.observation_age_ms ?? 0);
    const expiresAt = Math.min(context.started, context.receivedAt - age) + 30000;
    const inspectedAt = performance.now();
    const observed = context.eligible && !this.uncertain && inspectedAt < expiresAt;
    if (observed) this.observation = { frame: Object.freeze({ ...frame }), inspectedAt, expiresAt };
    return { observed, ...context.timing, display_ms: elapsed(started, inspectedAt) };
  }

  act(actions, { capture = true, settle_ms = 250, id } = {}) {
    if (this.uncertain) return Promise.reject(new Error('reset_control_required_after_unknown_input'));
    const observation = this.observation;
    if (!observation) return Promise.reject(new Error('fresh_observed_screen_required'));
    return this.enqueue('streamshot_act', () => ({
      session_id: observation.frame.session_id, frame_id: observation.frame.frame_id,
      actions, capture, settle_ms,
    }), { id }, observation);
  }

  async close() {
    if (this.closed) { if (this.child.exitCode === null) this.child.kill('SIGTERM'); return; }
    const exited = new Promise(resolve => this.child.once('exit', resolve));
    this.child.stdin.end();
    const timer = setTimeout(() => this.child.kill('SIGTERM'), 2000);
    let deadline;
    await Promise.race([exited, new Promise(resolve => { deadline = setTimeout(resolve, 2500); })]);
    clearTimeout(timer); clearTimeout(deadline); this.fail('mcp_session_closed');
  }
}
