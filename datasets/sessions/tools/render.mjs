import { chromium } from '/opt/browser/node_modules/playwright/index.mjs';
import { launchBrowser } from './browser.mjs';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
const [source,out]=process.argv.slice(2);mkdirSync(out,{recursive:true});
const browser=await launchBrowser(chromium,out);
const records=[];
for(const [name,width,height] of [['desktop',1280,900],['mobile',390,844]]){
 const page=await browser.newPage({viewport:{width,height},deviceScaleFactor:1});
 await page.route('**/*',r=>r.abort());
 await page.setContent(readFileSync(source,'utf8'),{waitUntil:'load'});
 await page.evaluate(()=>document.fonts.ready);
 await page.screenshot({path:out+'/'+name+'.png',fullPage:true});
 records.push({name,width,height,device_scale_factor:1,horizontal_overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1)});
 await page.close();
}
writeFileSync(out+'/render.json',JSON.stringify({browser:browser.version(),layouts:records}));
await browser.close();
