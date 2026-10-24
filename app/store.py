"""SQLite: заявки и волонтёры."""
import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ryadom.db"
SEED = ROOT / "data" / "volunteers.json"
AUDIO_DIR = ROOT / "data" / "audio"


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created REAL, transcript TEXT, data TEXT,
            status TEXT DEFAULT 'new', volunteer TEXT);
        CREATE TABLE IF NOT EXISTS tg_users (username TEXT PRIMARY KEY, chat_id INTEGER);
        CREATE TABLE IF NOT EXISTS volunteers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, languages TEXT, districts TEXT, skills TEXT,
            contact TEXT, tg_chat_id INTEGER, created REAL);
    """)
    if c.execute("SELECT COUNT(*) FROM volunteers").fetchone()[0] == 0:
        for v in json.loads(SEED.read_text(encoding="utf-8")):
            _insert_volunteer(c, v)
        c.commit()
    return c


# ---------- волонтёры ----------

def _insert_volunteer(c, v: dict) -> int:
    cur = c.execute(
        "INSERT INTO volunteers (name, languages, districts, skills, contact, created) VALUES (?,?,?,?,?,?)",
        (v["name"], json.dumps(v.get("languages", [])), json.dumps(v.get("districts", []), ensure_ascii=False),
         json.dumps(v.get("skills", []), ensure_ascii=False), v.get("contact"), time.time()))
    return cur.lastrowid


def _vol(row) -> dict:
    return {"id": row["id"], "name": row["name"], "languages": json.loads(row["languages"]),
            "districts": json.loads(row["districts"]), "skills": json.loads(row["skills"]),
            "contact": row["contact"], "tg_chat_id": row["tg_chat_id"]}


def add_volunteer(v: dict) -> int:
    with _conn() as c:
        return _insert_volunteer(c, v)


def list_volunteers() -> List[dict]:
    with _conn() as c:
        return [_vol(r) for r in c.execute("SELECT * FROM volunteers ORDER BY id").fetchall()]


def get_volunteer(vol_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM volunteers WHERE id=?", (vol_id,)).fetchone()
    return _vol(row) if row else None


def set_chat_id(vol_id: int, chat_id: int, username: Optional[str] = None) -> Optional[dict]:
    with _conn() as c:
        c.execute("UPDATE volunteers SET tg_chat_id=? WHERE id=?", (chat_id, vol_id))
        if username:
            c.execute("UPDATE volunteers SET contact=? WHERE id=? AND (contact IS NULL OR contact='')",
                      ("@" + username, vol_id))
    return get_volunteer(vol_id)


# ---------- заявки ----------

def add_request(transcript: str, data: dict) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO requests (created, transcript, data) VALUES (?,?,?)",
                        (time.time(), transcript, json.dumps(data, ensure_ascii=False)))
        return cur.lastrowid


def _req(row) -> dict:
    data = json.loads(row["data"])
    return {"id": row["id"], "created": row["created"], "status": row["status"],
            "volunteer": row["volunteer"], "transcript": row["transcript"],
            "candidates": [], **data,
            "audio": (AUDIO_DIR / f"{row['id']}.mp3").exists()}


def get_request(req_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    return _req(row) if row else None


def all_requests() -> List[dict]:
    order = {"high": 0, "medium": 1, "low": 2}
    with _conn() as c:
        rows = c.execute("SELECT * FROM requests ORDER BY created DESC").fetchall()
    out = [_req(r) for r in rows]
    out.sort(key=lambda x: (x["status"] != "new", order.get(x.get("urgency"), 1)))
    return out


def remember_tg_user(username: str, chat_id: int) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO tg_users (username, chat_id) VALUES (?, ?)", (username.lower(), chat_id))


def chat_by_username(username: str) -> Optional[int]:
    with _conn() as c:
        row = c.execute("SELECT chat_id FROM tg_users WHERE username=?", (username.lower().lstrip("@"),)).fetchone()
    return row["chat_id"] if row else None


def requests_of(chat_id: int) -> List[dict]:
    """Заявки, к которым привязан этот чат просящего."""
    return [r for r in all_requests() if r.get("requester_chat") == chat_id]


def set_field(req_id: int, key: str, value) -> None:
    with _conn() as c:
        row = c.execute("SELECT data FROM requests WHERE id=?", (req_id,)).fetchone()
        if row:
            data = json.loads(row["data"])
            data[key] = value
            c.execute("UPDATE requests SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), req_id))


def find_volunteer(name: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM volunteers WHERE name=? ORDER BY id DESC", (name,)).fetchone()
    return _vol(row) if row else None


def take(req_id: int, volunteer: str) -> Optional[dict]:
    """Закрепляет заявку за волонтёром. None — если заявки нет или её уже взяли."""
    with _conn() as c:
        cur = c.execute("UPDATE requests SET status='taken', volunteer=? WHERE id=? AND status='new'",
                        (volunteer, req_id))
        if cur.rowcount == 0:
            return None
    return get_request(req_id)
