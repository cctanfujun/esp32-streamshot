import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const TOOLS = new Set(['streamshot_status', 'streamshot_get_screen', 'streamshot_control',
  'streamshot_act', 'streamshot_reset_control', 'streamshot_stop']);
const MUTATING = new Set(['streamshot_control', 'streamshot_act']);
const MAX_LINE = 3 * 1024 * 1024;

export class StreamShotSession {
  static async start({ python = 'python3', serialPort, state, timeoutMs = 30000 } = {}) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 60000) throw new Error('invalid_timeout');
    const entry = fileURLToPath(new URL('./streamshot/streamshot_bridge.py', import.meta.url));
    const args = [entry, ...(state ? ['--state', state] : []), 'mcp'];
    const env = { ...process.env };
    // No inherited device selection: the caller must choose a port explicitly.
    delete env.STREAMSHOT_SERIAL_PORT;
    if (serialPort !== undefined) env.STREAMSHOT_SERIAL_PORT = serialPort;
    const session = new StreamShotSession(spawn(python, args, { env, stdio: ['pipe', 'pipe', 'pipe'] }), timeoutMs);
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

  call(name, args = {}, { id } = {}) {
    if (!TOOLS.has(name)) return Promise.reject(new Error('unsupported_tool'));
    const run = async () => {
      if (this.uncertain && MUTATING.has(name)) throw new Error('reset_control_required_after_transport_failure');
      try {
        const result = await this.request('tools/call', { name, arguments: args }, id);
        if (name === 'streamshot_reset_control' && result.isError === false) this.uncertain = false;
        return result;
      } catch (error) { if (MUTATING.has(name)) this.uncertain = true; throw error; }
    };
    const result = this.tail.then(run);
    this.tail = result.catch(() => {});
    return result;
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
