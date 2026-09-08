import {afterEach,expect,it,vi} from 'vitest';
import {mkdtempSync,writeFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
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
