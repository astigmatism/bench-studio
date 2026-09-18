import { useEffect, useState } from 'react';
import settings from '../../app.json';
export type Issue = {id:number; title:string; status:string; assignee:string; column_id?:number};
export type Item = {id:number; name:string; quantity:number; reorder_threshold?:number};
export async function api(path:string, data?:unknown, method='POST') {
  const response = await fetch('/api'+path, data === undefined ? {} : {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  if (!response.ok) throw new Error('Request failed ('+response.status+')');
  return response.json();
}
export default function App() {
  const [issues,setIssues]=useState<Issue[]>([]), [items,setItems]=useState<Item[]>([]), [name,setName]=useState(''), [error,setError]=useState('');
  const refresh=()=>Promise.all([api('/issues').then(setIssues),api('/items').then(setItems)]).catch(e=>setError(String(e)));
  useEffect(()=>{void refresh()},[]);
  return <main><header><p className="eyebrow">WORKSPACE / {settings.kind.toUpperCase()}</p><h1>{settings.title}</h1><p>{settings.description}</p></header>
    {error && <p role="alert">{error}</p>}
    <form onSubmit={e=>{e.preventDefault();void api(settings.kind==='issues'?'/issues':'/items',settings.kind==='issues'?{title:name}:{name,quantity:0}).then(()=>{setName('');void refresh()}).catch(e=>setError(String(e)))}}>
      <label>{settings.kind==='issues'?'Issue title':'Item name'}<input required value={name} onChange={e=>setName(e.target.value)}/></label><button>Add {settings.kind==='issues'?'issue':'item'}</button>
    </form>
    {settings.kind==='issues'?<section aria-label="Issues">{issues.map(i=><article key={i.id}><h2>{i.title}</h2><p>{i.status} · {i.assignee}</p></article>)}</section>:
    <table><thead><tr><th>Item</th><th>Quantity</th></tr></thead><tbody>{items.map(i=><tr key={i.id}><td>{i.name}</td><td>{i.quantity}</td></tr>)}</tbody></table>}
  </main>
}
