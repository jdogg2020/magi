const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const app=fs.readFileSync('src/magi/web/app.js','utf8');
const source=app.slice(app.indexOf('async function loadRecovered('),app.indexOf('function currentTracks('));
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
const event={id:'event-a',camera_id:'primary'};
let product={status:'invalid',reason:'stale capture evidence',processing:{state:'complete'},recovered_points:[]};
const context={$,api:async()=>product,reviewGeneration:1,reviewEvent:event,eventKey:e=>e.id,eventPath:()=>'',drawMotion(){}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 await context.loadRecovered(event,1);
 assert.match($('recovery-status').textContent,/Recovery: invalid/);
 assert.match($('recovery-status').textContent,/stale capture evidence/);
 product={...product,processing:{state:'running'}};
 await context.loadRecovered(event,1);
 assert.equal($('recover-event').disabled,true);
 console.log('Recovery UI: invalid evidence is visible despite completed processing.');
})().catch(error=>{console.error(error);process.exitCode=1;});
