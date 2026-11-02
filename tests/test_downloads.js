// Every availability combination uses identical descriptors for review/list.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{dataset:{},open:false,hidden:false,classList:{toggle(name,value){element(id).hidden=value;}},removeAttribute(name){delete this[name];}});return nodes.get(id);}
const ctx={selectedCamera:'primary',eventKey:row=>row.camera_id+':'+row.id,eventPath:(row,kind)=>'/api/cameras/'+row.camera_id+'/clips/'+row.id+'/'+kind,document:{getElementById:element}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('src/magi/web/events.js','utf8'),ctx);
const flags=['substream_preview','original','best_frame','thumbnail'];
for(let mask=0;mask<16;mask++){
 ctx.row={id:'20261102T120000-0123456789ab',camera_id:'cam-0123456789ab',...Object.fromEntries(flags.map((key,i)=>[key,!!(mask&(1<<i))]))};
 const choices=vm.runInContext('MagiEvents.downloadChoices(row)',ctx);
 assert.equal(choices.length,1+flags.filter(key=>ctx.row[key]).length);
 assert(choices.some(c=>c.kind==='metadata'));
 vm.runInContext('MagiEvents.updateDownloads(row)',ctx);
 for(const id of ['review-substream-download','review-original','review-json-download','review-frame-download','review-thumbnail-download']){
  const choice=choices.find(c=>c.id===id),link=element(id);
  assert.equal(link.hidden,!choice);
  if(choice){assert.equal(link.href,choice.href);assert.equal(link.download,choice.filename);assert(choice.href.includes('/cam-0123456789ab/'));}
  else {assert.equal(link.href,undefined);assert.equal(link.download,undefined);}
 }
 element('review-download-menu').open=true;vm.runInContext('MagiEvents.updateDownloads(row)',ctx);assert(element('review-download-menu').open,'Refresh must not close an active menu');
 ctx.row={...ctx.row,camera_id:'primary'};vm.runInContext('MagiEvents.updateDownloads(row)',ctx);assert(!element('review-download-menu').open,'Changing cameras closes the old event menu');
 assert(element('review-json-download').download.startsWith('primary-'));
}
console.log('Download choices: all availability combinations, missing-link cleanup, camera identity and menu refresh passed.');
