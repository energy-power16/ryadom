"""Подбор волонтёров: берём ранжирование Геммы из ответа на звонок, иначе — правила."""
import logging
from typing import List

from . import store

log = logging.getLogger("ryadom.matching")

LANG = {"es": "испанский", "ca": "каталанский", "en": "английский"}


def _score(req: dict, v: dict) -> int:
    skills = set(v["skills"])
    s = 3 * len(skills & set(req.get("topics", []))) + 2 * len(skills & set(req.get("help", [])))
    if req.get("needs_language") and req["needs_language"] in v["languages"]:
        s += 2
    d = (req.get("district") or "").lower()
    if d and any(d in x.lower() or x.lower() in d for x in v["districts"]):
        s += 1
    return s


def _rule_reason(req: dict, v: dict) -> str:
    parts = []
    common = [t for t in req.get("topics", []) + req.get("help", []) if t in v["skills"]]
    if common:
        parts.append("умеет: " + ", ".join(common))
    lang = req.get("needs_language")
    if lang in v["languages"]:
        parts.append(f"говорит на языке: {LANG.get(lang, lang)}")
    return ", ".join(parts).capitalize() or "Свободен"


def _public(v: dict, reason: str) -> dict:
    return {"id": v["id"], "name": v["name"], "languages": v["languages"],
            "districts": v["districts"], "reason": reason}


def candidates(req: dict, k: int = 3) -> List[dict]:
    vols = store.list_volunteers()
    by_id = {v["id"]: v for v in vols}
    # 1) ранжирование, которое Gemma вернула в том же ответе, что и заявку
    ranked = []
    for r in req.pop("ranking", None) or []:
        try:
            v = by_id.get(int(r["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if v and all(x["id"] != v["id"] for x in ranked):
            ranked.append(_public(v, r.get("reason") or _rule_reason(req, v)))
    if ranked:
        return ranked[:k]
    # 2) иначе — по правилам
    scored = sorted(((_score(req, v), v) for v in vols), key=lambda x: -x[0])
    return [_public(v, _rule_reason(req, v)) for s, v in scored if s > 0][:k]
