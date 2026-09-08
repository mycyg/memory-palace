import {Client} from '../../sdk/typescript/dist/index.js';
import {readFileSync} from 'node:fs';
import {homedir} from 'node:os';
const token=readFileSync((process.env.EVENTMEM_HOME??homedir()+'/.memorypalace')+'/local-token','utf8').trim();
const client=new Client(process.env.EVENTMEM_URL??'http://127.0.0.1:8319',token);
const scope={project:'memorypalace-example',persona:'example',collection:'tool',world:'real'};
console.log(await client.call('recall',{body:{query:'deployment rollback',scope,scenario:'tool',budget:2000}}));
