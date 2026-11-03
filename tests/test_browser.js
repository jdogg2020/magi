// Real Chromium rendering and camera routing against disposable synthetic feeds.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const base = process.argv[2];
(async()=>{
  const executable=process.env.MAGI_CHROMIUM_EXECUTABLE || ['/usr/bin/google-chrome','/usr/bin/google-chrome-stable'].find(path=>fs.existsSync(path));
  const browser = await chromium.launch({headless:true,...(executable ? {executablePath:executable} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1600,height:1100}});
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.goto(base);
    await page.locator('#login-user').fill('admin');
    await page.locator('#login-password').fill('demo-password-123');
    await page.locator('#login-form button').click();
    await page.locator('#camera-grid img:not(.hidden)').nth(1).waitFor();
    assert.equal(await page.locator('header select').count(),0);
    assert.equal(await page.locator('#camera-grid img:visible').count(),2);
    await page.waitForFunction(()=>[...document.querySelectorAll('#camera-grid img:not(.hidden)')].every(img=>img.complete && img.naturalWidth>0));
    const images=await page.locator('#camera-grid img:visible').evaluateAll(images=>images.every(img=>img.complete && img.naturalWidth>0));
    assert(images,'Both synthetic images must decode');
    assert(await page.locator('aside #camera-links a').count()>=2);
    await page.evaluate(()=>{
      window.stationTextViolations=[];
      const formats={connection:/^\d+\/\d+ cameras recording$/,buffer:/^\d+ \/ \d+$/,'detection-mode':/^\d+ detecting$/};
      const observer=new MutationObserver(()=>{for(const [id,format] of Object.entries(formats)){const text=document.getElementById(id).textContent;if(!format.test(text))window.stationTextViolations.push({id,text});}});
      for(const id of Object.keys(formats))observer.observe(document.getElementById(id),{childList:true,characterData:true,subtree:true});
      window.stationTextObserver=observer;
    });
    await page.waitForTimeout(4500); // Observe more than two normal dashboard refreshes.
    assert.deepEqual(await page.evaluate(()=>{window.stationTextObserver.disconnect();return window.stationTextViolations;}),[],'Station indicators must not flash per-camera labels between polls');

    fs.mkdirSync('dist/browser',{recursive:true});

    const maskRoute=async route=>{
      const response=await route.fetch(), data=await response.json();
      data.cameras.forEach((camera,index)=>{camera.ignore_zones=index===0?[{x:.69,y:.48,width:.3083,height:.52}]:[];});
      await route.fulfill({response,json:data});
    };
    await page.route('**/api/cameras',maskRoute);
    await page.evaluate(()=>refreshCameras());
    const maskTile=page.locator('#camera-grid article').first();
    const mask=maskTile.locator('canvas');await mask.waitFor({state:'visible'});
    assert.equal(await page.locator('#camera-grid article').nth(1).locator('canvas').isVisible(),false);
    const dimensions=await mask.evaluate(c=>({w:c.width,h:c.height,rect:c.getBoundingClientRect().toJSON()}));
    const imageDimensions=await maskTile.locator('img').evaluate(i=>({w:i.naturalWidth,h:i.naturalHeight,rect:i.getBoundingClientRect().toJSON()}));
    assert.equal(dimensions.w,imageDimensions.w);assert.equal(dimensions.h,imageDimensions.h);
    assert(Math.abs(dimensions.rect.width-imageDimensions.rect.width)<1);
    assert(Math.abs(dimensions.rect.height-imageDimensions.rect.height)<1);
    await page.screenshot({path:'dist/browser/overview-masks.png',fullPage:true});
    await maskTile.getByLabel('Show ignore zones').uncheck();assert(!await mask.isVisible());
    await page.evaluate(()=>refreshCameras());assert(!await mask.isVisible(),'Polling preserves display toggle');
    await maskTile.getByLabel('Show ignore zones').check();assert(await mask.isVisible());
    await page.unroute('**/api/cameras',maskRoute);
    await page.evaluate(()=>refreshCameras());
    await page.screenshot({path:'dist/browser/overview.png',fullPage:true});
    await page.locator('.nav[data-tab=storage]').click();
    await page.locator('#storage-status').filter({hasText:'Removed-camera archives:'}).waitFor();
    assert.equal(await page.locator('#storage-volumes p').count(),1);
    assert(await page.locator('#storage-cameras article').count()>=2);
    await page.screenshot({path:'dist/browser/storage.png',fullPage:true});
    await page.locator('#storage-cameras a').first().click();
    await page.locator('#cleanup_gb').waitFor({state:'visible'});
    assert.equal(await page.locator('#trash_days').inputValue(),'7');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'max_gb');
    assert.equal(new URL(page.url()).hash,'#storage-limits');
    await page.locator('.nav[data-tab=overview]').click();
    assert.equal(await page.locator('#overview #event-browser').count(),1,'Recent events must use the full list');
    assert(!await page.locator('#event-filter-panel').evaluate(panel=>panel.open),'Filters start collapsed');
    await page.locator('#event-filter-summary').click();
    await page.getByRole('button',{name:'Camera',exact:true}).click();
    const primaryName=await page.locator('#event-camera option[value=primary]').textContent();
    await page.getByRole('combobox',{name:'Search camera',exact:true}).fill(primaryName);
    await page.locator('#event-camera-choices').getByRole('option',{name:primaryName,exact:true}).click();
    await page.locator('#event-filters button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('event-filter-summary').textContent.includes('1 active'));
    await page.locator('#event-filter-summary').click();
    assert(await page.locator('#event-filter-summary').textContent().then(text=>text.includes('1 active')));
    await page.locator('#view-all').click();
    assert.equal(await page.locator('#events #event-browser').count(),1);
    assert.equal(await page.locator('#event-camera').inputValue(),'primary','Filters survive navigation');
    await page.locator('#event-filter-summary').click();
    await page.locator('#event-reset').click();
    await page.locator('#event-filter-summary').click();
    await page.locator('.nav[data-tab=overview]').click();
    await page.locator('.nav[data-tab=updates]').click();
    await page.locator('#update-status').filter({hasText:'helper is not available'}).waitFor();
    assert(await page.locator('#update-check').isDisabled());
    assert(await page.locator('#update-install').isDisabled());
    assert.equal(await page.locator('#updates input[type=password]').count(),0);
    await page.screenshot({path:'dist/browser/updates.png',fullPage:true});
    await page.locator('.nav[data-tab=overview]').click();
    // Capture requests to prove the tile's manual-save action uses its camera.
    const tile=page.locator('#camera-grid article').nth(1);
    const camera=await tile.getAttribute('data-camera-id');
    const trigger=page.waitForRequest(request=>request.url().endsWith(`/api/cameras/${camera}/trigger`));
    await tile.getByRole('button',{name:'Save an event'}).click();
    await trigger;
    await page.locator('.nav[data-tab=events]').click();
    await page.waitForFunction(()=>new Set([...document.querySelectorAll('#all-events .event-row')].map(row=>row.dataset.cameraId)).size===2);
    assert((await page.locator('.event-table th').allTextContents()).includes('Camera'));
    assert.equal(await page.locator('#event-camera').inputValue(),'all');
    await page.waitForFunction(()=>[...document.querySelectorAll('#all-events .event-row')].some(row=>row.dataset.cameraId!=='primary' && row.querySelector('img') && row.cells[7].firstChild.textContent==='complete'));
    const rows=await page.locator('#all-events .event-row').evaluateAll(rows=>rows.map(row=>({id:row.dataset.eventId,camera:row.dataset.cameraId,hasImage:!!row.querySelector('img'),hasVideo:row.textContent.includes('complete')})));
    assert.equal(new Set(rows.map(row=>row.camera)).size,2,'Shared list must show both cameras');
    await page.screenshot({path:'dist/browser/events.png',fullPage:true});
    const event=rows.find(row=>row.camera!=='primary' && row.hasImage && row.hasVideo) || rows.find(row=>row.camera!=='primary' && row.hasImage);
    const eventRow=page.locator(`#all-events tr[data-camera-id="${event.camera}"][data-event-id="${event.id}"]`);
    await eventRow.getByRole('button',{name:'Review',exact:true}).click();
    await page.locator('#review').waitFor();
    for(const id of ['review-event-prev','review-event-next']){
      assert.equal(await page.locator('#'+id).count(),1);
      assert(await page.locator('#'+id).isVisible());
      assert(await page.locator('#'+id).evaluate(button=>!!(button.compareDocumentPosition(document.getElementById('review-label-weather')) & Node.DOCUMENT_POSITION_FOLLOWING)),'Navigation must be above categories');
    }
    const still=page.locator('#review-still');
    const src=await still.getAttribute('src');
    assert(src.includes(`/api/cameras/${event.camera}/clips/${event.id}/`));
    await still.waitFor({state:'visible'});
    await page.waitForFunction(()=>{const image=document.getElementById('review-still');return image.complete && image.naturalWidth>0;});
    await page.screenshot({path:'dist/browser/full-frame-review.png',fullPage:true});
    const fullImage=await page.evaluate(()=>({size:[document.getElementById('review-still').naturalWidth,document.getElementById('review-still').naturalHeight],detail:reviewEvent.media_detail,best:reviewEvent.best_frame}));
    if(fullImage.best)assert.deepEqual(fullImage.size,[fullImage.detail.native_width,fullImage.detail.native_height],'Displayed still must retain native full-frame dimensions');
    await page.locator('#radio-report').waitFor({state:'visible'});
    assert((await page.locator('#radio-status').textContent()).includes('aircraft recorded'));
    const radioResponse=await page.request.get(new URL(await page.locator('#radio-report').getAttribute('href'),base).href);
    assert(radioResponse.ok());assert.equal((await radioResponse.json()).source,'local_readsb');
    assert(await still.isVisible());
    assert(!await page.locator('#review-video').isVisible());
    const menu=page.locator('#review-download-menu');
    const listDownloads=await eventRow.locator('.download-options a').evaluateAll(links=>links.map(a=>({text:a.textContent,href:a.getAttribute('href'),filename:a.download})));
    const reviewDownloads=await menu.locator('.download-options a:not(.hidden)').evaluateAll(links=>links.map(a=>({text:a.textContent,href:a.getAttribute('href'),filename:a.download})));
    assert.deepEqual(reviewDownloads,listDownloads,'Review and event list must offer the same files');
    assert(reviewDownloads.some(item=>item.text==='Small video (substream)'));
    assert(reviewDownloads.some(item=>item.text==='Event JSON'));
    for(const choice of reviewDownloads){
      await menu.locator('summary').click();
      const downloading=page.waitForEvent('download');
      await menu.getByRole('link',{name:choice.text,exact:true}).click();
      const download=await downloading;
      assert.equal(download.suggestedFilename(),choice.filename);
      assert.equal(await menu.getAttribute('open'),null,'Choosing a file closes the menu');
      const response=await page.request.get(new URL(choice.href,base).href);
      assert(response.ok());assert(response.headers()['content-disposition'].includes(choice.filename));
      assert.deepEqual(fs.readFileSync(await download.path()),await response.body(),'Download contains the saved file bytes');
      const source=await page.request.get(new URL(choice.href.split('?')[0],base).href);
      assert.deepEqual(await response.body(),await source.body(),'Download flag must not transform the saved file');
      const range=await page.request.get(new URL(choice.href,base).href,{headers:{Range:'bytes=0-15'}});
      assert.equal(range.status(),206);assert.deepEqual(await range.body(),(await response.body()).subarray(0,16));
      if(choice.text==='Event JSON')assert.equal((await response.json()).id,event.id);
    }
    const guest=await browser.newContext();
    try {const denied=await guest.request.get(new URL(reviewDownloads[0].href,base).href);assert.equal(denied.status(),401,'Downloads require authentication');}finally{await guest.close();}
    const missing=await page.request.get(new URL(reviewDownloads[0].href.replace(event.id,'20000101T000000-000000000000'),base).href);assert.equal(missing.status(),404);
    await menu.locator('summary').focus();await page.keyboard.press('Enter');
    assert.notEqual(await menu.getAttribute('open'),null,'Download menu supports keyboard activation');
    await page.screenshot({path:'dist/browser/review-download-menu.png',fullPage:true});
    await page.keyboard.press('Enter');
    await page.locator('#review-play').click();
    assert((await page.locator('#review-video').getAttribute('src')).includes(`/api/cameras/${event.camera}/clips/${event.id}/`));
    await page.locator('#review-full-video').click();
    await page.waitForFunction(()=>document.getElementById('review-video').getAttribute('src')?.endsWith('/full-video'),{},{timeout:30000});
    const supportsH264=await page.locator('#review-video').evaluate(v=>!!v.canPlayType('video/mp4; codecs="avc1.42E01E"'));
    if(supportsH264) {
      await page.waitForFunction(()=>document.getElementById('review-video').readyState>=2);
      await page.waitForFunction(()=>document.getElementById('review-video').currentTime>0);
      console.log('Full-quality H.264 playback decoded and advanced in the browser.');
    } else {
      await page.waitForFunction(()=>document.getElementById('review-video').error?.code===4);
      assert((await page.locator('#review-video-status').textContent()).includes('original'));
      console.log('Browser has no H.264 decoder: verified explicit original-download fallback.');
    }
    assert((await page.locator('#review-original').getAttribute('href')).includes(`/api/cameras/${event.camera}/clips/${event.id}/original`));
    assert(await page.locator('#show-tracker').isDisabled());
    await page.screenshot({path:'dist/browser/review-categories.png',fullPage:true});
    const next=rows[rows.findIndex(row=>row.id===event.id && row.camera===event.camera)+1];
    let releaseArchive,releaseSave;
    const blockedSave=new Promise(resolve=>releaseSave=resolve);
    await page.route('**/api/station/events/update',async route=>{await blockedSave;await route.continue();});
    const blockedArchive=new Promise(resolve=>releaseArchive=resolve);
    const holdArchive=async route=>{await blockedArchive;await route.continue();};
    if(next){
      await page.waitForFunction(()=>!document.getElementById('review-event-next').disabled);
      await page.route('**/api/station/events/list',holdArchive);
    }
    try {
      const started=Date.now();
      const saved=page.waitForResponse(response=>response.url().endsWith('/api/station/events/update') && response.request().method()==='POST');
      await page.locator('#review-label-lightning').click();

      if(next){
        await page.waitForFunction(next=>{const image=document.getElementById('review-still');return image.getAttribute('src')?.includes('/clips/'+next.id+'/') && image.getAttribute('src')?.includes('/api/cameras/'+next.camera+'/') && image.complete && image.naturalWidth>0;},next,{timeout:3000});
        assert(!await page.locator('#review-video').isVisible(),'Next image must display while archive lookups remain blocked');
        assert(await page.locator('#review-background-saves').isVisible());
        assert(await page.locator('#logout').isDisabled());
        await page.screenshot({path:'dist/browser/background-review-saves.png',fullPage:true});
        console.log(`Next image visible ${Date.now()-started} ms after category click with the save request and archive lookups blocked.`);
      }else await page.waitForFunction(()=>document.getElementById('review-save-status').textContent.includes('Last matching event'));
      releaseSave();const saveResponse=await saved;assert(saveResponse.ok());await saveResponse.finished();
      await page.waitForFunction(()=>!MagiEvents.hasPending());
      releaseArchive();
      const labelled=await page.evaluate(async event=>(await api('/api/station/events/list',{q:event.id,camera_id:event.camera})).events.find(row=>row.id===event.id),event);
      assert(labelled.review.reviewed,'Category save must mark the event reviewed');
    } finally {
      releaseSave();releaseArchive();
      await page.unrouteAll({behavior:'wait'});
    }
    await page.locator('#review-close').click();
    // Settings links select a camera, then Overview returns to all feeds.
    await page.locator(`aside a[href="/?camera=${event.camera}#settings"]`).click();
    await page.waitForURL('**/?camera='+event.camera+'#settings');
    await page.locator('#settings').waitFor();
    await page.locator('.nav[data-tab=overview]').click();
    assert.equal(new URL(page.url()).hash,'#overview');
    await page.reload();
    await page.locator('#overview').waitFor();
    await page.locator(`aside a[href="/?camera=${event.camera}#settings"]`).click();
    await page.locator('#settings').waitFor();
    await page.locator('.star-panel > summary').click();
    await page.locator('#auto-star-refresh').click();
    await page.waitForFunction(()=>document.getElementById('auto-star-status').textContent.startsWith('Latest search:'));
    assert(await page.locator('#auto-star-apply').isDisabled(),'Automatic calibration requires a reviewed candidate');
    await page.locator('#star-refresh').click();
    await page.locator('#star-picker').waitFor();
    assert(await page.locator('#star-picker-grid button').count()>0);
    await page.locator('#star-picker-grid button').first().click();
    await page.locator('#star-picker').waitFor({state:'hidden'});
    assert.equal(await page.locator('#star-elevation').inputValue(),'322.8');


    await page.waitForFunction(camera=>document.getElementById('camera-select').value===camera,event.camera);
    assert.equal(await page.locator('#camera-select').inputValue(),event.camera);
    // Navigation keeps the hash, so a second click must explicitly reopen settings.
    for(let attempt=0;attempt<2;attempt++){
      await page.locator('.nav[data-tab=events]').click();
      await page.locator('#events').waitFor();
      await page.locator(`aside a[href="/?camera=${event.camera}#settings"]`).click();
      await page.locator('#settings').waitFor();
    }
    await page.locator('.nav[data-tab=overview]').click();
    await page.locator('#camera-grid img:not(.hidden)').nth(1).waitFor();
    await page.locator(`#camera-grid a[href="/?camera=${event.camera}#settings"]`).click();
    await page.locator('#settings').waitFor();
    await page.locator('.nav[data-tab=overview]').click();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'dist/browser/mobile.png',fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile overview must fit the viewport');
    const count=await page.locator('#camera-select option').count();
    await page.getByRole('button',{name:'Add a camera',exact:true}).click();
    await page.locator('#camera-add-dialog').waitFor();
    await page.locator('#camera-add-cancel').click();
    assert.equal(await page.locator('#camera-select option').count(),count);
    await page.getByRole('button',{name:'Add a camera',exact:true}).click();
    await page.locator('#camera-new-name').fill('Browser setup camera');
    await page.locator('#camera-add').click();
    await page.waitForURL(/\/\?camera=cam-[^#]+#settings$/);
    await page.locator('#settings').waitFor();
    assert.equal(await page.locator('#setup-initialize').isChecked(),false);
    await page.waitForFunction(expected=>document.querySelectorAll('#camera-select option').length===expected,count+1);
    const removedId=new URL(page.url()).searchParams.get('camera');
    await page.locator('.camera-management > summary').click();
    page.once('dialog',dialog=>dialog.dismiss());
    await page.locator('#camera-remove').click();
    assert.equal(await page.locator('#camera-select option').count(),count+1);
    page.once('dialog',dialog=>dialog.accept());
    await page.locator('#camera-remove').click();
    await page.waitForURL('**/?camera=primary#overview');
    await page.waitForFunction(expected=>document.querySelectorAll('#camera-select option').length===expected,count);
    assert(await page.locator(`#event-camera option[value="${removedId}"]`).textContent().then(text=>text.includes('(removed)')));
    await page.locator('#camera-grid img:not(.hidden)').nth(1).waitFor();
    assert.equal(await page.locator('#camera-grid img:visible').count(),2);
    await page.locator('.nav[data-tab=storage]').click();
    const archive=page.locator('#storage-cameras article').filter({hasText:'Browser setup camera (removed)'});
    await archive.waitFor();
    page.once('dialog',dialog=>dialog.dismiss());
    await archive.getByRole('button',{name:'Delete archived camera'}).click();
    await page.locator('#storage-purge-status').filter({hasText:'Cancelled'}).waitFor();
    assert(await archive.isVisible());
    page.once('dialog',dialog=>dialog.accept());
    await archive.getByRole('button',{name:'Delete archived camera'}).click();
    await page.locator('#storage-purge-status').filter({hasText:'Archived camera removed'}).waitFor();
    await page.locator(`#event-camera option[value="${removedId}"]`).waitFor({state:'detached'});
    await archive.waitFor({state:'detached'});
    assert.equal(await page.locator(`#event-camera option[value="${removedId}"]`).count(),0);
    assert.equal(await archive.count(),0);
    // Purge feedback must stay visible during a slow batch, and report a
    // partial failure without claiming that all files were deleted.
    let purgeScenario='progress',releasePurge;
    const blockedPurge=new Promise(resolve=>releasePurge=resolve);
    await page.route('**/api/station/storage/purge',async route=>{
      const body=route.request().postDataJSON();let data;
      if(body.action==='preview')data={token:'synthetic-preview',count:12,estimated_bytes:1024,retained:2,scope:'Synthetic cameras',mode:body.mode};
      else if(body.step===0)data={step:1,done:false,deleted:10,failed:0,retained:2};
      else {
        if(purgeScenario==='progress')await blockedPurge;
        if(purgeScenario==='failure'){await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Synthetic interrupted batch'})});return;}
        data={step:2,done:true,deleted:11,failed:1,retained:2};
      }
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    });
    try {
      page.once('dialog',dialog=>dialog.accept());await page.locator('#storage-purge-all').click();
      await page.locator('#storage-purge-status').filter({hasText:'10 of 12 checked'}).waitFor();
      assert(await page.locator('#storage-purge-all').isDisabled());
      assert(await page.locator('#storage-empty-trash').isDisabled());
      assert(await page.locator('#storage-purge-progress').isVisible());
      assert(await page.locator('#storage-purge-feedback').evaluate(node=>{const b=node.getBoundingClientRect();return b.top>=0 && b.top<innerHeight;}),'Purge feedback must be brought into view');
      await page.waitForFunction(()=>/[1-9]\d*s elapsed/.test(document.getElementById('storage-purge-status').textContent));
      await page.screenshot({path:'dist/browser/storage-purge-progress.png',fullPage:true});
      releasePurge();await page.locator('#storage-purge-status').filter({hasText:'Purge complete. 11 events deleted; 1 skipped or failed; 2 retained.'}).waitFor();
      await page.waitForFunction(()=>!purgeBusy);
      assert(!await page.locator('#storage-purge-all').isDisabled());
      purgeScenario='failure';page.once('dialog',dialog=>dialog.accept());await page.locator('#storage-purge-all').click();
      await page.locator('#storage-purge-status').filter({hasText:'Deletion stopped:'}).waitFor();
      assert((await page.locator('#storage-purge-status').textContent()).includes('10 deletions confirmed'));
      assert(!await page.locator('#storage-purge-all').isDisabled());
    } finally {releasePurge();await page.unrouteAll({behavior:'wait'});}
    // Real canvas polygon editing and persistence through the settings API.
    await page.setViewportSize({width:1600,height:1100});
    await page.locator('#camera-links a').first().click();
    await page.waitForFunction(()=>zoneFrameReady);
    await page.locator('#zone-clear').click();
    await page.locator('#zone-mode').selectOption('polygon');
    await page.locator('#zone-feather').fill('2');
    const canvas=page.locator('#zone-canvas');await canvas.scrollIntoViewIfNeeded();
    const bounds=await canvas.boundingBox();
    for(const [x,y] of [[.001,.55],[.35,.4],[.7,.65],[.999,.55],[.999,.999],[.001,.999]])await page.mouse.click(bounds.x+x*bounds.width,bounds.y+y*bounds.height);
    await page.locator('#zone-finish').click();
    assert.equal(await page.evaluate(()=>ignoreZones[0].points.length),6);
    assert.equal(await page.evaluate(()=>ignoreZones[0].points[0][0]),0);
    assert.equal(await page.evaluate(()=>ignoreZones[0].feather),.02);
    await canvas.scrollIntoViewIfNeeded();
    const rect=await canvas.boundingBox();
    await page.mouse.move(rect.x+.35*rect.width,rect.y+.4*rect.height);
    await page.mouse.down();await page.mouse.move(rect.x+.4*rect.width,rect.y+.3*rect.height,{steps:4});await page.mouse.up();
    assert(Math.abs(await page.evaluate(()=>ignoreZones[0].points[1][0])-.4)<.005);
    await page.screenshot({path:'dist/browser/polygon-mask.png',fullPage:true});
    await page.locator('#zone-clear').click();
    await page.locator('#zone-new').click();
    await canvas.scrollIntoViewIfNeeded();
    const shapeBounds=await canvas.boundingBox();
    const dragMask=async(x,y,ex,ey)=>{
      await page.mouse.move(shapeBounds.x+x*shapeBounds.width,shapeBounds.y+y*shapeBounds.height);
      await page.mouse.down();await page.mouse.move(shapeBounds.x+ex*shapeBounds.width,shapeBounds.y+ey*shapeBounds.height,{steps:5});await page.mouse.up();
    };
    await dragMask(.5,.5,.6,.6);
    assert(Math.abs(await page.evaluate(()=>ignoreZones[0].points[0][0])-.35)<.005,'Drag inside moves the shape');
    await dragMask(.35,.35,-.03,-.03);
    assert.equal(await page.evaluate(()=>ignoreZones[0].points[0][0]),0);
    assert.equal(await page.evaluate(()=>ignoreZones[0].points[0][1]),0);
    await page.locator('.zone-panel').screenshot({path:'dist/browser/easier-mask-editor.png'});
    // Starting-point click closes a custom outline without duplicating a corner.
    await page.locator('#zone-mode').selectOption('polygon');
    await canvas.scrollIntoViewIfNeeded();const closeBounds=await canvas.boundingBox();
    for(const [x,y] of [[.2,.2],[.7,.2],[.7,.7],[.2,.7],[.2,.2]])await page.mouse.click(closeBounds.x+x*closeBounds.width,closeBounds.y+y*closeBounds.height);
    assert.equal(await page.evaluate(()=>polygonDraft.length),0);
    assert.equal(await page.evaluate(()=>ignoreZones[1].points.length),4);
    const expected=await page.evaluate(()=>JSON.stringify(ignoreZones));
    await page.locator('#detection-stream').selectOption('mainstream');
    await page.locator('#host').fill('synthetic.invalid');
    // Synthetic station storage is below the production form's minimum size.
    await page.evaluate(()=>document.querySelector('#settings-form').onsubmit({preventDefault(){},submitter:document.querySelector('#settings-form button[type=submit]')}));
    await page.locator('#overview').waitFor({state:'visible'});
    const saved=await page.evaluate(()=>api('/api/settings'));
    assert.equal(saved.detection.stream,'mainstream','Detection stream survives settings save');
    assert.equal(JSON.stringify(saved.detection.ignore_zones),expected,'Polygon and feather survive settings save');
    assert.deepEqual(errors,[]);
    console.log('Chromium passed: simultaneous images, station event list, explicit camera playback, settings links, manual save, mobile layout.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
