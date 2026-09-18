from fastapi.testclient import TestClient
from backend.app import app
client = TestClient(app)

def test_seed_and_creation():
    assert len(client.get('/api/issues').json()) >= 3
    assert len(client.get('/api/items').json()) >= 3
    issue = client.post('/api/issues',json={'title':'Regression issue','assignee':'Taylor'})
    assert issue.status_code == 201 and issue.json()['assignee'] == 'Taylor'
    item = client.post('/api/items',json={'name':'Regression item','quantity':7})
    assert item.status_code == 201 and item.json()['quantity'] == 7
    assert client.post('/api/items',json={'name':'Bad','quantity':-1}).status_code == 422
