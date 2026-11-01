// Exercise the real review controller with a deterministic, paginated archive.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function el(id){if(!elements.has(id))elements.set(id,{value:'',checked:false,disabled:false,textContent:'',open:true,tagName:'DIV',classList:{contains:()=>false,toggle(){}},append(){},replaceChildren(){},setAttribute(name,value){this[name]=value;},addEventListener(name,fn){this[name]=fn;}});return elements.get(id);}
const records=Array.from({length:230},(_,i)=>({id:String(i).padStart(4,'0'),camera_id:'a',trigger_unix:i,review:{labels:[],notes:'',reviewed:false,protected:false,trashed_at:null}}));
// Same timestamp/id on another camera must remain a distinct navigation target.
records.push({...records[125],camera_id:'b',review:{...records[125].review}});
let queries=[],updates=0,allowDiscard=false,failUpdate=false;
const ctx={console,setTimeout,csrf:'token',selectedCamera:'a',clipSignature:'',reviewEvent:null,reviewMode:'image',confirm:()=>allowDiscard,
 document:{getElementById:el,createElement:()=>el('node-'+Math.random())},eventKey:r=>r.camera_id+':'+r.id,eventPath:()=>'',updateReviewAvailability(){},refreshDashboard:async()=>{},
 api:async(path,data)=>{
  if(path.endsWith('/update')){updates++;if(failUpdate)return {updated:[],failed:[{error:'Test save rejected'}]};const row=records.find(r=>r.id===data.targets[0].id && r.camera_id===data.targets[0].camera_id);if(data.action==='update')Object.assign(row.review,data.changes);else row.review.trashed_at=data.action==='trash'?1:null;return {updated:data.targets,failed:[]};}
  queries.push(data);let rows=records.filter(r=>(data.reviewed!=='no'||!r.review.reviewed)&&(data.camera_id==='all'||data.camera_id===r.camera_id)&&(data.trash==='all'||(data.trash==='trash')===(r.review.trashed_at!=null)));
  rows.sort((a,b)=>(a.trigger_unix-b.trigger_unix || a.id.localeCompare(b.id)||a.camera_id.localeCompare(b.camera_id))*(data.sort==='oldest'?1:-1));
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
 assert(queries.some(q=>q.offset===100),'Navigation must read beyond the first page');
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
 ctx.openReview(records[20]);await settle();
 el('review-notes').value='Save this note';el('review-protected').checked=true;
 el('review-labels').value='Interesting';
 await el('review-label-aircraft').onclick();await settle();
 assert.equal(ctx.reviewEvent.id,'0021','Category saves and advances in current order');
 assert.deepEqual(Array.from(records[20].review.labels),['interesting','aircraft']);
 assert.equal(records[20].review.notes,'Save this note');assert(records[20].review.protected);
 assert(!records[20].review.reviewed,'Categorizing does not silently mark reviewed');
 ctx.openReview(records[20]);await settle();assert.equal(el('review-label-aircraft')['aria-pressed'],'true');
 await el('review-label-aircraft').onclick();await settle();assert(!records[20].review.labels.includes('aircraft'));
 await el('review-label-ignore').onclick();await settle();assert.equal(records[21].review.trashed_at,null);assert(records[21].review.labels.includes('ignore'));
 const failedId=ctx.reviewEvent.id;
 failUpdate=true;await el('review-label-lightning').onclick();failUpdate=false;
 assert(!ctx.reviewEvent.review.labels.includes('lightning'));assert.equal(el('review-label-lightning')['aria-pressed'],'false');
 assert(el('review-save-status').textContent.includes('rejected'));assert.equal(ctx.reviewEvent.id,failedId,'Failed saves must not advance');
 el('review-labels').value='a,b,c,d,e,f,g,h';before=updates;await el('review-label-weather').onclick();assert.equal(updates,before);
 ctx.openReview({...records[20],review:{...records[20].review,trashed_at:1}});await settle();assert(el('review-label-ignore').disabled);
 ctx.openReview({...records[20],review:{...records[20].review,annotation_error:true}});await settle();assert(el('review-label-ignore').disabled);
 ctx.openReview(records[0]);await settle();assert(el('review-event-prev').disabled);
 ctx.openReview(records[229]);await settle();assert(el('review-event-next').disabled);await el('review-label-weather').onclick();assert.equal(ctx.reviewEvent.id,'0229');assert(el('review-save-status').textContent.includes('Last matching event'));
 console.log('Event review: page boundaries, camera identity, filtered anchors, protection, restore, drafts and keyboard passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
