import asyncio
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import matching, store, telegram  # noqa: E402
from .tags import ALL, HELP_TYPES, TOPICS, clean  # noqa: E402
from .gemma import extract_request  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(telegram.run())
    yield
    task.cancel()


app = FastAPI(title="Рядом — линия взаимопомощи", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# Простой лимит запросов для публичной демки: N запросов в минуту с одного IP
_hits = defaultdict(deque)


def rate_limit(request: Request, bucket: str, per_minute: int) -> None:
    key = (bucket, request.client.host if request.client else "?")
    now, q = time.monotonic(), _hits[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(429, "Слишком много запросов. Подождите минуту и попробуйте снова.")
    q.append(now)


class Call(BaseModel):
    transcript: str


class Take(BaseModel):
    volunteer: str


class VolunteerIn(BaseModel):
    name: str
    languages: List[str] = []
    districts: List[str] = []
    skills: List[str] = []
    contact: Optional[str] = None


# ---------- страницы ----------

@app.get("/")
def call_page():
    return FileResponse(STATIC / "call.html")


@app.get("/volunteers")
def dashboard_page():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/join")
def join_page():
    return FileResponse(STATIC / "join.html")


# ---------- API ----------

@app.get("/api/config")
async def config():
    """Отдаёт фронту agentId или signed URL (если задан ключ ElevenLabs)."""
    agent_id = os.getenv("ELEVENLABS_AGENT_ID", "")
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if agent_id and api_key:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
                params={"agent_id": agent_id}, headers={"xi-api-key": api_key})
        if r.status_code == 200:
            return {"signedUrl": r.json()["signed_url"]}
    return {"agentId": agent_id}


@app.post("/api/calls")
def new_call(call: Call, request: Request):
    rate_limit(request, "calls", 10)
    if not call.transcript.strip():
        raise HTTPException(400, "Пустой разговор — расскажите, с чем нужна помощь.")
    trace = []
    t0 = time.monotonic()
    data = extract_request(call.transcript, store.list_volunteers())
    source = data.get("_source")
    trace.append({"step": "Разбор звонка и подбор волонтёров",
                  "detail": source if source != "fallback" else "Gemma недоступна → правила",
                  "ms": int((time.monotonic() - t0) * 1000)})
    trace.append({"step": "Проверка тегов по справочнику экспертизы",
                  "detail": ", ".join(data.get("topics", []) + data.get("help", []))})
    t1 = time.monotonic()
    data["candidates"] = matching.candidates(data)
    trace.append({"step": "Кандидаты",
                  "detail": ", ".join(c["name"] for c in data["candidates"]) or "никого — нужен координатор",
                  "ms": int((time.monotonic() - t1) * 1000)})
    req_id = store.add_request(call.transcript, data)
    data["notified"] = telegram.notify(req_id, data)
    trace.append({"step": "Telegram", "detail": f"заявка отправлена {data['notified']} волонтёрам"
                  if telegram.token() else "бот не подключён — заявка на панели"})
    data["requester_link"] = telegram.requester_link(req_id)
    if telegram.link_requester(req_id):
        data["requester_chat"] = True
        trace.append({"step": "Человеку в Telegram", "detail": "сообщение «заявка в работе» отправлено"})
    data["_trace"] = trace
    return {"id": req_id, **data}


@app.get("/api/requests")
def requests():
    return store.all_requests()


@app.post("/api/requests/{req_id}/take")
def take(req_id: int, body: Take, background: BackgroundTasks):
    req = store.take(req_id, body.volunteer)
    if req is None:
        raise HTTPException(409, "заявку уже взяли")
    background.add_task(telegram.after_take, req, body.volunteer)
    return {"ok": True}


@app.get("/api/requests/{req_id}/audio")
def audio(req_id: int):
    path = store.AUDIO_DIR / f"{req_id}.mp3"
    if not path.exists():
        raise HTTPException(404, "аудио ещё нет")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/tags")
def tags():
    return {"topics": TOPICS, "help": HELP_TYPES}


@app.get("/api/volunteers")
def volunteers():
    return [{**{k: v[k] for k in ["id", "name", "languages", "districts", "skills"]},
             "telegram": bool(v["tg_chat_id"])} for v in store.list_volunteers()]


@app.post("/api/volunteers")
def join(v: VolunteerIn, request: Request):
    rate_limit(request, "join", 5)
    data = v.dict()
    data["skills"] = clean(data["skills"], ALL)
    if not v.name.strip() or not data["skills"]:
        raise HTTPException(400, "нужно имя и хотя бы одно направление помощи")
    vol_id = store.add_volunteer(data)
    return {"id": vol_id, "telegram_link": telegram.bot_link(vol_id)}
