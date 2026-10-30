// Lightweight DOM harness for the actual dashboard JavaScript against a running
// disposable synthetic service. This checks behavior, not browser rendering.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const base = process.argv[2] || 'http://127.0.0.1:18790';
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.value=''; this.textContent=''; this.checked=false; this.disabled=false; this.open=false; this.listeners={}; this.classes=new Set(); this.dataset={}; this.classList={add:(...n)=>n.forEach(v=>this.classes.add(v)),remove:(...n)=>n.forEach(v=>this.classes.delete(v)),toggle:(n,yes)=>{if(yes ?? !this.classes.has(n)) this.classes.add(n);else this.classes.delete(n);},contains:n=>this.classes.has(n)}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  setAttribute(name,value) { this[name]=value; }
  getAttribute(name) { return this[name] ?? null; }
  removeAttribute(name) { delete this[name]; }
  showModal(){this.open=true;}
  close(){this.open=false;this.listeners.close?.();}
  getContext(){return {clearRect:()=>{this.drawn=false;},createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)}),putImageData:()=>{this.drawn=true;},strokeRect(){},fillRect(){}};}
  getBoundingClientRect(){return {left:0,top:0,width:320,height:180};}
  setPointerCapture(){}
  play(){this.playCalls=(this.playCalls||0)+1;return Promise.resolve();}
  pause(){}
  load(){}
}
const html = fs.readFileSync(path.join(__dirname,'../src/magi/web/index.html'),'utf8');
const elements={};
for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
 const el=new Element(match[1]); const attributes=match[2]; el.id=match[3];
 const classes=attributes.match(/class="([^"]*)"/); if(classes) classes[1].split(' ').forEach(c=>el.classList.add(c));
 const value=attributes.match(/value="([^"]*)"/); if(value) el.value=value[1]; el.checked=/\bchecked\b/.test(attributes);elements[el.id]=el;
}
const tabs=['overview','events','settings'].map(id=>elements[id]);
const nav=tabs.map(tab=>{const el=new Element('button');el.dataset.tab=tab.id;return el;});
const document={getElementById:id=>{assert(elements[id],`Missing HTML ID: ${id}`);return elements[id];},querySelectorAll:selector=>selector==='.tab'?tabs:selector==='.nav'?nav:[],createElement:tag=>new Element(tag),hidden:false};
let cookie='';
async function fetchWithCookies(url, options={}) {
 const headers={...(options.headers||{})}; if(cookie) headers.Cookie=cookie;
 const response=await fetch(base+url,{...options,headers});
 const setCookie=response.headers.get('set-cookie');if(setCookie) cookie=setCookie.split(';')[0];
 return response;
}
const context={document,fetch:fetchWithCookies,console,URL,JSON,Date,Math,Number,Error,encodeURIComponent,setInterval:()=>0,setTimeout,Promise,atob};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/magi/web/app.js'),'utf8'),context);
(async()=>{
 await new Promise(resolve=>setTimeout(resolve,250));
 assert(elements.login.classList.contains('hidden')===false,'Sign-in page should appear');
 elements['login-user'].value='admin';elements['login-password'].value='demo-password-123';
 await elements['login-form'].onsubmit({preventDefault(){}});
 assert(!elements.app.classList.contains('hidden'),'Dashboard should open after login: '+elements['login-error'].textContent);
 assert(elements.login.classList.contains('hidden'));
 for(let i=0;i<100;i++){await vm.runInContext('refreshFrame()',context);if(elements['camera-frame'].src)break;await new Promise(resolve=>setTimeout(resolve,100));}
 assert(elements['camera-frame'].src?.startsWith('blob:'),'Live image should load');
 assert.equal(elements['detection-mode'].textContent,'Tracking');
 for(let i=0;i<100;i++){await vm.runInContext('refresh()',context);if(elements['recent-events'].children.some(card=>{const text=card.children[1]?.children[2]?.textContent||'';return text.includes('substream video')&&text.includes('native crop');}))break;await new Promise(resolve=>setTimeout(resolve,150));}assert(elements['all-events'].children.some(el=>el.className==='event-card'),'Events should render');
 const card=elements['recent-events'].children.find(el=>{const text=el.children[1]?.children[2]?.textContent||'';return text.includes('substream video')&&text.includes('native crop');});
 assert(card,'A recent event should have its native thumbnail and substream recording');
 const view=card.children[0].children.find(el=>el.tagName==='button');
 assert(view,'Saved event should open its image');
 elements['review-video'].currentTime=17;
 await view.onclick();
 assert(!elements['review-still'].classList.contains('hidden'),'Opening an event must show its image first');
 assert(elements['review-still'].src.includes('/thumbnail'),'Image should be the native-resolution thumbnail');
 assert(elements['review-video'].classList.contains('hidden'),'Video should stay hidden until Play');
 assert(!elements['review-video'].playCalls,'Opening the image must not autoplay');
 elements['review-play'].onclick();
 assert.equal(elements['review-video'].currentTime,0,'Play must start at the beginning');
 assert(!elements['review-video'].classList.contains('hidden'));
 assert(elements['review-still'].classList.contains('hidden'));
 if(vm.runInContext('reviewEvent.substream_preview',context)){assert.equal(elements['review-quality'].value,'substream');assert(!elements['motion-overlay'].drawn,'Substream video must not show misaligned detection overlays');}
 elements['review-quality'].value='analysis';elements['review-quality'].onchange();
 assert(elements['motion-overlay'].drawn,'Changed pixel overlay should draw');elements['show-pixels'].checked=false;elements['show-pixels'].onchange();assert(!elements['motion-overlay'].drawn,'Highlights should turn off');elements['show-pixels'].checked=true;elements['show-pixels'].onchange();assert(elements['motion-overlay'].drawn,'Highlights should turn back on');
 assert(elements.review.open,'Review dialog should open');assert(elements['review-video'].src.endsWith('/preview'));
 elements['review-photo'].onclick();
 assert(!elements['review-still'].classList.contains('hidden'),'View image should return to the still');
 const calls=elements['review-video'].playCalls;
 elements['review-quality'].onchange();
 assert.equal(elements['review-video'].playCalls,calls,'Changing video choice while viewing the image must not autoplay');
 if(vm.runInContext('reviewEvent.best_frame',context)) {
   elements['review-full-frame'].onclick();
   assert(elements['review-still'].src.includes('/frame?'),'Full frame should open inline');
 }
 elements['review-play'].onclick();assert.equal(elements['review-video'].currentTime,0);
 elements['review-close'].onclick();assert(!elements.review.open);
 await view.onclick();
 assert(!elements['review-still'].classList.contains('hidden'),'Reopening an event must start with its image again');
 elements['review-close'].onclick();
 // Check setup navigation and save feedback without changing the disposable service config.
 vm.runInContext("renderStatus({state:'setup_required', health:{}, analysis:{}})",context);
 const setupLink=elements.notice.children.find(el=>el.tagName==='a');
 assert(setupLink,'Setup message should link to Settings');
 setupLink.onclick({preventDefault(){}});assert(!elements.settings.classList.contains('hidden'));
 elements['zone-frame'].onload();
 const pointer=(x,y)=>({clientX:x,clientY:y,pointerId:1,button:0,preventDefault(){}});
 elements['zone-canvas'].onpointerdown(pointer(32,18));elements['zone-canvas'].onpointermove(pointer(160,90));elements['zone-canvas'].onpointerup(pointer(160,90));
 assert.equal(vm.runInContext('ignoreZones.length',context),1,'Drawing should add an ignore rectangle');
 assert.equal(vm.runInContext('ignoreZones[0].x',context),.1);
 elements['zone-list'].children[0].children[1].onclick();assert.equal(vm.runInContext('ignoreZones.length',context),0);
 elements['zone-x'].value='20';elements['zone-y'].value='30';elements['zone-width'].value='10';elements['zone-height'].value='20';elements['zone-add'].onclick();
 assert.equal(vm.runInContext('ignoreZones.length',context),1,'Keyboard coordinates should add a zone');
 let savedPayload;
 const originalFetch=context.fetch;
 context.fetch=async(url,options)=>{if(url==='/api/settings' && options?.method==='POST'){savedPayload=JSON.parse(options.body);return {ok:true,status:200,json:async()=>({ok:true})};}return originalFetch(url,options);};
 const submit=new Element('button');
 await elements['settings-form'].onsubmit({preventDefault(){},submitter:submit});
 assert(!elements.overview.classList.contains('hidden'),'Successful save should open camera overview');
 assert(elements.settings.classList.contains('hidden'));
 assert(/Recording is active|Connecting to your camera/.test(elements.notice.textContent),'Overview should show connection outcome');
 assert(!submit.disabled);assert.equal(savedPayload.detection.ignore_zones.length,1,'Saved settings should include zones');
 elements['zone-clear'].onclick();assert.equal(vm.runInContext('ignoreZones.length',context),0);
 vm.runInContext("tab('settings')",context);
 context.fetch=async(url,options)=>url==='/api/settings' && options?.method==='POST'
   ? {ok:false,status:400,json:async()=>({error:'Invalid camera settings'})} : originalFetch(url,options);
 await elements['settings-form'].onsubmit({preventDefault(){},submitter:submit});
 assert(!elements.settings.classList.contains('hidden'),'Failed save should stay on Settings');
 assert.equal(elements['settings-result'].textContent,'Invalid camera settings');
 assert(!submit.disabled);context.fetch=originalFetch;
 await elements.logout.onclick();assert(!elements.login.classList.contains('hidden'));
 console.log('Dashboard behavior passed: sign-in, status, settings load, live frame, event cards, preview dialog, setup links, save navigation/status, save failure, ignore zones, image-first review, playback from zero, full-frame viewing, substream selection and detection overlay mode, sign-out.');
})().catch(error=>{console.error(error);process.exitCode=1;});
