"""Trusted reference overlays used only by preparation and verification diagnostics."""
from pathlib import Path

SETTINGS = '''
with connection() as db:
    db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("INSERT OR IGNORE INTO settings VALUES('default_assignee','Unassigned')")
class Settings(BaseModel):
    default_assignee: str = Field(min_length=1,max_length=80)
@app.get('/api/settings')
def settings():
    with connection() as db:
        return {'default_assignee':db.execute("SELECT value FROM settings WHERE key='default_assignee'").fetchone()[0]}
@app.put('/api/settings')
def save_settings(value:Settings):
    text=value.default_assignee.strip()
    if not text: raise HTTPException(422,'Assignee is required')
    with connection() as db:
        db.execute("UPDATE settings SET value=? WHERE key='default_assignee'",(text,))
    return settings()
'''
THRESHOLDS = '''
with connection() as db:
    if 'reorder_threshold' not in [r['name'] for r in db.execute('PRAGMA table_info(items)')]:
        db.execute('ALTER TABLE items ADD COLUMN reorder_threshold INTEGER NOT NULL DEFAULT 5')
class Threshold(BaseModel):
    reorder_threshold:int = Field(ge=0,strict=True)
@app.put('/api/items/{item_id}/threshold')
def threshold(item_id:int,value:Threshold):
    with connection() as db:
        if not db.execute('SELECT 1 FROM items WHERE id=?',(item_id,)).fetchone(): raise HTTPException(404,'Item missing')
        db.execute('UPDATE items SET reorder_threshold=? WHERE id=?',(value.reorder_threshold,item_id))
        return dict(db.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone())
'''
WORKFLOW = '''
with connection() as db:
    db.execute('CREATE TABLE IF NOT EXISTS columns(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL)')
    db.executemany('INSERT OR IGNORE INTO columns(id,name) VALUES(?,?)',[(1,'Open'),(2,'Closed')])
    if 'column_id' not in [r['name'] for r in db.execute('PRAGMA table_info(issues)')]:
        db.execute('ALTER TABLE issues ADD COLUMN column_id INTEGER NOT NULL DEFAULT 1')
        db.execute("UPDATE issues SET column_id=2 WHERE status='closed'")
    db.execute('CREATE TABLE IF NOT EXISTS transitions(id INTEGER PRIMARY KEY,issue_id INTEGER NOT NULL,from_column INTEGER NOT NULL,to_column INTEGER NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
class Column(BaseModel):
    name:str=Field(min_length=1,max_length=60)
class Move(BaseModel):
    column_id:int=Field(strict=True)
@app.get('/api/columns')
def columns():
    with connection() as db: return [dict(r) for r in db.execute('SELECT * FROM columns ORDER BY id')]
@app.post('/api/columns',status_code=201)
def add_column(value:Column):
    name=value.name.strip()
    if not name: raise HTTPException(422,'Column name required')
    with connection() as db:
        if db.execute('SELECT 1 FROM columns WHERE name=?',(name,)).fetchone(): raise HTTPException(409,'Duplicate column')
        cursor=db.execute('INSERT INTO columns(name) VALUES(?)',(name,))
        return {'id':cursor.lastrowid,'name':name}
@app.put('/api/issues/{issue_id}/move')
def move(issue_id:int,value:Move):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        issue=db.execute('SELECT * FROM issues WHERE id=?',(issue_id,)).fetchone()
        column=db.execute('SELECT * FROM columns WHERE id=?',(value.column_id,)).fetchone()
        if not issue or not column: raise HTTPException(404,'Issue or column missing')
        if issue['column_id']!=value.column_id:
            db.execute('UPDATE issues SET column_id=?,status=? WHERE id=?',(value.column_id,column['name'].lower(),issue_id))
            db.execute('INSERT INTO transitions(issue_id,from_column,to_column) VALUES(?,?,?)',(issue_id,issue['column_id'],value.column_id))
        return dict(db.execute('SELECT * FROM issues WHERE id=?',(issue_id,)).fetchone())
@app.get('/api/issues/{issue_id}/history')
def history(issue_id:int):
    with connection() as db:
        if not db.execute('SELECT 1 FROM issues WHERE id=?',(issue_id,)).fetchone(): raise HTTPException(404,'Issue missing')
        return [dict(r) for r in db.execute('SELECT * FROM transitions WHERE issue_id=? ORDER BY id',(issue_id,))]
'''
ORDERS = '''
with connection() as db:
    db.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,supplier TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open')")
    db.execute('CREATE TABLE IF NOT EXISTS order_lines(order_id INTEGER REFERENCES orders(id),item_id INTEGER REFERENCES items(id),quantity INTEGER NOT NULL,PRIMARY KEY(order_id,item_id))')
    db.execute('CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL UNIQUE,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
class Line(BaseModel):
    item_id:int=Field(strict=True)
    quantity:int=Field(gt=0,strict=True)
class Order(BaseModel):
    supplier:str=Field(min_length=1,max_length=80)
    lines:list[Line]=Field(min_length=1)
@app.get('/api/orders')
def orders():
    with connection() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY id')]
        for row in rows: row['lines']=[dict(r) for r in db.execute('SELECT item_id,quantity FROM order_lines WHERE order_id=?',(row['id'],))]
        return rows
@app.post('/api/orders',status_code=201)
def create_order(value:Order):
    supplier=value.supplier.strip()
    if not supplier or len({l.item_id for l in value.lines})!=len(value.lines): raise HTTPException(422,'Invalid supplier or duplicate items')
    with connection() as db:
        for line in value.lines:
            if not db.execute('SELECT 1 FROM items WHERE id=?',(line.item_id,)).fetchone(): raise HTTPException(404,'Item missing')
        cursor=db.execute('INSERT INTO orders(supplier) VALUES(?)',(supplier,))
        oid=cursor.lastrowid
        db.executemany('INSERT INTO order_lines VALUES(?,?,?)',[(oid,l.item_id,l.quantity) for l in value.lines])
        return {'id':oid,'supplier':supplier,'status':'open'}
@app.post('/api/orders/{order_id}/receive')
def receive(order_id:int):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        order=db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if not order: raise HTTPException(404,'Order missing')
        if order['status']!='open': raise HTTPException(409,'Already received')
        for line in db.execute('SELECT * FROM order_lines WHERE order_id=?',(order_id,)):
            db.execute('UPDATE items SET quantity=quantity+? WHERE id=?',(line['quantity'],line['item_id']))
        db.execute("UPDATE orders SET status='received' WHERE id=?",(order_id,))
        db.execute('INSERT INTO receipts(order_id) VALUES(?)',(order_id,))
        return {'id':order_id,'status':'received'}
@app.get('/api/orders/{order_id}/history')
def order_history(order_id:int):
    with connection() as db:
        if not db.execute('SELECT 1 FROM orders WHERE id=?',(order_id,)).fetchone(): raise HTTPException(404,'Order missing')
        return [dict(r) for r in db.execute('SELECT * FROM receipts WHERE order_id=?',(order_id,))]
'''

