// Exercise the real feeder HTML and JavaScript with delayed synthetic replies.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
 const executable=process.env.MAGI_CHROMIUM_EXECUTABLE || ['/usr/bin/google-chrome','/usr/bin/google-chrome-stable'].find(path=>fs.existsSync(path));
 const browser=await chromium.launch({headless:true,...(executable?{executablePath:executable}:{})});
 try{
  const page=await browser.newPage();
  await page.setContent(fs.readFileSync('src/magi/web/index.html','utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,''));
  await page.evaluate(()=>{
   document.body.replaceChildren(document.getElementById('opensky-feeding-setup'));
   window.$=id=>id==='settings'?{classList:{contains:()=>false}}:document.getElementById(id);
   window.csrf='fixture';window.setInterval=fn=>{window.poll=fn;};window.calls=[];
   window.reply={helper_available:true,busy:false,active:false,enabled:false,settings:{},gps:{latitude:41.1,longitude:-96.1,altitude_msl_m:350.4}};
   window.api=async(path,body)=>{calls.push(body || null);if(window.delay){window.delay=false;return new Promise(resolve=>window.resolvePoll=resolve);}if(window.fail && body)throw new Error('Local ADS-B output is unavailable.');return reply;};
  });
  const source=fs.readFileSync('src/magi/web/app.js','utf8');
  await page.addScriptTag({content:source.slice(source.indexOf('// Station-wide feeding is intentionally separate'))});
  await page.locator('#opensky-feeding-setup > summary').click();
  await page.waitForFunction(()=>document.getElementById('feeding-altitude').value==='350.4');
  await page.locator('#feeding-username').fill('station');
  await page.locator('#feeding-username').focus();
  await page.locator('#feeding-username').evaluate(el=>el.setSelectionRange(3,3));
  await page.evaluate(()=>{window.delay=true;poll();});
  assert.equal(await page.locator('#feeding-username').isEnabled(),true);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'feeding-username');
  assert.equal(await page.locator('#feeding-username').evaluate(el=>el.selectionStart),3);
  await page.keyboard.type('-typed-');
  await page.evaluate(()=>resolvePoll({...reply,gps:{latitude:1,longitude:2,altitude_msl_m:3}}));
  await page.waitForFunction(()=>!document.getElementById('feeding-refresh').disabled);
  assert.equal(await page.locator('#feeding-username').inputValue(),'sta-typed-tion');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'feeding-username');
  assert.equal(await page.locator('#feeding-latitude').inputValue(),'41.1');
  await page.locator('#feeding-consent').check();
  await page.evaluate(()=>window.fail=true);
  await page.locator('#feeding-enable').click();
  await page.waitForFunction(()=>document.getElementById('feeding-status').textContent.includes('Local ADS-B'));
  await page.evaluate(()=>poll());
  await page.waitForFunction(()=>!document.getElementById('feeding-refresh').disabled);
  assert.equal(await page.locator('#feeding-status').textContent(),'Local ADS-B output is unavailable.');
  assert.equal(await page.locator('#feeding-consent').isChecked(),true);
  assert.equal(await page.locator('#feeding-username').inputValue(),'sta-typed-tion');
  fs.mkdirSync('dist/browser',{recursive:true});await page.screenshot({path:'dist/browser/feeder-form.png',fullPage:true});
  console.log('Feeding Chromium: typing focus, caret, GPS edits and persistent failure passed.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
