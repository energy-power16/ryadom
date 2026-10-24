"""Telegram-бот: волонтёр подключается по ссылке из регистрации и получает заявки с кнопкой «Беру»."""
import asyncio
import logging
import os
import re
from typing import Optional

import httpx

from . import store, voice

log = logging.getLogger("ryadom.telegram")
API = "https://api.telegram.org/bot{token}/{method}"
state = {"username": None}

URG = {"high": "🔴 срочно", "medium": "🟡 на неделе", "low": "🟢 без срока"}


def token() -> Optional[str]:
    return os.getenv("TELEGRAM_BOT_TOKEN")


def bot_link(vol_id: int) -> Optional[str]:
    return f"https://t.me/{state['username']}?start=v{vol_id}" if state["username"] else None


def requester_link(req_id: int) -> Optional[str]:
    """Ссылка для того, кто просит помощи: бот пришлёт контакт волонтёра, когда тот согласится."""
    return f"https://t.me/{state['username']}?start=r{req_id}" if state["username"] else None


def _volunteer_contact(v: Optional[dict]) -> str:
    if not v:
        return "координатор передаст контакт"
    return v.get("contact") or "напишите волонтёру через координатора"


def _requester_contact(req: dict) -> str:
    parts = [req.get("name") or "без имени"]
    if req.get("requester_tg"):
        parts.append("@" + req["requester_tg"])
    if req.get("contact"):
        parts.append(req["contact"])
    if len(parts) == 1:
        parts.append("контакт не указан, координатор передаст")
    return ", ".join(parts)


def _request_text(req_id: int, data: dict, reason: str = "") -> str:
    lines = [f"🆘 Заявка #{req_id} · {URG.get(data.get('urgency'), '')}",
             f"{data.get('summary', '')}",
             "🏷 " + ", ".join(data.get("topics", []) + data.get("help", []))]
    if data.get("when"):
        lines.append(f"🕒 {data['when']}")
    if data.get("district"):
        lines.append(f"📍 {data['district']}")
    if reason:
        lines.append(f"\nПочему вы: {reason}")
    return "\n".join(lines)


def send_text(chat_id: int, text: str) -> None:
    if not token() or not chat_id:
        return
    try:
        httpx.post(API.format(token=token(), method="sendMessage"), timeout=10,
                   json={"chat_id": chat_id, "text": text[:4000]})
    except httpx.HTTPError as e:
        log.warning("sendMessage error: %s", e)


def notify(req_id: int, data: dict) -> int:
    """Рассылает заявку подходящим волонтёрам, у которых подключён Telegram. Возвращает число отправок."""
    if not token():
        return 0
    sent = 0
    for c in data.get("candidates", []):
        v = store.get_volunteer(c["id"])
        if not v or not v.get("tg_chat_id"):
            continue
        try:
            httpx.post(API.format(token=token(), method="sendMessage"), timeout=10, json={
                "chat_id": v["tg_chat_id"],
                "text": _request_text(req_id, data, c.get("reason", "")),
                "reply_markup": {"inline_keyboard": [[
                    {"text": "✋ Беру", "callback_data": f"take:{req_id}:{v['id']}"}]]},
            })
            sent += 1
        except httpx.HTTPError as e:
            log.warning("sendMessage error: %s", e)
    return sent


async def _call(client: httpx.AsyncClient, method: str, **params) -> dict:
    r = await client.post(API.format(token=token(), method=method), json=params, timeout=45)
    return r.json()


async def _handle(client: httpx.AsyncClient, upd: dict) -> None:
    msg = upd.get("message")
    if msg and (msg.get("from") or {}).get("username"):
        await asyncio.to_thread(store.remember_tg_user, msg["from"]["username"], msg["chat"]["id"])
    if msg and msg.get("text", "").startswith("/start"):
        chat_id = msg["chat"]["id"]
        payload = msg["text"].split(maxsplit=1)[1] if " " in msg["text"] else ""
        username = (msg.get("from") or {}).get("username")
        if payload.startswith("v") and payload[1:].isdigit():
            v = await asyncio.to_thread(store.set_chat_id, int(payload[1:]), chat_id, username)
            text = (f"Готово, {v['name']}! Подходящие заявки будут приходить сюда. Спасибо, что вы рядом 🤝"
                    if v else "Не нашли вашу регистрацию — зарегистрируйтесь на сайте ещё раз.")
        elif payload.startswith("r") and payload[1:].isdigit():
            req_id = int(payload[1:])
            req = await asyncio.to_thread(store.get_request, req_id)
            if not req:
                text = "Не нашли такую заявку. Позвоните на линию ещё раз."
            else:
                await asyncio.to_thread(store.set_field, req_id, "requester_chat", chat_id)
                if username:
                    await asyncio.to_thread(store.set_field, req_id, "requester_tg", username)
                req = await asyncio.to_thread(store.get_request, req_id)
                text = ("Здравствуйте! Это «Рядом».\n\n" + await asyncio.to_thread(status_text, req)
                        + "\n\nНапишите сюда любое сообщение или /status, чтобы узнать статус.")
        else:
            text = ("Здравствуйте! Это бот линии «Рядом». Если вы оставили заявку и назвали свой Telegram, "
                    "я пришлю сюда, когда волонтёр её возьмёт. Волонтёрам — регистрация на сайте.")
        await _call(client, "sendMessage", chat_id=chat_id, text=text)
        return

    if msg and msg.get("text"):
        chat_id = msg["chat"]["id"]
        reqs = await asyncio.to_thread(store.requests_of, chat_id)
        if reqs:
            parts = [await asyncio.to_thread(status_text, r) for r in reqs[:5]]
            await _call(client, "sendMessage", chat_id=chat_id, text="\n\n".join(parts))
        else:
            await _call(client, "sendMessage", chat_id=chat_id,
                        text="Заявок, привязанных к этому чату, пока нет. Откройте ссылку или QR-код, "
                             "который появился после звонка, или назовите свой Telegram оператору.")
        return

    cb = upd.get("callback_query")
    if cb and cb.get("data", "").startswith("take:"):
        _, req_id, vol_id = cb["data"].split(":")
        v = await asyncio.to_thread(store.get_volunteer, int(vol_id))
        req = await asyncio.to_thread(store.take, int(req_id), v["name"] if v else "волонтёр")
        if req is None:
            await _call(client, "answerCallbackQuery", callback_query_id=cb["id"],
                        text="Эту заявку уже взяли. Спасибо!")
            return
        await _call(client, "answerCallbackQuery", callback_query_id=cb["id"], text="Заявка ваша!")
        m = cb["message"]
        await _call(client, "editMessageText", chat_id=m["chat"]["id"], message_id=m["message_id"],
                    text=m.get("text", "") + "\n\n✅ Вы взяли эту заявку")
        await asyncio.to_thread(after_take, req, v["name"] if v else "волонтёр", m["chat"]["id"])


