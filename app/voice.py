"""Голосовое подтверждение звонящему через ElevenLabs Text-to-Speech."""
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from .store import AUDIO_DIR

log = logging.getLogger("ryadom.voice")


def confirm_text(req: dict, volunteer: str) -> str:
    greet = f"{req['name']}, здравствуйте!" if req.get("name") else "Здравствуйте!"
    when = f" Встреча: {req['when']}." if req.get("when") else ""
    return (f"{greet} Это линия «Рядом». Мы нашли вам помощника: "
            f"волонтёр {volunteer} скоро свяжется с вами.{when} Всего доброго!")


def confirm(req: dict, volunteer: str) -> Optional[Path]:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return None
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    try:
        r = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": key},
            json={"text": confirm_text(req, volunteer), "model_id": "eleven_multilingual_v2"},
            timeout=60)
    except Exception as e:  # голос — бонус, он не должен ронять взятие заявки
        log.warning("TTS error: %s", e)
        return None
    if r.status_code != 200:
        log.warning("TTS error %s: %s", r.status_code, r.text[:200])
        return None
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIO_DIR / f"{req['id']}.mp3"
    path.write_bytes(r.content)
    return path