FRONT = {
'issues-small': (
"const [status,setStatus]=useState('All'),[search,setSearch]=useState(''); const visible=issues.filter(i=>(status==='All'||i.status===status.toLowerCase())&&i.title.toLowerCase().includes(search.toLowerCase()));",
'''<div className="controls"><label>Status filter<select value={status} onChange={e=>setStatus(e.target.value)}>{['All','Open','Closed'].map(s=><option key={s}>{s}</option>)}</select></label><label>Search<input value={search} onChange={e=>setSearch(e.target.value)}/></label></div><p>{visible.length} issues</p>{visible.length===0&&<section><p>No matching issues</p><button onClick={()=>{setStatus('All');setSearch('')}}>Reset filters</button></section>}{visible.map(i=><article key={i.id}><h2>{i.title}</h2><p>{i.status} · {i.assignee}</p></article>)}'''),
'inventory-small': (
"const [sort,setSort]=useState('Name');const visible=[...items].sort((a,b)=>sort==='Name'?a.name.localeCompare(b.name):sort==='Stock ascending'?a.quantity-b.quantity:b.quantity-a.quantity);",
'''<label>Sort by<select value={sort} onChange={e=>setSort(e.target.value)}>{['Name','Stock ascending','Stock descending'].map(s=><option key={s}>{s}</option>)}</select></label><table><thead><tr><th>Item</th><th>Quantity</th><th>Status</th></tr></thead><tbody>{visible.map(i=><tr key={i.id}><td>{i.name}</td><td>{i.quantity}</td><td><span className="badge">{i.quantity<5?'Low stock':'In stock'}</span></td></tr>)}</tbody></table>'''),
'issues-medium': (
"const [assignee,setAssignee]=useState('');useEffect(()=>{void api('/settings').then(s=>setAssignee(s.default_assignee))},[]);",
'''<section className="panel"><label>Default assignee<input maxLength={80} value={assignee} onChange={e=>setAssignee(e.target.value)}/></label><button onClick={()=>{if(!assignee.trim()){setError('Assignee is required');return}void api('/settings',{default_assignee:assignee},'PUT').then(()=>setMessage('Settings saved')).catch(e=>setError(String(e)))}}>Save settings</button></section>{issues.map(i=><article key={i.id}><h2>{i.title}</h2><p>{i.status} · {i.assignee}</p></article>)}'''),
'inventory-medium': (
"const [thresholds,setThresholds]=useState<Record<number,string>>({});",
'''{items.map(i=><article key={i.id}><h2>{i.name}</h2><p>{i.quantity} units · <span className="badge">{i.quantity<(i.reorder_threshold??5)?'Low stock':'In stock'}</span></p><label>Reorder threshold<input type="number" min="0" step="1" value={thresholds[i.id]??String(i.reorder_threshold??5)} onChange={e=>setThresholds({...thresholds,[i.id]:e.target.value})}/></label><button onClick={()=>{const n=Number(thresholds[i.id]??i.reorder_threshold??5);if(!Number.isInteger(n)||n<0){setError('Threshold must be a nonnegative integer');return}void api('/items/'+i.id+'/threshold',{reorder_threshold:n},'PUT').then(()=>{setMessage('Threshold saved');void refresh()}).catch(e=>setError(String(e)))}}>Save threshold</button></article>)}'''),
'issues-large': (
"const [columns,setColumns]=useState<{id:number;name:string}[]>([]),[columnName,setColumnName]=useState(''),[moves,setMoves]=useState<Record<number,number>>({}),[history,setHistory]=useState('');const loadColumns=()=>api('/columns').then(setColumns);useEffect(()=>{void loadColumns()},[]);",
'''<form onSubmit={e=>{e.preventDefault();void api('/columns',{name:columnName}).then(()=>{setColumnName('');void loadColumns()}).catch(e=>setError(String(e)))}}><label>Column name<input required maxLength={60} value={columnName} onChange={e=>setColumnName(e.target.value)}/></label><button>Add column</button></form><div className="board">{columns.map(c=><section key={c.id} aria-label={c.name}><h2>{c.name}</h2>{issues.filter(i=>i.column_id===c.id).map(i=><article key={i.id}><h3>{i.title}</h3><p>{i.assignee}</p><label>Move to<select value={moves[i.id]??i.column_id} onChange={e=>setMoves({...moves,[i.id]:Number(e.target.value)})}>{columns.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select></label><button onClick={()=>{void api('/issues/'+i.id+'/move',{column_id:moves[i.id]??i.column_id},'PUT').then(refresh).catch(e=>setError(String(e)))}}>Move</button><button onClick={()=>{void api('/issues/'+i.id+'/history').then(h=>setHistory(h.map((event:{from_column:number;to_column:number;created_at:string})=>'Moved from '+(columns.find(c=>c.id===event.from_column)?.name??event.from_column)+' to '+(columns.find(c=>c.id===event.to_column)?.name??event.to_column)+' at '+event.created_at).join('; ')))}}>Show history</button></article>)}{!issues.some(i=>i.column_id===c.id)&&<p>No issues</p>}</section>)}</div><pre aria-label="Transition history">{history}</pre>'''),
'inventory-large': (
"const [supplier,setSupplier]=useState(''),[quantities,setQuantities]=useState<Record<number,string>>({}),[orders,setOrders]=useState<{id:number;supplier:string;status:string}[]>([]),[history,setHistory]=useState('');const loadOrders=()=>api('/orders').then(setOrders);useEffect(()=>{void loadOrders()},[]);",
'''<section className="panel"><h2>New purchase order</h2><label>Supplier<input required maxLength={80} value={supplier} onChange={e=>setSupplier(e.target.value)}/></label>{items.map(i=><label key={i.id}>Order quantity for {i.name}<input aria-label={'Order quantity for '+i.name} type="number" min="0" step="1" value={quantities[i.id]??'0'} onChange={e=>setQuantities({...quantities,[i.id]:e.target.value})}/></label>)}<button onClick={()=>{const values=Object.entries(quantities).map(([id,q])=>({item_id:Number(id),quantity:Number(q)}));const lines=values.filter(l=>l.quantity>0);if(!supplier.trim()||!lines.length||values.some(l=>!Number.isInteger(l.quantity)||l.quantity<0)){setError('Supplier and positive integer quantities required');return}void api('/orders',{supplier,lines}).then(()=>{setMessage('Order created');void loadOrders()}).catch(e=>setError(String(e)))}}>Create order</button></section>{orders.map(o=><article key={o.id}><h2>{o.supplier}</h2><p>{o.status}</p><button disabled={o.status==='received'} onClick={()=>{void api('/orders/'+o.id+'/receive',{}).then(()=>{void refresh();void loadOrders()}).catch(e=>setError(String(e)))}}>Receive order</button><button onClick={()=>{void api('/orders/'+o.id+'/history').then(h=>setHistory(h.map((event:{created_at:string})=>'Received at '+event.created_at).join('; ')))}}>Show history</button></article>)}<pre aria-label="Receipt history">{history}</pre><table><thead><tr><th>Item</th><th>Quantity</th></tr></thead><tbody>{items.map(i=><tr key={i.id}><td>{i.name}</td><td>{i.quantity}</td></tr>)}</tbody></table>''')
}


