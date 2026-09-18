"""Local application service. Database location is configurable for tests."""
import json
import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
KIND = json.loads((ROOT / 'app.json').read_text())['kind']
DB = os.environ.get('APP_DATABASE', str(ROOT / 'app.sqlite3'))
app = FastAPI(title='Issue tracker' if KIND == 'issues' else 'Inventory desk')


def connection():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def initialize():
    with connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT "open", assignee TEXT NOT NULL DEFAULT "Unassigned")')
        db.execute('CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL)')
        if not db.execute('SELECT 1 FROM issues').fetchone():
            db.executemany('INSERT INTO issues(title,status,assignee) VALUES(?,?,?)', [('Fix login','open','Maya'),('Refresh docs','closed','Kai'),('Add export','open','Maya')])
        if not db.execute('SELECT 1 FROM items').fetchone():
            db.executemany('INSERT INTO items(name,quantity) VALUES(?,?)', [('Adapters',2),('Cables',38),('Batteries',12)])


initialize()


class Issue(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    assignee: str | None = None


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=0, strict=True)


@app.get('/api/issues')
def issues():
    with connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM issues ORDER BY id')]


@app.post('/api/issues', status_code=201)
def create_issue(value: Issue):
    with connection() as db:
        cursor = db.execute('INSERT INTO issues(title,assignee) VALUES(?,?)', (value.title, value.assignee or 'Unassigned'))
        return dict(db.execute('SELECT * FROM issues WHERE id=?', (cursor.lastrowid,)).fetchone())


@app.get('/api/items')
def items():
    with connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM items ORDER BY id')]


@app.post('/api/items', status_code=201)
def create_item(value: Item):
    with connection() as db:
        cursor = db.execute('INSERT INTO items(name,quantity) VALUES(?,?)', (value.name, value.quantity))
        return dict(db.execute('SELECT * FROM items WHERE id=?', (cursor.lastrowid,)).fetchone())


# Additional API routes must be declared before the static application mount.
if (ROOT / 'frontend/dist').exists():
    app.mount('/', StaticFiles(directory=ROOT / 'frontend/dist', html=True), name='ui')
