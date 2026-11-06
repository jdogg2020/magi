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
  assert.equal(context.selected.size,0,'Completed exports must clear selection');
  context.selected.set('a',{id:'first',camera_id:'primary'});
  status={state:'failed',sent:0,failed:[],flag_failed:[]};
  await el('event-export-zip').onclick();
  assert.match(el('event-action-status').textContent,/not marked exported/);
  assert.equal(context.exporting,false);
  assert.equal(context.selected.size,1,'Failed exports retain selection for retry');
  Object.assign(context,{requestVersion:1,currentFilters:{exported:'not_exported'},eventKey:r=>r.camera_id+':'+r.id,withLocalReview:r=>r,render(){}});
  const matching=Array.from({length:40},(_,i)=>({id:String(i),camera_id:'primary'}));
  context.api=async(path,body)=>{assert.equal(body.limit,100);assert.equal(body.exported,'not_exported');return {events:matching,total:40};};
  vm.runInNewContext(full.slice(full.indexOf("    el('event-select-all').onclick="),full.indexOf("    el('event-clear-selection').onclick=")),context);
  await el('event-select-all').onclick();
  assert.equal(context.selected.size,40,'Select all includes events beyond the first page');
  context.api=async()=>({events:matching,total:101});
  await el('event-select-all').onclick();
  assert.match(el('event-action-status').textContent,/Narrow your filters/);
  assert.equal(context.selected.size,40,'Over-limit selection must not silently replace selection');
  context.api=async()=>{context.requestVersion++;return {events:[],total:0};};
  await el('event-select-all').onclick();
  assert.equal(context.selected.size,40,'Stale filter response must not change selection');
  let routed=false;el('event-export-zip').onclick=async()=>{routed=true;};
  vm.runInNewContext(full.slice(full.indexOf("    el('event-apply').onclick="),full.indexOf("    el('event-export-zip').onclick=")),context);
  el('event-action').value='export';await el('event-apply').onclick();assert(routed,'Export action must use ZIP export');
  console.log('Bulk export UI: saved labels, native download, completion and failure passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
