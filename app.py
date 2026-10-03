"""Simple Chat API: auth, chat, persistence, streaming. Run: uvicorn app:app --reload"""
import os
import time
from datetime import datetime, timedelta, timezone

import base64
import hashlib
import hmac
import secrets
import jwt
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from dotenv import load_dotenv
load_dotenv()  # reads .env into environment variables

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover - optional dependency for future LLM integration
    Anthropic = None

# ---------- Config ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./chat.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-in-production-32b")
TOKEN_MINUTES = 60

# ---------- Database ----------
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), default="New chat")
    messages: Mapped[list["Message"]] = relationship(
        cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text)


Base.metadata.create_all(engine)


def get_db():
    with Session(engine) as db:
        yield db


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    salt_text = base64.urlsafe_b64encode(salt).decode().rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"pbkdf2_sha256$260000${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_text, digest_text = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), decode(salt_text), int(iterations)
        )
        return hmac.compare_digest(digest, decode(digest_text))
    except (ValueError, TypeError):
        return False


# ---------- Schemas (request/response contracts) ----------
class AuthIn(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=72)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ConversationIn(BaseModel):
    title: str = Field(default="New chat", max_length=100)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: int
    title: str
    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


# ---------- Auth helpers ----------
bearer = HTTPBearer()


def make_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MINUTES)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm="HS256")


def current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    try:
        user_id = int(jwt.decode(creds.credentials, SECRET_KEY, algorithms=["HS256"])["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, "Invalid or expired token")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(401, "User not found")
    return user


def owned_conversation(
    conversation_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    return conv


llm = Anthropic() if os.getenv("ANTHROPIC_API_KEY") else None


def generate_reply(history: list[dict]):
    if llm is None:  # no key: fake reply for local testing
        reply = f"You said: {history[-1]['content']}"
        for i, word in enumerate(reply.split()):
            time.sleep(0.05)
            yield word if i == 0 else " " + word
        return

    with llm.messages.stream(
        model="claude-sonnet-5-5",
        max_tokens=1024,
        system="You are a helpful assistant. Answer in plain text without Markdown, in under 150 words.",
        messages=history,
    ) as stream:
        for text in stream.text_stream:
            yield text


def history_of(conv: Conversation) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in conv.messages]


# ---------- App & routes ----------
app = FastAPI(title="Simple Chat API")


@app.post("/auth/register", status_code=201)
def register(body: AuthIn, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "Username already taken")
    hashed = hash_password(body.password)
    user = User(username=body.username, password_hash=hashed)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username}


@app.post("/auth/login", response_model=TokenOut)
def login(body: AuthIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong username or password")
    return TokenOut(access_token=make_token(user.id))


@app.post("/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(
    body: ConversationIn, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    conv = Conversation(user_id=user.id, title=body.title)
    db.add(conv)
    db.commit()
    return conv


@app.get("/conversations", response_model=list[ConversationOut])
def list_conversations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Conversation).where(Conversation.user_id == user.id)).all()


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conv: Conversation = Depends(owned_conversation)):
    return conv


@app.post("/conversations/{conversation_id}/messages", response_model=MessageOut)
def send_message(
    body: MessageIn, conv: Conversation = Depends(owned_conversation), db: Session = Depends(get_db)
):
    conv.messages.append(Message(role="user", content=body.content))
    reply = "".join(generate_reply(history_of(conv)))
    assistant = Message(role="assistant", content=reply)
    conv.messages.append(assistant)
    db.commit()
    return assistant


@app.post("/conversations/{conversation_id}/stream")
def stream_message(body: MessageIn, conv: Conversation = Depends(owned_conversation)):
    conv_id = conv.id

    def event_stream():
        # Own DB session: this generator keeps running while the response streams.
        with Session(engine) as db:
            c = db.get(Conversation, conv_id)
            c.messages.append(Message(role="user", content=body.content))
            db.commit()
            parts = []
            for chunk in generate_reply(history_of(c)):
                parts.append(chunk)
                yield chunk  # sent to the client immediately
            c.messages.append(Message(role="assistant", content="".join(parts)))
            db.commit()

    return StreamingResponse(event_stream(), media_type="text/plain")
