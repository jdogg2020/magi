const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const full=fs.readFileSync('src/magi/web/events.js','utf8');
const source=full.slice(full.indexOf("    el('event-export-zip').onclick="),full.indexOf("    el('event-export').onclick="));
const elements=new Map(),links=[],calls=[];
const el=id=>{if(!elements.has(id))elements.set(id,{value:id==='event-export-media'?'originals':'',textContent:''});return elements.get(id);};
let status={state:'complete',sent:2,failed:[],flag_failed:[]},refreshes=0;
const context={
  el,selected:new Map([['a',{id:'first',camera_id:'primary'}],['b',{id:'second',camera_id:'cam-second'}]]),
  mutating:false,exporting:false,pendingSaves:new Map(),selectedCamera:'primary',selection(){},
  node(){const link={click(){links.push(this.href);},remove(){}};return link;},
  document:{body:{append(){}}},lastRefresh:1,
  setTimeout(resolve){resolve();},
  refresh:async()=>{refreshes++;},
  api:async(path,body)=>{calls.push({path,body});return body?{id:'token',download:'/api/station/events/export/token/download'}:status;}
};
vm.runInNewContext(source,context);
(async()=>{
  context.pendingSaves.set('a',{});
  await el('event-export-zip').onclick();
  assert.equal(calls.length,0);
  context.pendingSaves.clear();
  await el('event-export-zip').onclick();
  assert.equal(calls[0].body.targets.length,2);
  assert.equal(calls[0].body.media,'originals');
  assert.equal(links[0],'/api/station/events/export/token/download');
  assert.equal(refreshes,1);
  assert.match(el('event-action-status').textContent,/Sent 2 event/);
  assert.equal(context.exporting,false);
  status={state:'failed',sent:0,failed:[],flag_failed:[]};
  await el('event-export-zip').onclick();
  assert.match(el('event-action-status').textContent,/not marked exported/);
  assert.equal(context.exporting,false);
  console.log('Bulk export UI: saved labels, native download, completion and failure passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
