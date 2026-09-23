import {afterEach,expect,it,vi} from 'vitest';
import {mkdtempSync,writeFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {ServiceRuntime} from '../src/service-runtime.js';
import {Config} from '../src/config.js';
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
 runtime.sessionStart('session','/synthetic-project',inject,'compact');await runtime.flushAll();
 expect(sent.map(r=>r.event)).toEqual(['start','message','pre_action','compact','end']);
 expect(readdirSync(join(root,'host-spool'))).toEqual([]);expect(inject).toHaveBeenCalled();
});
it('keeps a durable source when the service is unavailable',async()=>{
 const {root,runtime}=setup();vi.stubGlobal('fetch',vi.fn(async()=>{throw new Error('offline')}));
 runtime.message('s','/synthetic-project','assistant','Unverified account',9);await runtime.flush('s');
 expect(readdirSync(join(root,'host-spool')).filter(f=>f.endsWith('.json'))).toHaveLength(1);
});
it('settles only the exact context that the host accepted, retaining an uncertain receipt',async()=>{
 const {root,runtime}=setup();const injected:string[]=[];const seen:{url:string,body:any}[]=[];
 const body='Source-backed context';const hash=createHash('sha256').update(body).digest('hex');
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  const entry={url:String(url),body:JSON.parse(init.body)};seen.push(entry);
  if(entry.url.endsWith('/v1/context/receipts'))throw Error('receipt connection dropped');
  return new Response(JSON.stringify({text:body,delivery:{id:'ctx_one',body_hash:hash,epoch:0,state:'prepared'}}),{status:200});
 }));
 runtime.sessionStart('s','/synthetic-project',text=>injected.push(text));await runtime.flush('s');
 expect(injected).toEqual([body]);
 expect(seen.map(s=>s.url.endsWith('/v1/context/receipts'))).toEqual([false,true]);
 expect(seen[1]!.body).toMatchObject({session:'s',delivery_id:'ctx_one',body_hash:hash,state:'accepted'});
 const spool=readdirSync(join(root,'host-spool')).filter(f=>f.endsWith('.json'));
 expect(spool).toHaveLength(1);
});
