"""Сравнение: голая Gemma 4 vs «Рядом» (та же модель + наш слой) vs только правила.

Запуск из корня проекта (нужен GEMINI_API_KEY в .env):
    .venv/bin/python -m eval.run_eval
С «размышлениями» модели:
    GEMMA_THINKING=1 .venv/bin/python -m eval.run_eval

Результат — таблица в eval/results.md.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
# В eval меряем модель, а не наши таймауты демо: даём ей больше времени
os.environ.setdefault("GEMMA_DEADLINE_S", "120")

from app import gemma, matching, store  # noqa: E402
from app.tags import HELP_TYPES, TOPICS  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES = json.loads((HERE / "calls.json").read_text(encoding="utf-8"))

# Отдельная временная база с теми же 5 тестовыми волонтёрами (id 1–5)
store.DB = Path(tempfile.mkdtemp()) / "eval.db"
VOLS = store.list_volunteers()

BASELINE_PROMPT = """Вот звонок на линию помощи русскоязычным в Барселоне и список волонтёров.
Определи, что нужно человеку, и выбери лучшего волонтёра.
Ответь JSON: {{"name": ..., "when": ..., "district": ..., "topics": [...], "help": [...], "needs_language": "es|ca|en|null", "urgency": "high|medium|low", "volunteer_id": <id>}}
topics — из списка {topics}; help — из списка {help}.

Волонтёры: {volunteers}

Звонок: {transcript}
"""


def baseline(transcript):
    vols = [{k: v[k] for k in ("id", "name", "languages", "districts", "skills")} for v in VOLS]
    text = gemma.generate(BASELINE_PROMPT.format(topics=list(TOPICS), help=list(HELP_TYPES),
                                                 volunteers=json.dumps(vols, ensure_ascii=False),
                                                 transcript=transcript))
    if text is None:
        return None
    try:
        d = gemma.extract_json(text)
    except Exception:
        return {}
    return {"topics": d.get("topics") or [], "help": d.get("help") or [], "lang": d.get("needs_language"),
            "urgency": d.get("urgency"), "top": d.get("volunteer_id"),
            "name": d.get("name"), "when": d.get("when"), "district": d.get("district")}


def ryadom(transcript, use_gemma=True):
    if use_gemma:
        d = gemma.extract_request(transcript, VOLS)
    else:
        d = gemma._fallback(transcript)
    cands = matching.candidates(d)
    return {"topics": d.get("topics", []), "help": d.get("help", []), "lang": d.get("needs_language"),
            "urgency": d.get("urgency"), "top": cands[0]["id"] if cands else None, "source": d.get("_source"),
            "name": d.get("name"), "when": d.get("when"), "district": d.get("district")}


def norm_lang(x):
    return None if x in (None, "", "null", "None") else str(x).lower()


def details_ok(pred, case):
    """Имя/время/район извлечены верно, а где их нет в звонке — не выдуманы."""
    for field, gold in case["details"].items():
        val = pred.get(field)
        val = None if val in (None, "", "null", "None") else str(val).lower()
        if gold is None:
            if val is not None:
                return False  # галлюцинация
        elif val is None or not any(g in val for g in gold):
            return False
    return True


def score(pred, case):
    if not pred:
        return {"topics": False, "help": False, "lang": False, "urgency": False, "volunteer": False, "details": False}
    try:
        top = int(pred["top"]) if pred["top"] is not None else None
    except (TypeError, ValueError):
        top = None
    return {
        "topics": set(case["topics"]) <= set(map(str, pred["topics"])),
        "help": (not case["help"]) or bool(set(case["help"]) & set(map(str, pred["help"]))),
        "lang": norm_lang(pred["lang"]) in case["lang"],
        "urgency": pred["urgency"] in case["urgency"],
        "volunteer": top in case["volunteers"],
        "details": details_ok(pred, case),
    }


def main():
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise SystemExit("Нужен GEMINI_API_KEY в .env")
    systems = {"Голая Gemma 4": lambda t: baseline(t),
               "Только правила (без LLM)": lambda t: ryadom(t, use_gemma=False),
               "Рядом: Gemma 4 + наш слой": lambda t: ryadom(t)}
    metrics = ["topics", "help", "lang", "urgency", "volunteer", "details"]
    totals = {s: {m: 0 for m in metrics + ["all"]} for s in systems}
    times = {s: [] for s in systems}
    rows = []
    for case in CASES:
        row = [str(case["id"]), case["transcript"][:60] + "…"]
        for name, fn in systems.items():
            t0 = time.monotonic()
            pred = fn(case["transcript"])
            times[name].append(time.monotonic() - t0)
            sc = score(pred, case)
            for m in metrics:
                totals[name][m] += sc[m]
            ok = all(sc.values())
            totals[name]["all"] += ok
            row.append("✅" if ok else "❌ " + ",".join(m for m in metrics if not sc[m]))
            if "Gemma" in name:
                time.sleep(1)  # бережём лимиты бесплатного тарифа
        rows.append(row)
        print(" | ".join(row))

    n = len(CASES)
    mode = "с размышлениями" if os.getenv("GEMMA_THINKING") == "1" else "без размышлений"
    out = [f"# Eval: {n} обезличенных звонков, модель {gemma.MODEL} ({mode})", "",
           "| Система | Сфера | Тип помощи | Язык | Срочность | Нужный волонтёр | Имя/время/район | **Всё верно** | Ср. время |",
           "|---|---|---|---|---|---|---|---|---|"]
    for name in systems:
        t = totals[name]
        avg = sum(times[name]) / n
        out.append(f"| {name} | " + " | ".join(f"{t[m]}/{n}" for m in metrics) + f" | **{t['all']}/{n}** | {avg:.1f} с |")
    out += ["", "Разметка — eval/calls.json. Случай 11 — попытка prompt injection, случай 8 — две просьбы в одном звонке.",
            "«Имя/время/район»: извлечено верно, а если в звонке этого нет — не выдумано.",
            "Оговорка: ключевые слова правил и тестовые звонки писали одни люди, поэтому результат правил завышен.",
            "", "| # | Звонок | " + " | ".join(systems) + " |", "|---|---|" + "---|" * len(systems)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    suffix = "_thinking" if os.getenv("GEMMA_THINKING") == "1" else ""
    path = HERE / f"results{suffix}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n" + "\n".join(out[:2 + 2 + len(systems)]))
    print(f"\nСохранено: {path.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
