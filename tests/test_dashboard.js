// Lightweight DOM harness for the actual dashboard JavaScript against a running
// disposable synthetic service. This checks behavior, not browser rendering.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const cameraId = process.argv[3] || 'primary';
const base = process.argv[2] || 'http://127.0.0.1:18790';
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.value=''; this.textContent=''; this.checked=false; this.disabled=false; this.open=false; this.listeners={}; this.classes=new Set(); this.dataset={}; this.style={}; this.classList={add:(...n)=>n.forEach(v=>this.classes.add(v)),remove:(...n)=>n.forEach(v=>this.classes.delete(v)),toggle:(n,yes)=>{if(yes ?? !this.classes.has(n)) this.classes.add(n);else this.classes.delete(n);},contains:n=>this.classes.has(n)}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  setAttribute(name,value) { this[name]=value; }
  getAttribute(name) { return this[name] ?? null; }
  removeAttribute(name) { delete this[name]; }
  showModal(){this.open=true;}
  close(){this.open=false;this.listeners.close?.();}
  getContext(){return {clearRect:()=>{this.drawn=false;this.pixelDrawn=false;this.boxDrawn=false;},createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)}),putImageData:()=>{this.drawn=true;this.pixelDrawn=true;},strokeRect:()=>{this.boxDrawn=true;},fillRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},setLineDash(){},fillText(){}};}
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
const context={location:{href:base+'/?camera='+cameraId},document,fetch:fetchWithCookies,console,URL,JSON,Date,Math,Number,Error,encodeURIComponent,setInterval:()=>0,setTimeout,Promise,atob};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/magi/web/tracks.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/magi/web/events.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/magi/web/app.js'),'utf8'),context);
(async()=>{
 for(let i=0;i<100 && elements.login.classList.contains('hidden');i++)await new Promise(resolve=>setTimeout(resolve,50));
 assert(elements.login.classList.contains('hidden')===false,'Sign-in page should appear after session lookup');
 elements['login-user'].value='admin';elements['login-password'].value='demo-password-123';
 await elements['login-form'].onsubmit({preventDefault(){}});
 assert(!elements.app.classList.contains('hidden'),'Dashboard should open after login: '+elements['login-error'].textContent);
 assert(elements.login.classList.contains('hidden'));
 for(let i=0;i<100;i++){await vm.runInContext('refreshFrame()',context);if(elements['camera-frame'].src)break;await new Promise(resolve=>setTimeout(resolve,100));}
 assert.equal(elements['camera-select'].value,cameraId,'Camera selection must follow the page URL');
 assert(elements['camera-grid'].children.length>=2,'Station should show both camera workers');
 assert.equal(vm.runInContext("cameraPath('/api/settings')",context),cameraId==='primary'?'/api/settings':`/api/cameras/${cameraId}/settings`);
 assert.equal(vm.runInContext("cameraPath('/api/session')",context),'/api/session','Sign-in remains station-wide');
 assert(elements['camera-frame'].src?.startsWith('blob:'),'Live image should load');
 assert.equal(elements['detection-mode'].textContent,'Tracking');
 for(let i=0;i<100;i++){await vm.runInContext('refresh()',context);if(elements['recent-events'].children.some(card=>{const text=card.children[1]?.children[2]?.textContent||'';return text.includes('substream video')&&text.includes('native crop');}))break;await new Promise(resolve=>setTimeout(resolve,150));}await vm.runInContext('MagiEvents.refresh(true)',context);assert(elements['all-events'].children.some(el=>el.className==='event-row'),'Events should render as rows');
 const card=elements['recent-events'].children.find(el=>{const text=el.children[1]?.children[2]?.textContent||'';return text.includes('substream video')&&text.includes('native crop');});
 assert(card,'A recent event should have its native thumbnail and substream recording');
 const view=card.children[0].children.find(el=>el.tagName==='button');
 assert(view,'Saved event should open its image');
 elements['review-video'].currentTime=17;
 await view.onclick();
 assert(!elements['review-still'].classList.contains('hidden'),'Opening an event must show its image first');
 const networkFetch=context.fetch;
 context.fetch=async(url,options)=>{const response=await networkFetch(url,options);if(url.endsWith('/settings')){const data=await response.json();return {ok:true,json:async()=>({...data,camera_setup:{suggested_network:'192.168.86.0/24'}})};}return response;};
 elements['setup-network'].value='';await vm.runInContext('fillSettings()',context);
 assert.equal(elements['setup-network'].value,'192.168.86.0/24');
 elements['setup-network'].value='192.168.7.0/24';await vm.runInContext('fillSettings()',context);
 assert.equal(elements['setup-network'].value,'192.168.7.0/24','Settings refresh must preserve the entered search network');
 context.fetch=networkFetch;
 const savedFetch=context.fetch;
 context.fetch=async()=>({ok:true,json:async()=>({status:'disabled',measurements:{status:'available',tracks:[{track_id:'fixture',sampled_path_angle_deg:12,duration_seconds:2,mean_path_speed_deg_s:6}]}})});
 await vm.runInContext('loadIdentification(reviewEvent.id)',context);
 assert(elements['identification-detail'].textContent.includes('6.00°/s mean angular speed'),'Review should summarize calibrated measurements even with reference checks disabled');
 assert(elements['identification-detail'].textContent.includes('uncertainty unverified'));
 context.fetch=savedFetch;

 assert(elements['review-still'].src.includes('/thumbnail'),'Image should be the native-resolution thumbnail');
 assert(elements['review-video'].classList.contains('hidden'),'Video should stay hidden until Play');
 assert(!elements['review-video'].playCalls,'Opening the image must not autoplay');
 elements['review-play'].onclick();
 assert.equal(elements['review-video'].currentTime,0,'Play must start at the beginning');
 assert(!elements['review-video'].classList.contains('hidden'));
 assert(elements['review-still'].classList.contains('hidden'));
 if(vm.runInContext('reviewEvent.substream_preview',context)){assert.equal(elements['review-quality'].value,'substream');assert(!elements['motion-overlay'].pixelDrawn,'Substream video must not show exact pixel masks');}
 assert(!elements['show-tracker'].disabled,'Substream should have estimated object tracking');
 assert(vm.runInContext('reviewColorTracks.length > 0',context),'Substream tracks should map through segment timing');
 elements['show-tracker'].checked=false;elements['show-tracker'].onchange();
 assert(!elements['motion-overlay'].boxDrawn,'Tracker should toggle off');
 elements['show-tracker'].checked=true;elements['show-tracker'].onchange();
 elements['review-quality'].value='analysis';elements['review-quality'].onchange();
 elements['track-timeline'].value='1.5';elements['track-timeline'].oninput();assert.equal(elements['review-video'].currentTime,1.5);
 assert(!elements['track-timeline'].disabled,'Analysis track timeline should be available');
 assert(elements['track-bands'].children.length>0,'Timeline must show recorded detection intervals');
 assert(elements['motion-overlay'].drawn,'Changed pixel overlay should draw');elements['show-pixels'].checked=false;elements['show-pixels'].onchange();assert(!elements['motion-overlay'].pixelDrawn,'Highlights should turn off');elements['show-pixels'].checked=true;elements['show-pixels'].onchange();assert(elements['motion-overlay'].drawn,'Highlights should turn back on');
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
 elements['star-latitude'].value='40';elements['star-longitude'].value='-98';elements['star-elevation'].value='500';
 // Operator review metadata and bulk actions round-trip through the real API.
 await view.onclick();
 const managedEventId=vm.runInContext('reviewEvent.id',context);
 elements['review-labels'].value='aircraft, regression-review-label';elements['review-notes'].value='UI regression notes';elements['review-reviewed'].checked=true;
 await elements['review-save'].onclick();assert(elements['review-save-status'].textContent.includes('saved'));
 let managed=await vm.runInContext("api('/api/events/list',{q:'regression-review-label'})",context);assert(managed.events.some(e=>e.id===managedEventId && e.review.reviewed && e.review.notes==='UI regression notes'));
 await vm.runInContext('MagiEvents.refresh(true)',context);
 const selectManaged=()=>{const row=elements['all-events'].children.find(r=>r.dataset.eventId===managedEventId);assert(row,'Managed row should be visible');const checkbox=row.children[0].children[0];checkbox.checked=true;checkbox.onchange();};
 selectManaged();elements['event-action'].value='protect';await elements['event-apply'].onclick();
 managed=await vm.runInContext("api('/api/events/list',{q:'regression-review-label'})",context);assert(managed.events[0].review.protected);
 selectManaged();elements['event-action'].value='unprotect';await elements['event-apply'].onclick();
 selectManaged();elements['event-action'].value='trash';await elements['event-apply'].onclick();assert(elements['event-action-status'].textContent.includes('Updated 1 event'));
 elements['event-folder'].value='trash';await elements['event-filters'].onsubmit({preventDefault(){}});
 const trashed=elements['all-events'].children.find(r=>r.dataset.eventId===managedEventId);assert(trashed,'Trash folder should show the event');
 const restore=trashed.children[7].children.find(b=>b.textContent==='Restore');await restore.onclick();
 elements['event-folder'].value='active';await elements['event-filters'].onsubmit({preventDefault(){}});
 managed=await vm.runInContext("api('/api/events/list',{q:'regression-review-label'})",context);assert.equal(managed.events[0].review.trashed_at,null);
 elements['review-close'].onclick();
 await elements['star-refresh'].onclick();
 assert(elements['star-event'].children.length>0,'Star calibration should list full-resolution saved frames');
 await elements['star-load'].onclick();
 assert(!elements['star-editor'].classList.contains('hidden'),'Frozen calibration image should open');
 assert(elements['star-name'].children.length>0,'Visible named stars should be available');
 const starNames=elements['star-name'].children.map(option=>option.value);
 for(let i=0;i<6;i++) {elements['star-name'].value=starNames[i];elements['star-x'].value=String(10+i*14);elements['star-y'].value=String(10+i*14);elements['star-add'].onclick();}
 assert.equal(elements['star-points'].children.length,6);
 elements['star-zoom'].value='2';elements['star-zoom'].onchange();assert.equal(elements['star-surface'].style.width,'200%');
 await elements['star-solve'].onclick();
 assert(elements['star-apply'].disabled,'A collinear/misidentified star fit must not be applied');
 assert(elements['star-status'].textContent.includes('Spread'),'Rejected fit should explain how to improve it');
 elements['star-clear'].onclick();assert.equal(elements['star-points'].children.length,0);
 vm.runInContext("renderStatus({state:'buffering', health:{}, analysis:{state:'substream_probe_failed',width:320,height:180,fps:8,probe_error:'Camera did not answer in time.',retry_in_seconds:7}})",context);
 assert.equal(elements['analysis-state'].textContent,'Motion detection paused');
 assert(elements['analysis-diagnostic'].textContent.includes('in 7 seconds'));
 assert(elements['analysis-diagnostic'].textContent.includes('Resolution unknown'));
 assert(elements['analysis-diagnostic'].textContent.includes('Main stream recording continues'));
 vm.runInContext("renderStatus({state:'buffering', health:{}, analysis:{state:'analyzing',width:640,height:360,fps:20}})",context);
 assert(elements['analysis-diagnostic'].classList.contains('hidden'));
 assert(elements['analysis-state'].textContent.includes('640×360 / 20 fps'));
 // Read-only camera information and safe local credential import.
 vm.runInContext("renderStatus({state:'buffering',health:{},camera_info:{state:'partial',message:'Some information unavailable',streams:{sub:{width:640,height:360,fps:10,codec:'h264',bitrate_kbps:256}},unavailable_commands:['GetIsp']},analysis:{state:'substream_probe_failed'}})",context);
 assert(elements['camera-info-observed'].textContent.includes('resolution unknown'));
 assert(elements['camera-info-details'].children.some(p=>p.children.some(v=>typeof v==='string' && v.includes('640×360'))));
 elements['opensky-credentials-file'].files=[{size:80,text:async()=>JSON.stringify({clientId:'test-import-id',clientSecret:'test-import-secret'})}];
 await elements['opensky-credentials-file'].onchange();
 assert.equal(elements['id-opensky_client_id'].value,'test-import-id');assert.equal(elements['id-opensky_client_secret'].value,'test-import-secret');
 assert.equal(elements['id-aircraft_provider'].value,'opensky');assert(elements['opensky-import-status'].textContent.includes('Save Settings'));
 elements['opensky-credentials-file'].files=[{size:10,text:async()=>'{bad'}];await elements['opensky-credentials-file'].onchange();assert(elements['opensky-import-status'].textContent.includes('not a valid JSON'));
 elements['id-opensky_client_id'].value='';elements['id-opensky_client_secret'].value='';
 // Inspection validation errors must survive background status refreshes.
 elements['setup-initialize'].checked=true;elements['setup-new-password'].value='';elements['setup-confirm-password'].value='';
 await elements['setup-inspect'].onclick();
 assert(elements['setup-status'].textContent.includes('before inspection'));
 elements['setup-initialize'].checked=true;elements['setup-new-password'].value='new-camera-pass';elements['setup-confirm-password'].value='different-password';
 await elements['setup-inspect'].onclick();
 assert(elements['setup-status'].textContent.includes('do not match'));
 vm.runInContext("renderCameraSetup({camera_setup:{state:'discovered',message:'Found 1 compatible setup service'}})",context);
 assert(elements['setup-status'].textContent.includes('do not match'),'Discovery refresh must not hide inspection errors');
 assert(elements['setup-apply'].disabled);
 elements['setup-confirm-password'].listeners.input();
 vm.runInContext("renderCameraSetup({camera_setup:{state:'discovered',message:'Found 1 compatible setup service'}})",context);
 assert(elements['setup-status'].textContent.includes('Found 1'));
 // Camera mutation requires a matching inspected connection; edits invalidate Apply.
 elements.host.value='192.168.86.242';elements.username.value='admin';elements['camera-password'].value='';elements['setup-initialize'].checked=false;
 vm.runInContext("setupSignature=JSON.stringify(setupFields());renderCameraSetup({camera_setup:{state:'review',token:'test-token',host:'192.168.86.242',identity:{model:'CX820'},ports:{http:{port:80,enabled:false}}}})",context);
 assert(!elements['setup-apply'].disabled);elements.host.value='192.168.86.243';vm.runInContext("renderCameraSetup({camera_setup:{state:'review',token:'test-token'}})",context);assert(elements['setup-apply'].disabled);
 await elements['camera-info-read'].onclick();
 for(let i=0;i<30;i++){await vm.runInContext('refresh()',context);if(elements['camera-info-status'].textContent.includes('test video'))break;await new Promise(resolve=>setTimeout(resolve,100));}
 assert(elements['camera-info-status'].textContent.includes('test video'),'Synthetic camera should explain why API inspection is unavailable');
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
 context.fetch=async(url,options)=>{if(url.endsWith('/settings') && options?.method==='POST'){savedPayload=JSON.parse(options.body);return {ok:true,status:200,json:async()=>({ok:true})};}return originalFetch(url,options);};
 const submit=new Element('button');
 await elements['settings-form'].onsubmit({preventDefault(){},submitter:submit});
 assert(!elements.overview.classList.contains('hidden'),'Successful save should open camera overview');
 assert(elements.settings.classList.contains('hidden'));
 assert(/Recording is active|Connecting to your camera/.test(elements.notice.textContent),'Overview should show connection outcome');
 assert(!submit.disabled);assert.equal(savedPayload.detection.ignore_zones.length,1,'Saved settings should include zones');assert.equal(savedPayload.detection.lightning_enabled,true);assert.equal(savedPayload.detection.meteor_enabled,true);assert(savedPayload.detection.retain_types.includes('meteor_candidate'));
 elements['zone-clear'].onclick();assert.equal(vm.runInContext('ignoreZones.length',context),0);
 vm.runInContext("tab('settings')",context);
 context.fetch=async(url,options)=>url.endsWith('/settings') && options?.method==='POST'
   ? {ok:false,status:400,json:async()=>({error:'Invalid camera settings'})} : originalFetch(url,options);
 await elements['settings-form'].onsubmit({preventDefault(){},submitter:submit});
 assert(!elements.settings.classList.contains('hidden'),'Failed save should stay on Settings');
 assert.equal(elements['settings-result'].textContent,'Invalid camera settings');
 assert(!submit.disabled);context.fetch=originalFetch;
 elements['camera-name'].value='Renamed '+cameraId;
 await elements['camera-rename'].onclick();
 assert.equal(elements['selected-camera-name'].textContent,'Renamed '+cameraId);
 const inventory=await vm.runInContext("api('/api/cameras')",context);
 assert(inventory.cameras.some(c=>c.id===cameraId && c.name==='Renamed '+cameraId));
 elements['camera-new-name'].value='New setup camera';
 await elements['camera-add'].onclick();
 assert(context.location.href.endsWith('#settings'),'Adding a camera should open its own setup screen');
 assert(context.location.href.includes('/?camera=cam-'));
 await elements.logout.onclick();assert(!elements.login.classList.contains('hidden'));
 console.log('Dashboard '+cameraId+' behavior passed: sign-in, status, settings load, live frame, event cards, preview dialog, setup links, save navigation/status, save failure, ignore zones, image-first review, playback from zero, full-frame viewing, substream track timing, tracker toggles, detection timeline exact pixel overlay mode, assisted star selection and invalid-fit rejection, event rows, operator labels/notes, bulk protection, trash/restore, sign-out.');
})().catch(error=>{console.error(error);process.exitCode=1;});
