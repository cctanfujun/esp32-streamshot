import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { StreamShotSession } from '../skills/esp32-streamshot/scripts/session.mjs';

function fake(handler) {
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.exitCode = null;
  child.stdin = new Writable({ write(data, encoding, callback) {
    handler(JSON.parse(data.toString()), child); callback();
  }});
  child.stdin.on('finish', () => { child.exitCode = 0; child.emit('exit', 0); });
  child.kill = () => { child.exitCode = 0; child.emit('exit', 0); };
  return child;
}
function answer(child, message, result) {
  child.stdout.write(JSON.stringify({ jsonrpc:'2.0', id:message.id, result }) + '\n');
}

test('preserves an image response split across pipe chunks without file I/O', async () => {
  const synthetic = Buffer.alloc(12000, 8).toString('base64');
  const child = fake((message, process) => {
    const wire = JSON.stringify({jsonrpc:'2.0',id:message.id,result:{content:[{type:'image',mimeType:'image/jpeg',data:synthetic}],isError:false}})+'\n';
    process.stdout.write(wire.slice(0, 17)); process.stdout.write(wire.slice(17, 7900)); process.stdout.write(wire.slice(7900));
  });
  const client = new StreamShotSession(child);
  const result = await client.call('streamshot_get_screen');
  assert.equal(result.content[0].data, synthetic);
  await client.close();
});

test('serializes callers and preserves an explicit replay ID', async () => {
  const seen=[];
  const child=fake((message, process)=>{seen.push(message.id);setTimeout(()=>answer(process,message,{content:[],isError:false}),5);});
  const client=new StreamShotSession(child);
  await Promise.all([client.call('streamshot_status',{}, {id:'same'}),client.call('streamshot_status',{}, {id:'same'})]);
  assert.deepEqual(seen,['same','same']);
  await client.close();
});

test('transport timeout interrupts its own child and never retries input', async () => {
  const seen=[];
  const child=fake((message, process)=>{
    seen.push(message.params.name);
    if(message.params.name!=='streamshot_act')answer(process,message,{content:[],isError:false});
  });
  const client=new StreamShotSession(child,100);
  await assert.rejects(client.call('streamshot_act',{}),/timed_out/);
  await assert.rejects(client.call('streamshot_control',{}),/reset_control_required/);
  assert.deepEqual(seen,['streamshot_act']);
  assert.notEqual(child.exitCode, null);
  await assert.rejects(client.call('streamshot_reset_control'),/session_closed/);
  await client.close();
});

test('a timed-out running batch cannot execute its remaining scheduled actions', async () => {
  const directory=await mkdtemp(join(tmpdir(),'streamshot-session-'));
  const receipt=join(directory,'synthetic-actions.txt');
  const code=`const fs=require('node:fs');process.stdin.once('data',()=>{[20,700,1000].forEach((ms,i)=>setTimeout(()=>fs.appendFileSync(process.argv[1],String(i)+'\\n'),ms));});`;
  const child=spawn(process.execPath,['-e',code,receipt],{stdio:['pipe','pipe','pipe']});
  const client=new StreamShotSession(child,400);
  try {
    await assert.rejects(client.call('streamshot_act',{}),/timed_out/);
    await new Promise(resolve=>setTimeout(resolve,1000));
    assert.equal(await readFile(receipt,'utf8'),'0\n');
  } finally { await client.close(); await rm(directory,{recursive:true,force:true}); }
});

test('EOF rejects pending request and unknown tools never reach transport', async () => {
  const child=fake(()=>{});const client=new StreamShotSession(child);
  await assert.rejects(client.call('shell'),/unsupported_tool/);
  const pending=client.call('streamshot_status');
  await new Promise(resolve=>setImmediate(resolve));child.emit('exit',1);
  await assert.rejects(pending,/process_closed/);
  child.exitCode=1;
});

test('malformed response rejects safely without echoing content', async () => {
  const child=fake((message, process)=>process.stdout.write('private malformed response\n'));
  const client=new StreamShotSession(child);
  await assert.rejects(client.call('streamshot_status'),/invalid_mcp_response/);
  await client.close();
});
