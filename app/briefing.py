"""Памятка волонтёру: Gemma 4 собирает её из базы знаний команды (data/expertise.md) под конкретную заявку."""
import logging
import re
from pathlib import Path

from .gemma import generate

log = logging.getLogger("ryadom.briefing")
EXPERTISE = Path(__file__).resolve().parent.parent / "data" / "expertise.md"

PROMPT = """Ты помогаешь волонтёру линии «Рядом» подготовиться к заявке.
Составь короткую памятку по-русски, строго опираясь на базу знаний ниже.
Не добавляй фактов, документов и адресов, которых нет в базе. Если чего-то не хватает,
напиши «уточнить у человека».

Формат (простой текст, без markdown-заголовков, до 120 слов):
Что сделать: 1–2 предложения.
Что взять: список через запятую.
Фразы: 2–3 фразы на нужном языке с переводом.
Важно: одно предупреждение.

Заявка:
{request}

База знаний:
{knowledge}
"""


def _sections(topics: list) -> str:
    """Разделы базы для тем заявки + общие правила."""
    text = EXPERTISE.read_text(encoding="utf-8")
    parts = re.split(r"^## ", text, flags=re.M)
    by_name = {p.split("\n", 1)[0].strip(): "## " + p.strip() for p in parts[1:]}
    keys = [t for t in topics if t in by_name] + ["как помогать пожилым с техникой", "общие правила волонтёра"]
    return "\n\n".join(by_name[k] for k in keys if k in by_name)


def build(req: dict) -> str:
    knowledge = _sections(req.get("topics") or [])
    request = "\n".join(f"{k}: {req.get(k)}" for k in ("summary", "topics", "help", "needs_language", "when", "district")
                        if req.get(k))
    text = generate(PROMPT.format(request=request, knowledge=knowledge))
    if text:
        return text.strip()
    # Gemma недоступна — отдаём нужные разделы базы как есть
    return "Из базы знаний:\n" + re.sub(r"^## ", "", knowledge, flags=re.M)