def status_text(req: dict) -> str:
    head = f"Заявка #{req['id']}: {req.get('summary', '')}"
    if req.get("status") == "taken":
        v = store.find_volunteer(req.get("volunteer") or "")
        return (f"✅ {head}\nСтатус: взята. Вам поможет волонтёр {req.get('volunteer')}.\n"
                f"Контакт волонтёра: {_volunteer_contact(v)}")
    return f"📝 {head}\nСтатус: в работе — ищем волонтёра. Как только кто-то возьмёт заявку, я напишу."


USERNAME = re.compile(r"@([A-Za-z0-9_]{4,32})")


def _find_requester(req: dict) -> Optional[tuple]:
    """@username, названный в звонке (поле contact или расшифровка) → чат, если человек писал боту."""
    for source in (req.get("contact") or "", req.get("transcript") or ""):
        for name in USERNAME.findall(source):
            chat = store.chat_by_username(name)
            if chat:
                return name, chat
    return None


def link_requester(req_id: int) -> bool:
    """После звонка: если просящий назвал свой Telegram и уже писал боту — сообщаем, что заявка в работе."""
    if not token():
        return False
    req = store.get_request(req_id)
    found = _find_requester(req) if req else None
    if not found:
        return False
    name, chat = found
    store.set_field(req_id, "requester_chat", chat)
    store.set_field(req_id, "requester_tg", name)
    send_text(chat, status_text(store.get_request(req_id)))
    return True


def after_take(req: dict, volunteer: str, chat_id: Optional[int] = None) -> None:
    """Волонтёр согласился: обмен контактами, памятка волонтёру (Gemma + база знаний),
    голосовое подтверждение звонящему."""
    from . import briefing
    v = store.find_volunteer(volunteer)
    if chat_id is None and v:
        chat_id = v.get("tg_chat_id")
    req = store.get_request(req["id"]) or req  # свежие данные: просящий мог подключиться к боту
    if not req.get("requester_chat"):
        found = _find_requester(req)
        if found:
            req["requester_tg"], req["requester_chat"] = found
            store.set_field(req["id"], "requester_chat", found[1])
            store.set_field(req["id"], "requester_tg", found[0])
    # 1. Волонтёру — контакт человека (только после его согласия)
    send_text(chat_id, f"🤝 Спасибо, что взяли заявку #{req['id']}!\nКонтакт человека: {_requester_contact(req)}")
    # 2. Просящему — контакт волонтёра
    if req.get("requester_chat"):
        send_text(req["requester_chat"], f"✅ Заявка #{req['id']} взята! Вам поможет волонтёр {volunteer}.\n"
                                         f"Контакт волонтёра: {_volunteer_contact(v)}\n"
                                         "Волонтёр тоже получил ваш контакт и скоро свяжется с вами.")
    # 3. Памятка волонтёру
    try:
        text = briefing.build(req)
        store.set_field(req["id"], "briefing", text)
        send_text(chat_id, "📋 Памятка к заявке #%s\n\n%s" % (req["id"], text))
    except Exception as e:
        log.warning("briefing error: %s", e)
    # 4. Голосовое подтверждение
    voice.confirm(req, volunteer)


async def run() -> None:
    """Long polling. Запускается фоном при старте сервера, если задан TELEGRAM_BOT_TOKEN."""
    if not token():
        return
    async with httpx.AsyncClient() as client:
        try:
            me = await _call(client, "getMe")
            state["username"] = me.get("result", {}).get("username")
            log.warning("Telegram bot @%s запущен", state["username"])
        except Exception as e:
            log.warning("Telegram getMe failed: %s", e)
            return
        offset = 0
        while True:
            try:
                upd = await _call(client, "getUpdates", offset=offset, timeout=30)
                for u in upd.get("result", []):
                    offset = u["update_id"] + 1
                    try:
                        await _handle(client, u)
                    except Exception as e:
                        log.warning("handle error: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("polling error: %s", e)
                await asyncio.sleep(3)
