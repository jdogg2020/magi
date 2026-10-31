// Real Chromium rendering and camera routing against disposable synthetic feeds.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const base = process.argv[2];
(async()=>{
  const browser = await chromium.launch({headless:true,...(process.env.MAGI_CHROMIUM_EXECUTABLE ? {executablePath:process.env.MAGI_CHROMIUM_EXECUTABLE} : {})});
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
    fs.mkdirSync('dist/browser',{recursive:true});
    await page.screenshot({path:'dist/browser/overview.png',fullPage:true});
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
    const rows=await page.locator('#all-events .event-row').evaluateAll(rows=>rows.map(row=>({id:row.dataset.eventId,camera:row.dataset.cameraId,hasImage:!!row.querySelector('img'),hasVideo:row.textContent.includes('complete')})));
    assert.equal(new Set(rows.map(row=>row.camera)).size,2,'Shared list must show both cameras');
    await page.screenshot({path:'dist/browser/events.png',fullPage:true});
    const event=rows.find(row=>row.camera!=='primary' && row.hasImage && row.hasVideo) || rows.find(row=>row.camera!=='primary' && row.hasImage);
    const eventRow=page.locator(`#all-events tr[data-camera-id="${event.camera}"][data-event-id="${event.id}"]`);
    await eventRow.getByRole('button',{name:'Review',exact:true}).click();
    await page.locator('#review').waitFor();
    const still=page.locator('#review-still');
    const src=await still.getAttribute('src');
    assert(src.includes(`/api/cameras/${event.camera}/clips/${event.id}/`));
    assert(await still.isVisible());
    assert(!await page.locator('#review-video').isVisible());
    await page.locator('#review-play').click();
    assert((await page.locator('#review-video').getAttribute('src')).includes(`/api/cameras/${event.camera}/clips/${event.id}/`));
    await page.locator('#review-close').click();
    // Settings links select a camera, then Overview returns to all feeds.
    await page.locator(`aside a[href="/?camera=${event.camera}#settings"]`).click();
    await page.waitForURL('**/?camera='+event.camera+'#settings');
    await page.locator('#settings').waitFor();
    await page.waitForFunction(camera=>document.getElementById('camera-select').value===camera,event.camera);
    assert.equal(await page.locator('#camera-select').inputValue(),event.camera);
    await page.locator('.nav[data-tab=overview]').click();
    await page.locator('#camera-grid img:not(.hidden)').nth(1).waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'dist/browser/mobile.png',fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile overview must fit the viewport');
    assert.deepEqual(errors,[]);
    console.log('Chromium passed: simultaneous images, station event list, explicit camera playback, settings links, manual save, mobile layout.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
