import { chromium } from '/opt/browser/node_modules/playwright/index.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
const [task,prototype,output]=process.argv.slice(2);
const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1280,height:900},deviceScaleFactor:1});
page.setDefaultTimeout(10000);
await page.route('**/*',r=>r.request().url().startsWith('http://127.0.0.1:8111/')&&!prototype?r.continue():r.abort());
try {
if(prototype) await page.setContent(readFileSync(prototype,'utf8'),{waitUntil:'load'});
else await page.goto('http://127.0.0.1:8111');
await page.getByRole('heading',{level:1}).waitFor();
const assert=(value,msg)=>{if(!value)throw new Error(msg)};
const text=()=>page.locator('body').innerText();
if(task==='issues-small'){
 // Test the behavior without prescribing a dropdown for the visual design.
 const label=/^Status(?: filter)?$/i;
 const select=page.getByRole('combobox',{name:label});
 const filterOpen=async()=>{
 if(await select.count()===1) await select.selectOption({label:'Open'});
 else {
  const group=page.getByRole('group',{name:label}).or(page.getByRole('radiogroup',{name:label})).or(page.getByRole('tablist',{name:label}));
  const radio=group.getByRole('radio',{name:'Open',exact:true});
  if(await radio.count()===1) await radio.check();
  else await group.getByRole('button',{name:'Open',exact:true}).or(group.getByRole('tab',{name:'Open',exact:true})).click();
 }
 };
 await filterOpen();
 await page.getByLabel('Search',{exact:true}).fill('LOGIN');
 await page.getByText('Fix login',{exact:true}).waitFor();
 assert(/\b1\b/.test(await text()),'visible count for one issue');
 assert(!(await text()).includes('Refresh docs'),'status filter failed');
 assert(!(await text()).includes('Add export'),'combined search failed');
 // Repeated reset controls are valid (toolbar plus empty state). Exercise
 // every visible one rather than imposing a unique accessible name.
 const empty=async()=>{
  await filterOpen();
  await page.getByLabel('Search',{exact:true}).fill('zzzz-does-not-exist');
  await page.getByText('No matching issues',{exact:true}).waitFor();
  assert(/\b0\b/.test(await text()),'visible count for zero issues');
 };
 await empty();
 const resets=page.getByRole('button',{name:'Reset filters',exact:true});
 const resetCount=await resets.count();
 assert(resetCount>0,'empty state needs a Reset filters button');
 for(let i=0;i<resetCount;i++){
  if(i)await empty();
  await resets.nth(i).click();
  await page.getByText('Refresh docs',{exact:true}).waitFor();
  await page.getByText('Fix login',{exact:true}).waitFor();
  await page.getByText('Add export',{exact:true}).waitFor();
  assert(await page.getByLabel('Search',{exact:true}).inputValue()==='','reset clears search');
 }
}else if(task==='inventory-small'){
 await page.getByLabel('Sort by').selectOption('Stock ascending');
 assert((await page.locator('tbody tr').first().innerText()).includes('Adapters'),'ascending');
 await page.getByLabel('Sort by').selectOption('Stock descending');
 assert((await page.locator('tbody tr').first().innerText()).includes('Cables'),'descending');
 await page.getByLabel('Sort by').selectOption('Name');
 assert((await page.locator('tbody tr').first().innerText()).includes('Adapters'),'name sort');
 assert((await text()).includes('Low stock')&&(await text()).includes('In stock'),'stock badges');
}else if(task==='issues-medium'){
 await page.getByLabel('Default assignee',{exact:true}).fill('Morgan');
 await page.getByRole('button',{name:'Save settings',exact:true}).click();
 await page.getByRole('status').filter({hasText:/sav/i}).waitFor();
 await page.getByLabel('Issue title',{exact:true}).fill('UI inheritance');
 await page.getByRole('button',{name:'Add issue',exact:true}).click();
 const card=page.locator('article').filter({hasText:'UI inheritance'});await card.waitFor();
 await page.waitForFunction(()=>[...document.querySelectorAll('article')].some(e=>e.textContent.includes('UI inheritance')&&e.textContent.includes('Morgan')));
}else if(task==='inventory-medium'){
 const card=page.locator('article').filter({has:page.getByRole('heading',{name:'Adapters',exact:true})});
 await card.getByLabel('Reorder threshold',{exact:true}).fill('3');
 await card.getByRole('button',{name:'Save threshold',exact:true}).click();
 await card.getByText('Low stock',{exact:true}).waitFor();
 await card.getByLabel('Reorder threshold',{exact:true}).fill('1');
 await card.getByRole('button',{name:'Save threshold',exact:true}).click();
 await card.getByText('In stock',{exact:true}).waitFor();
}else if(task==='issues-large'){
 if(prototype)await page.getByRole('button',{name:'Board',exact:true}).click();
 await page.getByLabel('Column name',{exact:true}).fill('QA');
 await page.getByRole('button',{name:'Add column',exact:true}).click();
 await page.getByRole('heading',{name:'QA',exact:true}).waitFor();
 const card=page.locator('article').filter({hasText:'Fix login'});
 await card.getByLabel('Move to').selectOption({label:'QA'});
 await card.getByRole('button',{name:'Move',exact:true}).click();
 await page.getByRole('region',{name:'QA',exact:true}).getByText('Fix login',{exact:true}).waitFor();
 await card.getByRole('button',{name:'Show history',exact:true}).click();
 if(prototype)await page.getByRole('button',{name:'History',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[aria-label="Transition history"]')?.textContent.match(/\d{4}-\d{2}-\d{2}/));
}else if(task==='inventory-large'){
 if(prototype)await page.getByRole('button',{name:'Orders',exact:true}).click();
 await page.getByLabel('Supplier',{exact:true}).fill('Browser supplier');
 await page.getByLabel('Order quantity for Adapters',{exact:true}).fill('4');
 await page.getByRole('button',{name:'Create order',exact:true}).click();
 const card=page.locator('article').filter({hasText:'Browser supplier'});await card.waitFor();
 await card.getByRole('button',{name:'Receive order',exact:true}).click();
 await card.getByText('received',{exact:true}).waitFor();
 assert(await card.getByRole('button',{name:'Receive order',exact:true}).isDisabled(),'duplicate receipt control');
 if(prototype){
  await page.getByRole('button',{name:'Inventory',exact:true}).click();
  assert((await page.locator('tbody tr').filter({hasText:'Adapters'}).innerText()).includes('6'),'receiving updates inventory screen');
  await page.getByRole('button',{name:'Orders',exact:true}).click();
 }
 await card.getByRole('button',{name:'Show history',exact:true}).click();
 if(prototype)await page.getByRole('button',{name:'History',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[aria-label="Receipt history"]')?.textContent.match(/\d{4}-\d{2}-\d{2}/));
}
const layouts=[];
for(const [name,width,height] of [['desktop',1280,900],['mobile',390,844]]){
 await page.setViewportSize({width,height});await page.evaluate(()=>document.fonts.ready);
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
 assert(!overflow,name+' horizontal overflow');
 const unlabeled=await page.locator('input:not([type=hidden]),select,button').evaluateAll(nodes=>nodes.filter(e=>!e.labels?.length&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')&&!e.textContent.trim()).length);
 assert(unlabeled===0,name+' unlabeled controls');
 if(output)await page.screenshot({path:output+'/'+name+'.png',fullPage:true});
 layouts.push({name,width,height,horizontal_overflow:overflow,unlabeled_controls:unlabeled});
}
if(output)writeFileSync(output+'/browser.json',JSON.stringify({functional_interactions:{passed:true,task},objective_layout:{passed:true,layouts},browser:browser.version(),rendering:{deviceScaleFactor:1,headless:true,network:"disabled"}}));
console.log('Browser acceptance passed:',task);
} catch (error) {
 if(output){await page.screenshot({path:output+'/failure.png',fullPage:true});writeFileSync(output+'/failure.html',await page.content());writeFileSync(output+'/browser.json',JSON.stringify({passed:false,error:String(error),browser:browser.version()}));}
 throw error;
} finally {await browser.close();}
