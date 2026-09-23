import {afterEach,expect,it,vi} from 'vitest';
import {mkdtempSync,writeFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {ServiceRuntime} from '../src/service-runtime.js';
import {Config} from '../src/config.js';
import {apply} from '../src/index.js';
const paths:string[]=[];
afterEach(()=>{vi.unstubAllGlobals();vi.unstubAllEnvs();for(const p of paths.splice(0))rmSync(p,{recursive:true,force:true})});
function setup(){const root=mkdtempSync(join(tmpdir(),'mp-service-'));paths.push(root);writeFileSync(join(root,'local-token'),'fixture-token');vi.stubEnv('EVENTMEM_HOME',root);return {root,runtime:new ServiceRuntime(Config({}))}}
it('orders capture, pre-action recall, compact and end through the common service',async()=>{
 const {root,runtime}=setup();const sent:any[]=[];const inject=vi.fn();
 vi.stubGlobal('fetch',vi.fn(async(_url,init)=>{sent.push(JSON.parse(init.body));return new Response(JSON.stringify({text:'Scoped shared-core context'}),{status:200})}));
 runtime.sessionStart('session','/synthetic-project',inject);
 runtime.message('session','/synthetic-project','user','My preference',1);
 await runtime.preAction('session','/synthetic-project','Read',{path:'app.py'},inject);
 expect(sent.map(r=>r.event)).toEqual(['start','message','pre_action']);
 runtime.sessionStart('session','/synthetic-project',inject,'compact');
 runtime.sessionStart('session','/synthetic-project',inject,'compact');await runtime.flushAll();
 expect(sent.map(r=>r.event)).toEqual(['start','message','pre_action','compact','compact','end']);
 expect(sent[3].payload.command_id).not.toBe(sent[4].payload.command_id);
 expect(readdirSync(join(root,'host-spool'))).toEqual([]);expect(inject).toHaveBeenCalled();
});
it('keeps a durable source when the service is unavailable',async()=>{
 const {root,runtime}=setup();vi.stubGlobal('fetch',vi.fn(async()=>{throw new Error('offline')}));
 runtime.message('s','/synthetic-project','assistant','Unverified account',9);await runtime.flush('s');
 expect(readdirSync(join(root,'host-spool')).filter(f=>f.endsWith('.json'))).toHaveLength(1);
});
it('settles only context entered into a native model step, retaining an uncertain receipt',async()=>{
 const {root,runtime}=setup();const injected:string[]=[];const seen:{url:string,body:any}[]=[];
 const body='Source-backed context';const hash=createHash('sha256').update(body).digest('hex');
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  const entry={url:String(url),body:JSON.parse(init.body)};seen.push(entry);
  if(entry.url.endsWith('/v1/context/receipts'))throw Error('receipt connection dropped');
  return new Response(JSON.stringify({text:body,delivery:{id:'ctx_one',body_hash:hash,epoch:0,state:'prepared'}}),{status:200});
 }));
 runtime.sessionStart('s','/synthetic-project',text=>{injected.push(text);return 'native-message-one'});await runtime.flush('s');
 expect(injected).toEqual([body]);
 expect(seen.map(s=>s.url.endsWith('/v1/context/receipts'))).toEqual([false]);
 runtime.turnStarted('s',1);
 runtime.contextClaimed('s','native-message-one',body,1);
 runtime.stepStarted('s',1);
 runtime.contextEntered('s','native-message-one','wrong body');
 await runtime.flush('s');
 expect(seen).toHaveLength(1);
 runtime.contextEntered('s','native-message-one',body);
 await runtime.flush('s');
 runtime.contextEntered('s','native-message-one',body);await runtime.flush('s');
 expect(seen.map(s=>s.url.endsWith('/v1/context/receipts'))).toEqual([false,true]);
 expect(seen[1]!.body).toMatchObject({session:'s',delivery_id:'ctx_one',body_hash:hash,state:'accepted'});
 const spool=readdirSync(join(root,'host-spool')).filter(f=>f.endsWith('.json'));
 expect(spool).toHaveLength(1);
});
it('adapter settles exact entered, rejected, and canceled native messages',async()=>{
 const {root}=setup();const body='Native context';const hash=createHash('sha256').update(body).digest('hex');
 const seen:{url:string,body:any}[]=[];
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  const row={url:String(url),body:JSON.parse(init.body)};seen.push(row);
  return new Response(JSON.stringify(row.url.endsWith('/v1/host/events')
   ? {text:body,delivery:{id:'ctx_'+row.body.payload.session_id,body_hash:hash}}
   : {state:'accepted'}),{status:200});
 }));
 const handlers=new Map<string,(...args:any[])=>any>();
 const ctx:any={on:(event:string,handler:(...args:any[])=>any)=>handlers.set(event,handler),get:()=>undefined,effect:()=>undefined};
 apply(ctx,Config({}));
 const emit=(event:string,...args:any[])=>handlers.get(event)!(...args);
 const makeAgent=(id:string)=>{
  const messages:any[]=[];
  const session:any={id,header:{cwd:'/synthetic-project'}};
  return {session,messages,agent:{session,inject:(message:any)=>messages.push(message)}};
 };
 const rejected=makeAgent('rejected');
 emit('agent/session-start',{agent:rejected.agent,source:'startup'});
 await emit('session/flush',rejected.session);
 expect(rejected.messages).toHaveLength(1);
 expect(seen.filter(r=>r.url.endsWith('/v1/context/receipts'))).toHaveLength(0);
 emit('session/event',rejected.session,{type:'turn/start',seq:1,data:{turn:1}});
 emit('agent/inbox/claimed',{agent:rejected.agent,message:rejected.messages[0],turn:1});
 emit('session/event',rejected.session,{type:'turn/end',seq:2,data:{turn:1,reason:{kind:'blocked'}}});
 await emit('session/flush',rejected.session);
 expect(seen.filter(r=>r.url.endsWith('/v1/context/receipts')).map(r=>r.body.state)).toEqual(['discarded']);

 const accepted=makeAgent('accepted');
 emit('agent/session-start',{agent:accepted.agent,source:'startup'});
 await emit('session/flush',accepted.session);
 emit('session/event',accepted.session,{type:'turn/start',seq:3,data:{turn:1}});
 emit('agent/inbox/claimed',{agent:accepted.agent,message:accepted.messages[0],turn:1});
 emit('session/event',accepted.session,{type:'step/start',seq:4,data:{turn:1,step:1}});
 expect(seen.filter(r=>r.url.endsWith('/v1/context/receipts'))).toHaveLength(1);
 emit('session/event',accepted.session,{type:'user/message',seq:5,data:accepted.messages[0]});
 await emit('session/flush',accepted.session);
 const canceled=makeAgent('canceled');
 emit('agent/session-start',{agent:canceled.agent,source:'startup'});
 await emit('session/flush',canceled.session);
 emit('agent/inbox/discarded',{agent:canceled.agent,message:canceled.messages[0]});
 await emit('session/flush',canceled.session);
 const receipts=seen.filter(r=>r.url.endsWith('/v1/context/receipts'));
 expect(receipts.map(r=>r.body.state)).toEqual(['discarded','accepted','discarded']);
 expect(receipts[1]!.body).toMatchObject({session:'accepted',delivery_id:'ctx_accepted',state:'accepted',body_hash:hash});
 expect(readdirSync(join(root,'host-spool')).filter(f=>f.endsWith('.json'))).toHaveLength(0);
});
