'use strict';
const assert=require('assert');
const tracks=require('../src/magi/web/tracks.js');
const segments=[{media_start:0,media_end:2,host_start_unix:100,host_end_unix:102},
 {media_start:4,media_end:6,host_start_unix:104,host_end_unix:106}];
assert.equal(tracks.clipSeconds(segments,105),3);
assert.equal(tracks.clipSeconds(segments,103),null,'Missing footage must not receive a guessed overlay time');
assert.equal(tracks.clipSeconds([{media_start:0}],101),null);
const meta={width:640,height:360};
const obj=(x,y,id='a')=>({x,y,track_id:id});
const rows=[{video_seconds:0,host_unix:100,objects:[obj(64,36)]},
 {video_seconds:.25,host_unix:100.25,objects:[obj(128,72)]},
 {video_seconds:.5,host_unix:100.5,objects:[obj(192,108)],suppressed:true},
 {video_seconds:3,host_unix:105,objects:[obj(320,180)]},
 {video_seconds:3.25,host_unix:105.25,objects:[obj(384,216)]}];
const grouped=tracks.collect(rows,meta);
assert.equal(grouped.length,2,'Reconnect/observation gaps must split the drawn path');
assert.deepEqual(grouped[0].points,[[0,64.5/640,36.5/360],[.25,128.5/640,72.5/360]]);
assert.deepEqual(tracks.collect(rows,meta,segments)[1].points,[[3,320.5/640,180.5/360],[3.25,384.5/640,216.5/360]]);
assert.equal(tracks.position(grouped[0].points,2),null,'Reticle must disappear outside observed times');
const middle=tracks.position(grouped[0].points,.125);
assert(Math.abs(middle[1]-(96.5/640))<1e-10);
assert.equal(tracks.collect([{video_seconds:0,objects:[obj(NaN,0)]}],meta).length,0);
assert.equal(tracks.collect(rows,meta,[]).length,0,'Empty color-video timing must not fall back to replay timing');
console.log('Track geometry passed: normalization, substream timing, gaps, suppression, interpolation and missing-data behavior.');

const corners=tracks.collect([
 {video_seconds:0,objects:[obj(0,0)]},
 {video_seconds:.25,objects:[obj(639,359)]}],meta)[0].points;
assert.deepEqual(corners,[[0,.5/640,.5/360],[.25,639.5/640,359.5/360]]);
assert.equal(corners[0][1]*3840,3,'4K overlay must land at the scaled pixel center');
