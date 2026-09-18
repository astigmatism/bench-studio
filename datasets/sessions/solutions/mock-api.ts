const store={issues:[{id:1,title:'Fix login',status:'open',assignee:'Maya',column_id:1},{id:2,title:'Refresh docs',status:'closed',assignee:'Kai',column_id:2},{id:3,title:'Add export',status:'open',assignee:'Maya',column_id:1}],items:[{id:1,name:'Adapters',quantity:2,reorder_threshold:5},{id:2,name:'Cables',quantity:38,reorder_threshold:5},{id:3,name:'Batteries',quantity:12,reorder_threshold:5}],settings:{default_assignee:'Unassigned'},columns:[{id:1,name:'Open'},{id:2,name:'Closed'}],transitions:[] as {issue_id:number;from_column:number;to_column:number;created_at:string}[],orders:[] as {id:number;supplier:string;status:string;lines:{item_id:number;quantity:number}[]}[],receipts:[] as {order_id:number;created_at:string}[]};
export async function api(path:string,data?:unknown,method='POST'):Promise<any>{
 const value=data as any;const parts=path.split('/');let result:any;
 if(path==='/issues'&&data===undefined)result=store.issues;
 else if(path==='/items'&&data===undefined)result=store.items;
 else if(path==='/issues'){const row={id:store.issues.length+1,title:value.title,status:'open',assignee:value.assignee??store.settings.default_assignee,column_id:1};store.issues.push(row);result=row;}
 else if(path==='/items'){const row={id:store.items.length+1,name:value.name,quantity:value.quantity,reorder_threshold:5};store.items.push(row);result=row;}
 else if(path==='/settings'){if(data!==undefined){if(!value.default_assignee.trim())throw Error('Assignee required');store.settings.default_assignee=value.default_assignee.trim()}result=store.settings;}
 else if(parts[1]==='items'&&parts[3]==='threshold'){const item=store.items.find(i=>i.id===Number(parts[2]))!;if(!Number.isInteger(value.reorder_threshold)||value.reorder_threshold<0)throw Error('Invalid threshold');item.reorder_threshold=value.reorder_threshold;result=item;}
 else if(path==='/columns'){if(data!==undefined){if(!value.name.trim()||store.columns.some(c=>c.name===value.name.trim()))throw Error('Invalid or duplicate column');store.columns.push({id:store.columns.length+1,name:value.name.trim()})}result=store.columns;}
 else if(parts[1]==='issues'&&parts[3]==='move'){const issue=store.issues.find(i=>i.id===Number(parts[2]))!;if(issue.column_id!==value.column_id){store.transitions.push({issue_id:issue.id,from_column:issue.column_id,to_column:value.column_id,created_at:new Date().toISOString()});issue.column_id=value.column_id;}result=issue;}
 else if(parts[1]==='issues'&&parts[3]==='history')result=store.transitions.filter(t=>t.issue_id===Number(parts[2]));
 else if(path==='/orders'){if(data!==undefined){if(!value.supplier.trim()||!value.lines.length||value.lines.some((l:any)=>!Number.isInteger(l.quantity)||l.quantity<=0))throw Error('Invalid order');store.orders.push({id:store.orders.length+1,supplier:value.supplier.trim(),status:'open',lines:value.lines})}result=store.orders;}
 else if(parts[1]==='orders'&&parts[3]==='receive'){const order=store.orders.find(o=>o.id===Number(parts[2]))!;if(order.status!=='open')throw Error('Already received');for(const l of order.lines)store.items.find(i=>i.id===l.item_id)!.quantity+=l.quantity;order.status='received';store.receipts.push({order_id:order.id,created_at:new Date().toISOString()});result=order;}
 else if(parts[1]==='orders'&&parts[3]==='history')result=store.receipts.filter(r=>r.order_id===Number(parts[2]));
 else throw Error('Unknown action '+method+' '+path);
 return JSON.parse(JSON.stringify(result));
}
