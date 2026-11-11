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
const FRAME = { frame_id:'11111111-1111-4111-8111-111111111111', session_id:'22222222-2222-4222-8222-222222222222',
  width:591, height:1280, orientation:1, geometry_epoch:1, source_callback_age_ms:0, observation_age_ms:0 };
const imageBlock = {type:'image',mimeType:'image/jpeg',data:Buffer.from('synthetic-image').toString('base64')};
const screenResult = (frame = FRAME) => ({content:[imageBlock,{type:'text',text:JSON.stringify(frame)}],isError:false});
const actResult = (changes = {}, isError = false) => ({content:[{type:'text',text:JSON.stringify({ok:true,
  requested:1,acknowledged:1,status:'acknowledged_not_visual_confirmation',retryInput:false,
  input_ms:20,capture_ms:250,total_ms:270,results:[{step:1,ok:true,id:'synthetic-command'}],
  screen_status:'captured',screen:FRAME,...changes})},...(changes.screen_status === 'unavailable' ? [] : [imageBlock])],isError});

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

test('only an awaited explicit display makes a received screen usable', async () => {
  const seen=[];
  const child=fake((message,process)=>{seen.push(message.params);answer(process,message,screenResult());});
  const client=new StreamShotSession(child);
  const result=await client.screen({settle_ms:1000});
  assert.deepEqual(seen[0].arguments,{settle_ms:1000});
  await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
  let releaseDisplay;
  const displaying=client.inspect(result,async rendered=>{
    assert.equal(rendered.images[0].data,imageBlock.data);
    assert.equal(rendered.summary.screen.frame_id,FRAME.frame_id);
    assert.equal(typeof rendered.summary.client.rpc_ms,'number');
    await new Promise(resolve=>{releaseDisplay=resolve;});
  });
  await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
  releaseDisplay();
  assert.equal((await displaying).observed,true);
  await client.close();
});

test('act uses session-owned inspected metadata and requires inspection of its new image', async () => {
  const seen=[];
  const child=fake((message,process)=>{
    seen.push(message.params);
    answer(process,message,message.params.name==='streamshot_act'?actResult():screenResult());
  });
  const client=new StreamShotSession(child);
  await client.inspect(await client.screen(),async()=>{});
  const result=await client.act([{op:'click',button:'left'}],{settle_ms:0});
  assert.deepEqual(seen[1].arguments,{session_id:FRAME.session_id,frame_id:FRAME.frame_id,
    actions:[{op:'click',button:'left'}],capture:true,settle_ms:0});
  await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
  let rendered;
  const receipt=await client.inspect(result,async value=>{rendered=value;});
  assert.equal(receipt.observed,true);
  assert.equal(typeof receipt.decision_ms,'number');
  assert.equal(rendered.summary.total_ms,270);
  assert.equal(rendered.summary.results,undefined);
  assert.equal(JSON.stringify(rendered.summary).includes(imageBlock.data),false);
  await client.close();
});

test('queued acts cannot reuse the observation consumed by the first act', async () => {
  const names=[];
  const child=fake((message,process)=>{
    names.push(message.params.name);
    setTimeout(()=>answer(process,message,message.params.name==='streamshot_act'?actResult():screenResult()),5);
  });
  const client=new StreamShotSession(child);
  await client.inspect(await client.screen(),async()=>{});
  const first=client.act([{op:'click',button:'left'}]);
  const second=client.act([{op:'click',button:'left'}]);
  await first; await assert.rejects(second,/observe_again/);
  assert.deepEqual(names,['streamshot_get_screen','streamshot_act']);
  await client.close();
});

test('capture failure preserves the ACK but neither replays input nor restores observation', async () => {
  const names=[];
  const child=fake((message,process)=>{
    names.push(message.params.name);
    answer(process,message,message.params.name==='streamshot_act'
      ?actResult({screen:undefined,screen_status:'unavailable',screen_error:'screen_no_video_sample'},true):screenResult());
  });
  const client=new StreamShotSession(child);
  await client.inspect(await client.screen(),async()=>{});
  const result=await client.act([{op:'click',button:'left'}]);
  let rendered;
  assert.equal((await client.inspect(result,async value=>{rendered=value;})).observed,false);
  assert.equal(rendered.summary.acknowledged,1);
  assert.equal(rendered.summary.screen_error,'screen_no_video_sample');
  assert.equal(client.uncertain,false);
  await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
  assert.deepEqual(names,['streamshot_get_screen','streamshot_act']);
  await client.close();
});

test('reset, stop and a changed session invalidate an inspected frame', async () => {
  for(const invalidator of ['streamshot_reset_control','streamshot_stop','streamshot_status']) {
    const child=fake((message,process)=>answer(process,message,message.params.name==='streamshot_get_screen'
      ?screenResult():{content:[{type:'text',text:JSON.stringify({active:true,session_id:'different-session'})}],isError:false}));
    const client=new StreamShotSession(child);
    const original=await client.screen();
    await client.inspect(original,async()=>{});
    await client.call(invalidator);
    await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
    await assert.rejects(client.inspect(original,async()=>{}),/superseded/);
    await client.close();
  }
});

