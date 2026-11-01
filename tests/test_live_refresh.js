// Exercise the actual refresh functions without camera hardware or a web server.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/magi/web/app.js','utf8');
function element(){return {hidden:false,src:'old',classList:{add(){},remove(){}},removeAttribute(){this.src=null;}};}
const timers=[],revoked=[];
let fetchImpl;
const context={AbortController,Promise,Map,URL:{createObjectURL:()=>`blob:${Math.random()}`,revokeObjectURL:url=>revoked.push(url)},
 setTimeout:fn=>(timers.push(fn),timers.length),clearTimeout:()=>{},
 fetch:(...args)=>fetchImpl(...args),csrf:'session',cameraPath:(path,id)=>id+path};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const frameRequests ='),source.indexOf('const settingsFrame =')),context);
const update=(id,img,owner)=>context.updateLiveFrame(id,img,element(),owner);
(async()=>{
 const a=element(),b=element(),ownerA={url:'old-a'},ownerB={url:'old-b'};
 let bRequests=0;
 fetchImpl=async(url,options)=>{
   assert.equal(options.cache,'no-store');
   if(url.startsWith('a'))return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
   bRequests++;return {ok:true,blob:async()=>({})};
 };
 const pending=update('a',a,ownerA);
 await update('b',b,ownerB);const first=b.src;
 await update('b',b,ownerB);
 assert.equal(bRequests,2,'An unresponsive camera must not block another refresh');
 assert.notEqual(b.src,first);
 await update('a',a,ownerA); // Duplicate request is skipped, not queued.
 timers[0]();await pending;
 assert.equal(a.src,null,'Timeout removes the misleading old image');
 assert.equal(ownerA.url,null);
 fetchImpl=async()=>({ok:true,blob:async()=>({})});
 await update('a',a,ownerA);
 assert(a.src.startsWith('blob:'),'Next poll recovers after timeout');
 // A stalled response body is covered by the same deadline.
 fetchImpl=async(url,options)=>({ok:true,blob:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('body stalled'))))});
 const body=update('a',a,ownerA);await new Promise(resolve=>setImmediate(resolve));timers.at(-1)();await body;
 assert.equal(a.src,null);
 assert(revoked.includes('old-a')&&revoked.includes('old-b'));
 // Tab navigation updates the refresh destination, preserving the camera query.
 let fragment;
 context.history={replaceState:(state,title,url)=>fragment=url};
 context.document={querySelectorAll:()=>[]};context.MagiEvents={mount:()=>{},refresh:()=>{}};
 vm.runInContext(source.slice(source.indexOf('function tab(name)'),source.indexOf("document.querySelectorAll('.nav').forEach(el => el.addEventListener")),context);
 context.tab('events');assert.equal(fragment,'#events');
 context.tab('overview');assert.equal(fragment,'#overview');
 console.log('Live refresh isolation, timeout, recovery, body timeout and navigation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
