import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"  # set before importing the app

import pytest
from fastapi.testclient import TestClient

import app as app_module   
from app import Base, app, engine

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    # Force the echo fallback: tests stay fast, free and predictable.
    monkeypatch.setattr(app_module, "llm", None)

def auth(username="alice", password="secret123"):
    client.post("/auth/register", json={"username": username, "password": password})
    token = client.post("/auth/login", json={"username": username, "password": password}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


def new_chat(headers):
    return client.post("/conversations", json={"title": "Test"}, headers=headers).json()["id"]


def test_register_login():
    r = client.post("/auth/register", json={"username": "bob", "password": "secret123"})
    assert r.status_code == 201
    r = client.post("/auth/login", json={"username": "bob", "password": "secret123"})
    assert "access_token" in r.json()


def test_duplicate_username():
    auth()
    r = client.post("/auth/register", json={"username": "alice", "password": "secret123"})
    assert r.status_code == 409


def test_wrong_password():
    auth()
    r = client.post("/auth/login", json={"username": "alice", "password": "wrongpass"})
    assert r.status_code == 401


def test_needs_token():
    assert client.get("/conversations").status_code in (401, 403)


def test_chat_and_persistence():
    h = auth()
    cid = new_chat(h)
    r = client.post(f"/conversations/{cid}/messages", json={"content": "hi"}, headers=h)
    assert r.json()["content"] == "You said: hi"
    msgs = client.get(f"/conversations/{cid}", headers=h).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_streaming():
    h = auth()
    cid = new_chat(h)
    with client.stream("POST", f"/conversations/{cid}/stream", json={"content": "hello"}, headers=h) as r:
        text = "".join(r.iter_text())
    assert text == "You said: hello"
    msgs = client.get(f"/conversations/{cid}", headers=h).json()["messages"]
    assert msgs[-1]["content"] == "You said: hello"


def test_cannot_read_other_users_chat():
    cid = new_chat(auth("alice"))
    r = client.get(f"/conversations/{cid}", headers=auth("mallory"))
    assert r.status_code == 404


def test_empty_message_rejected():
    h = auth()
    cid = new_chat(h)
    r = client.post(f"/conversations/{cid}/messages", json={"content": ""}, headers=h)
    assert r.status_code == 422