test('a superseded or failed display cannot install a stale observation', async () => {
  const child=fake((message,process)=>answer(process,message,screenResult()));
  const client=new StreamShotSession(child);
  const old=await client.screen();
  const latest=await client.screen();
  await assert.rejects(client.inspect(old,async()=>{}),/superseded/);
  await assert.rejects(client.inspect(latest,async()=>{throw Error('display_failed');}),/display_failed/);
  await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
  let finish;
  const pending=client.inspect(latest,async()=>new Promise(resolve=>{finish=resolve;}));
  await client.screen();finish();
  await assert.rejects(pending,/superseded/);
  await client.close();
});

test('unknown input stays blocked across a new displayed screen until explicit reset', async () => {
  const names=[];
  const child=fake((message,process)=>{
    names.push(message.params.name);
    answer(process,message,message.params.name==='streamshot_act'
      ?actResult({ok:false,acknowledged:0,status:'stopped',results:[{step:1,ok:false,status:'outcome_unknown',error:'control_reset_required'}]},true)
      :message.params.name==='streamshot_reset_control'?{content:[],isError:false}:screenResult());
  });
  const client=new StreamShotSession(child);
  await client.inspect(await client.screen(),async()=>{});
  const unknown=await client.act([{op:'click',button:'left'}]);
  let rendered;
  assert.equal((await client.inspect(unknown,async value=>{rendered=value;})).observed,false);
  assert.deepEqual(rendered.summary.failures,[{step:1,status:'outcome_unknown',error:'control_reset_required'}]);
  assert.equal((await client.inspect(await client.screen(),async()=>{})).observed,false);
  await client.call('streamshot_reset_control');
  await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
  assert.equal((await client.inspect(await client.screen(),async()=>{})).observed,true);
  assert.deepEqual(names,['streamshot_get_screen','streamshot_act','streamshot_get_screen','streamshot_reset_control','streamshot_get_screen']);
  await client.close();
});

test('benchmark records wall-clock, decision and round-trip time without input or images', async () => {
  const child=fake((message,process)=>setTimeout(()=>answer(process,message,
    message.params.name==='streamshot_act'?actResult():screenResult()),5));
  const client=new StreamShotSession(child);
  const start=client.beginBenchmark();
  assert.match(start.started_at,/Z$/);
  await client.inspect(await client.screen(),async()=>{});
  await new Promise(resolve=>setTimeout(resolve,15));
  await client.inspect(await client.act([{op:'type',text:'PRIVATE_TYPED_TEXT'}]),async()=>{});
  const metrics=client.endBenchmark({completed:true});
  assert.equal(metrics.completed,true);
  assert.equal(metrics.calls.length,2);
  assert.ok(metrics.wall_clock_ms>=15);
  assert.ok(metrics.calls[1].decision_ms>=10);
  for(const call of metrics.calls) {
    assert.equal(typeof call.roundtrip_ms,'number');
    assert.equal(typeof call.display_ms,'number');
  }
  assert.equal(metrics.calls[1].input_ms,20);
  const serialized=JSON.stringify(metrics);
  assert.equal(serialized.includes('PRIVATE_TYPED_TEXT'),false);
  assert.equal(serialized.includes(imageBlock.data),false);
  assert.equal(serialized.includes(FRAME.frame_id),false);
  await client.close();
});

test('a known not-sent remainder permits a new decision only after inspecting its captured frame', async () => {
  for(const screenStatus of ['captured','session_changed']) {
    const names=[];
    const child=fake((message,process)=>{
      names.push(message.params.name);
      answer(process,message,message.params.name==='streamshot_act'
        ?actResult({ok:false,requested:2,acknowledged:1,status:'stopped',screen_status:screenStatus,
          results:[{step:1,ok:true},{step:2,ok:false,status:'not_sent',error:'observe_again'}]},true)
        :screenResult());
    });
    const client=new StreamShotSession(child);
    await client.inspect(await client.screen(),async()=>{});
    const result=await client.act([{op:'click',button:'left'},{op:'key',usage:40,modifiers:0}]);
    await assert.rejects(client.act([{op:'click',button:'left'}]),/fresh_observed/);
    let rendered;
    const inspected=await client.inspect(result,async value=>{rendered=value;});
    assert.equal(inspected.observed,screenStatus==='captured');
    assert.deepEqual(rendered.summary.failures,[{step:2,status:'not_sent',error:'observe_again'}]);
    assert.equal(client.uncertain,false);
    // Displaying a partial receipt never sends or resumes its remaining key.
    assert.deepEqual(names,['streamshot_get_screen','streamshot_act']);
    await client.close();
  }
});