def apply(project, task_id):
    project=Path(project)
    backend=project/'backend/app.py'
    source=backend.read_text()
    addition={'issues-medium':SETTINGS,'inventory-medium':THRESHOLDS,'issues-large':WORKFLOW,'inventory-large':ORDERS}.get(task_id,'')
    source=source.replace('# Additional API routes',addition+'\n# Additional API routes')
    if task_id=='issues-medium':
        source=source.replace("value.assignee or 'Unassigned'", "value.assignee if value.assignee is not None else settings()['default_assignee']")
    backend.write_text(source)
    state,markup=FRONT[task_id]
    base=(project/'frontend/src/App.tsx').read_text()
    prefix=base[:base.index('export default function App()')]
    code=prefix+'''export default function App() {
 const [issues,setIssues]=useState<Issue[]>([]),[items,setItems]=useState<Item[]>([]),[name,setName]=useState(''),[error,setError]=useState(''),[message,setMessage]=useState('');
 const refresh=()=>Promise.all([api('/issues').then(setIssues),api('/items').then(setItems)]).catch(e=>setError(String(e)));
 useEffect(()=>{void refresh()},[]);
 '''+state+'''
 return <main><header><p className="eyebrow">WORKSPACE</p><h1>{settings.title}</h1><p>{settings.description}</p></header>{error&&<p role="alert">{error}</p>}{message&&<p role="status">{message}</p>}
 <form onSubmit={e=>{e.preventDefault();void api(settings.kind==='issues'?'/issues':'/items',settings.kind==='issues'?{title:name}:{name,quantity:0}).then(()=>{setName('');void refresh()}).catch(e=>setError(String(e)))}}><label>{settings.kind==='issues'?'Issue title':'Item name'}<input required value={name} onChange={e=>setName(e.target.value)}/></label><button>Add {settings.kind==='issues'?'issue':'item'}</button></form>
 '''+markup+'''</main>;
}
'''
    (project/'frontend/src/App.tsx').write_text(code)
    with (project/'README.md').open('a') as doc:
        doc.write('\n## Feature: '+task_id+'\n\nFeature data persists in SQLite. New controls include validation and preserve existing records. Run the regression suite before committing.\n')


