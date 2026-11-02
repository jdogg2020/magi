// Exercise the real review controller with a deterministic, paginated archive.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function el(id){if(!elements.has(id))elements.set(id,{value:'',checked:false,disabled:false,textContent:'',open:true,tagName:'DIV',children:[],dataset:{},classList:{contains:()=>false,toggle(){}},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(name,value){this[name]=value;},addEventListener(name,fn){this[name]=fn;}});return elements.get(id);}
const records=Array.from({length:230},(_,i)=>({id:String(i).padStart(4,'0'),camera_id:'a',trigger_unix:i,review:{labels:[],notes:'',reviewed:false,protected:false,trashed_at:null}}));
// Same timestamp/id on another camera must remain a distinct navigation target.
records.push({...records[125],camera_id:'b',review:{...records[125].review}});
const rejectKeys=new Set();
let queries=[],updates=0,allowDiscard=false,failUpdate=false,saveGate=null,neighborGate=null,failNeighbors=false;
const windowListeners={};
const ctx={console,setTimeout,window:{addEventListener:(name,fn)=>windowListeners[name]=fn},csrf:'token',selectedCamera:'a',clipSignature:'',reviewEvent:null,reviewMode:'image',confirm:()=>allowDiscard,
 document:{getElementById:el,createElement:()=>el('node-'+Math.random())},eventKey:r=>r.camera_id+':'+r.id,eventPath:()=>'',updateReviewAvailability(){},refreshDashboard:async()=>{},
 api:async(path,data)=>{
  if(path.endsWith('/update')){updates++;if(saveGate)await saveGate;if(failUpdate || rejectKeys.has(data.targets[0].camera_id+':'+data.targets[0].id))return {updated:[],failed:[{error:'Test save rejected'}]};const row=records.find(r=>r.id===data.targets[0].id && r.camera_id===data.targets[0].camera_id);if(data.action==='update')Object.assign(row.review,data.changes);else row.review.trashed_at=data.action==='trash'?1:null;return {updated:data.targets,failed:[]};}
  queries.push(data);if(data.anchor && neighborGate)await neighborGate;if(data.anchor && failNeighbors)return {previous:null,next:null,unavailable_cameras:['a']};let rows=records.filter(r=>(data.reviewed!=='no'||!r.review.reviewed)&&(data.camera_id==='all'||data.camera_id===r.camera_id)&&(data.trash==='all'||(data.trash==='trash')===(r.review.trashed_at!=null)));
  rows.sort((a,b)=>(a.trigger_unix-b.trigger_unix || a.id.localeCompare(b.id)||a.camera_id.localeCompare(b.camera_id))*(data.sort==='oldest'?1:-1));
  if(data.anchor){const a=data.anchor,compare=r=>(r.trigger_unix-a.trigger_unix || r.id.localeCompare(a.id)||r.camera_id.localeCompare(a.camera_id))*(data.sort==='oldest'?1:-1);return {previous:structuredClone(rows.filter(r=>compare(r)<0).at(-1)||null),next:structuredClone(rows.find(r=>compare(r)>0)||null),unavailable_cameras:[]};}
  return {events:structuredClone(rows.slice(data.offset,data.offset+data.limit)),total:rows.length,offset:data.offset,labels:[]};
 }};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('src/magi/web/events.js','utf8'),ctx);
