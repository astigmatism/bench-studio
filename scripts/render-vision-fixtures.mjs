// Regenerate deliberately designed, code-native screenshot fixtures. Commit the PNGs.
import {chromium} from '../frontend/node_modules/playwright/index.mjs';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../datasets/sessions/',import.meta.url));
const manifest=JSON.parse(readFileSync(root+'manifest.json','utf8'));
const bodies={
 'text-1':'<h1>Invoice</h1><p>Northwind supplies</p><table><tr><td>Subtotal</td><td>$1,135.00</td></tr><tr><td>Tax</td><td>$113.50</td></tr><tr><th>Total</th><th>$1,248.50</th></tr></table>',
 'text-2':'<p class="eyebrow">ISSUE TRACKER</p><h1>OPS-407</h1><h2>Repair the shipment export</h2><p>Assigned to Maya · Open</p>',
 'text-3':'<h1>Cables</h1><p>Warehouse A / Shelf 12</p><p>Available units</p><strong class="number">38</strong><p>Reserved: 7</p>',
 'text-4':'<h1>Deployment</h1><p>Production / Healthy</p><p>Deployment date</p><h2>2026-09-18</h2><p>Release 4.2</p>',
 'state-1':'<h1>Project</h1><nav><button>Overview</button><button class="selected">Activity</button><button>Settings</button></nav><h2>Recent activity</h2><p>Maya moved an issue to Review.</p>',
 'state-2':'<h1>Preferences</h1><label>Display name<input value="Maya"/></label><p>No unsaved changes</p><button disabled>Save</button>',
 'state-3':'<h1>Notifications</h1><div style="display:flex;align-items:center;gap:20px"><span>Notifications</span><span style="display:inline-block;background:#206750;border-radius:30px;width:64px;height:34px;padding:4px;text-align:right"><span style="display:inline-block;border-radius:50%;width:26px;height:26px;background:white"></span></span><b>On</b></div>',
 'state-4':'<h1>Issues</h1><p>Status</p><nav><button>All</button><button>Open</button><button class="selected">Closed</button></nav><p>Refresh docs · Closed</p>',
 'layout-1':'<h1>Project settings</h1><div style="position:relative;height:140px"><label>Project name<input value="Northwind" style="width:360px"/></label><button style="position:absolute;left:180px;top:36px;background:#873657">Delete project</button></div><p>Two controls occupy the same space.</p>',
 'layout-2':'<div style="height:38px;overflow:hidden"><h1 style="margin:20px 0 0">Inventory overview</h1></div><p style="margin-top:50px">Stock across all locations</p><button>View inventory</button>',
 'layout-3':'<h1>Inventory</h1><div style="width:100%;overflow:auto;border:2px solid #bd5663;padding:12px"><table style="width:1300px"><tr><th>Item</th><th>Warehouse</th><th>Supplier</th><th>Quantity</th><th>Threshold</th></tr><tr><td>Adapters</td><td>West</td><td>Northwind</td><td>2</td><td>5</td></tr></table></div><p>Scroll → to see remaining columns</p>',
 'layout-4':'<h1>Inventory</h1><div style="display:flex;justify-content:space-between;align-items:center"><h2>Stock overview</h2><button>Add item</button></div><table><tr><th>Item</th><th>Quantity</th><th>Status</th></tr><tr><td>Adapters</td><td>2</td><td>Low stock</td></tr><tr><td>Cables</td><td>38</td><td>In stock</td></tr></table>'
};
const browser=await chromium.launch({headless:true});mkdirSync(root+'screenshots',{recursive:true});
for(const item of manifest.vision_checks){
 const page=await browser.newPage({viewport:{width:960,height:640},deviceScaleFactor:1});
 await page.setContent('<html><head><meta charset="utf-8"><style>*{box-sizing:border-box}body{margin:0;background:#edf2f6;color:#183140;font:20px Arial,sans-serif}main{margin:48px;padding:36px;background:white;border:1px solid #ccd9e0;border-radius:16px;min-height:490px}h1{font-size:38px;margin:0 0 28px}h2{font-size:26px}.eyebrow{font-size:14px;letter-spacing:2px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:16px;border-bottom:1px solid #ccd9e0}button{padding:14px 24px;font:inherit;border:1px solid #94a8b3;border-radius:8px;background:#fff;color:#183140}button.selected{background:#205770;color:white;border-bottom:5px solid #102e40}button:disabled{background:#d9e0e5;color:#8a98a3;border-color:#d9e0e5}nav{display:flex;gap:12px;margin-bottom:40px}label{display:flex;flex-direction:column;gap:12px}input{font:inherit;padding:14px;border:1px solid #94a8b3;border-radius:8px}.number{font-size:80px}</style></head><body><main>'+bodies[item.id]+'</main></body></html>');
 await page.evaluate(()=>document.fonts.ready);const png=await page.screenshot({path:root+item.image});
 item.image_sha256=createHash('sha256').update(png).digest('hex');item.width=960;item.height=640;
 await page.close();
}
manifest.screenshot_renderer={browser:browser.version(),viewport:{width:960,height:640},device_scale_factor:1};
writeFileSync(root+'manifest.json',JSON.stringify(manifest,null,2)+'\n');await browser.close();
