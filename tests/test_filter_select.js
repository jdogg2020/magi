// Focused browser check; no station, camera, or network service required.
const {chromium}=require('playwright');
const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}});
  const html=fs.readFileSync('src/magi/web/index.html','utf8');
  const form=html.match(/<form id="event-filters"[\s\S]*?<\/form>/)[0];
  await page.setContent('<style>'+fs.readFileSync('src/magi/web/style.css','utf8')+'</style>'+form);
  await page.addScriptTag({path:'src/magi/web/filter-select.js'});
  await page.evaluate(()=>document.dispatchEvent(new Event('DOMContentLoaded')));
  const label=page.getByRole('button',{name:'Label',exact:true});
  await page.evaluate(()=>{
   const select=document.getElementById('event-label-filter');
   for(const text of ['aircraft','weather','night <test>'])select.add(new Option(text,text));
  });
  await label.click();
  const search=page.getByRole('combobox',{name:'Search label',exact:true});
  await search.fill('wea');await search.press('Enter');
  assert.equal(await page.locator('#event-label-filter').inputValue(),'weather');
  assert.match(await label.innerText(),/weather/);
  await label.click();await search.fill('not a label');await search.press('Enter');
  assert.equal(await page.locator('#event-label-filter').inputValue(),'weather','Typing cannot create a filter value');
  assert(await page.locator('.filter-select-panel:not([hidden])').getByText('No matching choices').isVisible());
  await search.press('Escape');assert.equal(await label.getAttribute('aria-expanded'),'false');
  await label.press('ArrowDown');await search.fill('air');await search.press('ArrowDown');await search.press('Enter');
  assert.equal(await page.locator('#event-label-filter').inputValue(),'aircraft');
  await page.evaluate(()=>{document.getElementById('event-label-filter').value='';MagiFilterSelect.sync();});
  assert.match(await label.innerText(),/Any label/);
  await label.click();await search.fill('<test>');await page.getByRole('option',{name:'night <test>',exact:true}).click();
  assert.equal(await page.locator('#event-label-filter').inputValue(),'night <test>');
  await page.getByRole('button',{name:'Reference result',exact:true}).click();
  await page.getByRole('combobox',{name:'Search reference result'}).fill('satellite');
  assert.equal(await page.getByRole('option').count(),2);
  await page.getByRole('option',{name:'Likely satellite',exact:true}).click();
  assert.equal(await page.locator('#event-reference').inputValue(),'likely_satellite');
  assert.equal(await page.locator('#event-reference').isVisible(),false);
  await label.click();await page.getByRole('button',{name:'Reset',exact:true}).click();
  assert.equal(await label.getAttribute('aria-expanded'),'false');
  console.log('Searchable filters: listed choices, search, keyboard, Escape, reset, dynamic labels and mobile layout passed.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