ctx.openReview=row=>{ctx.reviewEvent=structuredClone(row);vm.runInContext('MagiEvents.updateReview(reviewEvent)',ctx);};
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));};
(async()=>{
 vm.runInContext('MagiEvents.init()',ctx);
 el('event-reviewed').value='no';el('event-sort').value='oldest';el('event-folder').value='active';
 await el('event-filters').onsubmit({preventDefault(){}});
 ctx.openReview(records[125]);await settle();
 el('review-event-next').onclick();await settle();assert.equal(ctx.reviewEvent.camera_id,'b');
 el('review-event-next').onclick();await settle();assert.equal(ctx.reviewEvent.id,'0126');
 el('review-event-prev').onclick();await settle();assert.equal(ctx.reviewEvent.camera_id,'b');
 assert(queries.some(q=>q.anchor),'Navigation asks for adjacent events directly');assert(!queries.some(q=>q.offset===100),'Navigation must not scan archive pages');
 await el('review-mark').onclick();await settle();assert(ctx.reviewEvent.review.reviewed);
 el('review-event-next').onclick();await settle();assert.equal(ctx.reviewEvent.id,'0126','A removed anchor must not skip its successor');
 await el('review-keep').onclick();assert(el('review-trash').disabled);
 let before=updates;await el('review-trash').onclick();assert.equal(updates,before);
 await el('review-keep').onclick();await el('review-trash').onclick();assert.equal(el('review-trash').textContent,'Restore from trash');
 await el('review-trash').onclick();assert.equal(ctx.reviewEvent.review.trashed_at,null);
 el('review-notes').value='unsaved';el('review-event-next').onclick();assert.equal(ctx.reviewEvent.id,'0126');
 await el('review-mark').onclick();assert.equal(el('review-notes').value,'unsaved','Quick review preserves notes');
 const key=(key,tag='DIV',extra={})=>el('review').keydown({key,target:{tagName:tag},preventDefault(){},...extra});
 before=updates;key('r','TEXTAREA');key('Delete','INPUT');key('r','DIV',{repeat:true});key('r','DIV',{ctrlKey:true});await settle();assert.equal(updates,before);
 allowDiscard=true;key('ArrowRight');await settle();assert.equal(ctx.reviewEvent.id,'0127');
 key('r');await settle();assert(ctx.reviewEvent.review.reviewed);
 let played=0,paused=0;el('review-play').onclick=()=>{played++;ctx.reviewMode='video';};el('review-video').paused=false;el('review-video').pause=()=>paused++;
 key(' ');key(' ');assert.equal(played,1);assert.equal(paused,1);
 const pending=()=>vm.runInContext('MagiEvents.hasPending()',ctx);
 const findButton=(title)=>el('review-background-saves').children.flatMap(n=>n.children || []).find(n=>n.textContent===title);
 const resetDraft=()=>{el('review-notes').value='';el('review-labels').value='';el('review-protected').checked=false;};
 // All category buttons advance before an intentionally unresolved save.
 for(const label of ['ignore','aircraft','bird-insect','lightning','weather','meteor','satellite','unknown']){
   ctx.openReview(records[150]);await settle();
   let releaseSave;saveGate=new Promise(resolve=>releaseSave=resolve);
   const start=queries.length;
   await el('review-label-'+label).onclick();await settle();
   assert.equal(ctx.reviewEvent.id,'0151','Category opens next event before save confirmation');
   assert(pending());assert(el('logout').disabled);
   assert.equal(queries.length,start+1,'Only the newly opened event queries its neighbors');
   let warned=false;windowListeners.beforeunload({preventDefault(){warned=true;},returnValue:null});assert(warned);
   releaseSave();saveGate=null;await settle();assert(!pending());assert(!el('logout').disabled);
 }
 // A second edit to the same event must be sent after its first write, and
 // returning via stale neighbor metadata must retain the latest local draft.
 ctx.openReview(records[20]);await settle();
 let releaseSave;saveGate=new Promise(resolve=>releaseSave=resolve);
 el('review-notes').value='Keep this note';el('review-protected').checked=true;
 await el('review-label-aircraft').onclick();await settle();
 const sent=updates;
 ctx.openReview(records[20]);await settle();assert.equal(el('review-notes').value,'Keep this note');
 assert(el('review-label-aircraft')['aria-pressed']==='true');
 assert(el('review-trash').disabled,'Do not race trash/protection against a queued write');
 await el('review-label-weather').onclick();await settle();assert.equal(updates,sent,'Writes are serialized');
 releaseSave();saveGate=null;await settle();
 assert.deepEqual(Array.from(records[20].review.labels),['aircraft','weather']);
 assert.equal(records[20].review.notes,'Keep this note');assert(records[20].review.protected);assert(!pending());
 ctx.openReview(records[20]);await settle();assert(el('review-label-weather')['aria-pressed']==='true');
 // A failed background save remains recoverable without yanking review back.
 ctx.openReview(records[30]);await settle();failUpdate=true;
 await el('review-label-lightning').onclick();await settle();
 assert.equal(ctx.reviewEvent.id,'0031');assert(pending());assert(findButton('Retry'));
 assert(!records[30].review.labels.includes('lightning'));
 vm.runInContext('MagiEvents.reset()',ctx);assert(pending(),'Session reset must not discard failed changes');
 ctx.openReview(records[30]);await settle();assert.equal(el('review-label-lightning')['aria-pressed'],'true');
 failUpdate=false;findButton('Retry').onclick();await settle();assert(!pending());assert(records[30].review.labels.includes('lightning'));
 // Discard requires confirmation and removes only the failed local change.
 ctx.openReview(records[40]);await settle();failUpdate=true;
 await el('review-label-weather').onclick();await settle();
 allowDiscard=false;findButton('Discard unsaved changes').onclick();assert(pending());
 allowDiscard=true;failUpdate=false;findButton('Discard unsaved changes').onclick();await settle();assert(!pending());
 ctx.openReview(records[40]);await settle();assert.equal(el('review-label-weather')['aria-pressed'],'false');
 // One failed camera/event must not block another with the same event ID.
 let releasePair;saveGate=new Promise(resolve=>releasePair=resolve);
 rejectKeys.add('a:0125');ctx.openReview(records[125]);await settle();await el('review-label-weather').onclick();
 ctx.openReview(records.at(-1));await settle();await el('review-label-lightning').onclick();
 releasePair();saveGate=null;await settle();
 assert(records.at(-1).review.labels.includes('lightning'));assert(pending());
 rejectKeys.clear();findButton('Retry').onclick();await settle();assert(!pending());
 // Bound unsaved work without dropping the hundredth queued edit.
 let releaseBatch;saveGate=new Promise(resolve=>releaseBatch=resolve);
 for(let i=50;i<150;i++){ctx.openReview(records[i]);await settle();await el('review-label-weather').onclick();}
 assert(pending());ctx.openReview(records[180]);await settle();
 assert(el('review-label-weather').disabled,'Backpressure must stop adding a 101st event');
 const beforeFull=updates;await el('review-label-weather').onclick();assert.equal(updates,beforeFull);
 releaseBatch();saveGate=null;await settle();assert(!pending());
 // Early clicks share an unfinished navigation lookup, but still queue saving.
 let releaseInitial;neighborGate=new Promise(resolve=>releaseInitial=resolve);
 const start=queries.length;ctx.openReview(records[160]);await settle();
 const early=el('review-label-weather').onclick();await settle();
 assert.equal(queries.length,start+1);assert.equal(ctx.reviewEvent.id,'0160');
 neighborGate=null;releaseInitial();await early;await settle();assert.equal(ctx.reviewEvent.id,'0161');
 // Unavailable navigation must not prevent the background save.
 failNeighbors=true;ctx.openReview(records[170]);await settle();
 await el('review-label-weather').onclick();await settle();assert.equal(ctx.reviewEvent.id,'0170');
 assert(records[170].review.labels.includes('weather'));assert(el('review-save-status').textContent.includes('Navigation unavailable'));
 failNeighbors=false;
 ctx.openReview(records[229]);await settle();await el('review-label-weather').onclick();await settle();
 assert.equal(ctx.reviewEvent.id,'0229');assert(el('review-save-status').textContent.includes('Last matching event'));
 ctx.openReview(records[180]);await settle();
 el('review-labels').value='a,b,c,d,e,f,g,h';const tooMany=updates;await el('review-label-weather').onclick();assert.equal(updates,tooMany);
 ctx.openReview({...records[180],review:{...records[180].review,trashed_at:1}});await settle();assert(el('review-label-weather').disabled);
 ctx.openReview({...records[180],review:{...records[180].review,annotation_error:true}});await settle();assert(el('review-label-weather').disabled);
 ctx.openReview(records[180]);await settle();el('review-notes').value='Unsaved manual note';failUpdate=true;
 await el('review-save').onclick();failUpdate=false;
 assert.equal(ctx.reviewEvent.id,'0180');assert.equal(el('review-notes').value,'Unsaved manual note');assert(el('review-save-status').textContent.includes('rejected'));
 const refreshStart=queries.length;await vm.runInContext('MagiEvents.refresh()',ctx);assert.equal(queries.length,refreshStart);
 el('review').open=false;el('review').close();await settle();assert.equal(queries.length,refreshStart+1);
 let warned=false;windowListeners.beforeunload({preventDefault(){warned=true;}});assert(!warned);
 console.log('Event review: page boundaries, camera identity, filtered anchors, protection, restore, drafts and keyboard passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
