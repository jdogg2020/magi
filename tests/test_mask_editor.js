// Focused interaction regressions for the actual editor, without a running station.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const elements=new Map();
function element(){return {value:'',disabled:false,style:{},children:[],width:640,height:360,textContent:'',
 getContext(){return new Proxy({}, {get:(target,key)=>target[key]||(()=>{})});},
 getBoundingClientRect(){return {left:0,top:0,width:640,height:360};},
 setPointerCapture(){},focus(){},setAttribute(){},append(...v){this.children.push(...v);},replaceChildren(){this.children=[];}};}
const $=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
const ctx={$,document:{createElement:element},console};vm.createContext(ctx);
const app=fs.readFileSync(require('path').join(__dirname,'../src/magi/web/app.js'),'utf8');
vm.runInContext('let ignoreZones=[],zoneStart=null,zoneDraft=null,zoneFrameReady=true,zoneFrameURL=null;'+app.slice(app.indexOf('let polygonDraft ='),app.indexOf('setInterval(refresh,')),ctx);
const run=s=>vm.runInContext(s,ctx), pointer=(x,y)=>({clientX:x,clientY:y,pointerId:1,button:0,preventDefault(){}});
const canvas=$('zone-canvas');
function drag(x,y,endX,endY){canvas.onpointerdown(pointer(x,y));canvas.onpointermove(pointer(endX,endY));canvas.onpointerup(pointer(endX,endY));}
function clear(){ $('zone-clear').onclick();$('zone-feather').value='2'; }
clear();$('zone-new').onclick();
assert.equal($('zone-mode').value,'edit');assert.equal(run('ignoreZones.length'),1);
assert.equal(run('ignoreZones[0].feather'),.02);
// Whole-shape movement preserves every edge length and clamps beyond image.
drag(320,180,1000,800);
assert.equal(run('Math.max(...ignoreZones[0].points.map(p=>p[0]))'),1);
assert.equal(run('Math.max(...ignoreZones[0].points.map(p=>p[1]))'),1);
assert.equal(run('Math.min(...ignoreZones[0].points.map(p=>p[0]))'),.5);
// Grab a corner off-center, then pull outside both image bounds.
clear();$('zone-new').onclick();drag(165,95,-50,-50);
assert.equal(run('ignoreZones[0].points[0][0]'),0);assert.equal(run('ignoreZones[0].points[0][1]'),0);
// Grab an edge slightly inside it; snapping must use the shape, not the cursor.
clear();$('zone-new').onclick();drag(320,95,320,-50);
assert.equal(run('ignoreZones[0].points[0][1]'),0);assert.equal(run('ignoreZones[0].points[1][1]'),0);
assert(run('validPolygon(ignoreZones[0].points)'));
// Skyline starter already seals left/right/bottom frame edges.
clear();$('zone-ground').onclick();
assert.equal(run('ignoreZones[0].points[0][0]'),0);assert.equal(run('ignoreZones[0].points[3][0]'),1);
assert.equal(run('ignoreZones[0].points[3][1]'),1);
canvas.ondblclick(pointer(160,234));assert.equal(run('ignoreZones[0].points.length'),6);
// Natural closing gesture must finish rather than insert a duplicate corner.
clear();$('zone-mode').value='polygon';
for(const p of [[80,80],[400,80],[400,260],[80,260],[82,82]])canvas.onpointerdown(pointer(...p));
assert.equal(run('ignoreZones.length'),1);assert.equal(run('ignoreZones[0].points.length'),4);assert.equal(run('polygonDraft.length'),0);
clear();$('zone-mode').value='polygon';
for(const p of [[80,80],[400,80],[400,260]])canvas.onpointerdown(pointer(...p));
canvas.onkeydown({key:'Enter',preventDefault(){}});assert.equal(run('ignoreZones.length'),1);
// Older draft with an explicit closing vertex is also normalized.
clear();run('polygonDraft=[[0,0],[1,0],[1,1],[0,0]]');assert(run('finishZone()'));assert.equal(run('ignoreZones[0].points.length'),3);
// Bad outline remains editable with an actionable error; it is never saved.
clear();run('polygonDraft=[[0,0],[1,1],[0,1],[1,0]]');assert(!run('finishZone()'));assert.equal(run('ignoreZones.length'),0);assert($('zone-status').textContent.includes('Undo'));
$('zone-cancel').onclick();assert.equal(run('polygonDraft.length'),0);
// Pointer cancellation restores the exact original shape.
$('zone-new').onclick();const before=run('JSON.stringify(ignoreZones)');canvas.onpointerdown(pointer(320,180));canvas.onpointermove(pointer(500,200));canvas.onpointercancel();assert.equal(run('JSON.stringify(ignoreZones)'),before);
console.log('Mask editor: starter shapes, move, edge/corner drag beyond frame, corner insertion, close gesture, Enter, duplicate cleanup, invalid outline and cancellation passed.');