def visual_screens(project, task_id):
    """Reference navigation for Large design briefs; coding UI stays independent."""
    if task_id not in ('issues-large', 'inventory-large'):
        return
    path = Path(project) / 'frontend/src/App.tsx'
    source = path.read_text()
    screens = ['Board', 'History'] if task_id == 'issues-large' else ['Orders', 'Inventory', 'History']
    source = source.replace("export default function App() {", "export default function App() {\n const [view,setView]=useState('"+screens[0]+"');")
    source = source.replace('[history,setHistory]', '[history,saveHistory]')
    source = source.replace(' return <main>', " const setHistory=(value:string)=>{saveHistory(value);setView('History')};\n return <main>")
    navigation = '<nav aria-label="Screens" className="controls">{'+str(screens)+'.map(v=><button key={v} aria-pressed={view===v} onClick={()=>setView(v)}>{v}</button>)}</nav>'
    source = source.replace('</header>', '</header>'+navigation)
    markup = FRONT[task_id][1]
    content, tail = markup.split('<pre ',1)
    history, after = ('<pre '+tail).split('</pre>',1)
    replacement = "{view==='"+screens[0]+"'&&<>"+content+"</>}{view==='History'&&<>"+history+'</pre></>}'
    if task_id == 'inventory-large':
        replacement += "{view==='Inventory'&&<>"+after+'</>}'
    source = source.replace(markup, replacement)
    path.write_text(source)
