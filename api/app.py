"""Epea v0.5 - language-learning API (step 9).

Language learning delivered through Muse: the conversation is the interface,
this API owns the learning machinery (curriculum content, exercise
generation, grading, spaced-repetition scheduling, audio).

Step 9 adds grammar-topic drilling: /drill serves tense drills built from
build-time spaCy tags (data/tag_grammar.py). Step 8 closed the profanity
gap. Step 7 added six languages: Italian, German, Portuguese (Brazilian),
Russian, Ukrainian, Dutch. v1 languages: es/fr. Step 6 added: Mexican
Spanish voice (es-mx opt-in), /translate, /words, progress "weakest", and
the placement test. Exercises are built deterministically from the
content DB (step 2), so an exercise ID always resolves to the same
exercise and FSRS cards persist across sessions.

Endpoint map (v0.5):
    GET  /health                  public liveness probe
    GET  /languages               auth, language list (8 active)
    POST /learners                auth, issue opaque learner UUID (no PII)
    GET  /lesson/next            auth, 5 due/new exercises for a learner
    GET  /drill                   auth, up to 10 grammar-topic (tense) drills
    GET  /exercise/{id}          auth, single exercise payload (no answer)
    POST /grade                  auth, grade an answer, update FSRS state
    GET  /progress                auth, learner stats
    DELETE /learners/{id}         auth, erase all learner data
    GET  /audio/{clip}.mp3         auth, pronunciation audio
                                   (es/fr: s-<id>; step-7: <lang>-<id>;
                                    Spanish ?voice=es|es-mx)
    GET  /translate                auth, phrase lookup (9 languages)
    GET  /words                    auth, word lookup + examples (8 L2 langs)
    POST /placement                auth, start placement test
    GET  /placement/result         auth, CEFR suggestion from probes

Pattern follows Trophe/Phos: single API key in one header (X-API-Key),
OpenAPI 3.1 spec, /health is public, everything else 401s without the key.

Run locally (from this directory)::

    EPEA_API_KEY=test-key ../.venv/bin/uvicorn app:app --port 8124
"""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from fsrs import Card, Rating, Scheduler, State

# ---------------------------------------------------------------------------
# Auth: exactly one API key in one header (X-API-Key), constant-time compare.
# ---------------------------------------------------------------------------
_API_KEY = os.environ.get("EPEA_API_KEY", "")
if not _API_KEY:
    raise RuntimeError(
        "EPEA_API_KEY environment variable is required (the API key clients "
        "must send in the X-API-Key header)."
    )

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    key: Annotated[str | None, Depends(api_key_header)],
) -> str:
    """Reject requests without the correct X-API-Key header (HTTP 401)."""
    ok = key is not None and hmac.compare_digest(key, _API_KEY)
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Send it in the X-API-Key header.",
        )
    return key  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Content DB + learner tables
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "epea.db"

# Pre-generated pronunciation audio (step 4). Pure static serving: no TTS
# engine exists anywhere in the request path or the runtime venv.
AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"
_CLIP_RE = re.compile(r"^(s|it|de|pt|ru|uk|nl)-\d+$")
# Clip-id prefix -> audio subdirectory. es/fr keep the legacy "s-" form
# (same ids serve both Spain and Mexican voices via ?voice=); step-7
# languages use their own code as the prefix.
_CLIP_DIR = {
    "s": ("es", "fr"),  # legacy: search es then fr dirs (unchanged behavior)
    "it": ("it",), "de": ("de",), "pt": ("pt",),
    "ru": ("ru",), "uk": ("uk",), "nl": ("nl",),
}


def _clip_id(api_lang: str, sid: int) -> str:
    """Deterministic clip id for a sentence id, matching build_audio.py."""
    if api_lang in ("es", "fr"):
        return f"s-{sid}"
    return f"{api_lang}-{sid}"


def _audio_clip_count() -> int:
    if not AUDIO_DIR.exists():
        return 0
    return sum(1 for _ in AUDIO_DIR.rglob("*.mp3"))


_AUDIO_CLIPS = _audio_clip_count()

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db_lock = threading.Lock()

_db.executescript(
    """
CREATE TABLE IF NOT EXISTS learners(
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards(
    learner_id TEXT NOT NULL,
    exercise_key TEXT NOT NULL,
    state INTEGER NOT NULL,
    step INTEGER,
    stability REAL,
    difficulty REAL,
    due TEXT NOT NULL,
    last_review TEXT,
    PRIMARY KEY (learner_id, exercise_key)
);
CREATE TABLE IF NOT EXISTS attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id TEXT NOT NULL,
    exercise_id TEXT NOT NULL,
    type TEXT NOT NULL,
    correct INTEGER NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(learner_id, due);
CREATE INDEX IF NOT EXISTS idx_attempts_learner ON attempts(learner_id);
CREATE TABLE IF NOT EXISTS placement_probes(
    learner_id TEXT NOT NULL,
    exercise_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (learner_id, exercise_id)
);
"""
)
_db.commit()


def _q(sql: str, params: tuple = ()):
    with _db_lock:
        return _db.execute(sql, params).fetchall()


def _w(sql: str, params: tuple = ()):
    with _db_lock:
        _db.execute(sql, params)
        _db.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


_scheduler = Scheduler()

# Top-5000 frequency words per L2 language, loaded once (for cloze blanks).
_VOCAB: dict[str, dict[str, int]] = {}
with _db_lock:
    for _api_lang, _db_lang in (
        ("es", "spa"), ("fr", "fra"),
        ("it", "ita"), ("de", "deu"), ("pt", "por"),
        ("ru", "rus"), ("uk", "ukr"), ("nl", "nld"),
    ):
        _VOCAB[_api_lang] = {
            r[0]: r[1]
            for r in _db.execute(
                "SELECT word, rank FROM words WHERE lang=? AND rank<=5000",
                (_db_lang,),
            )
        }

# API-facing L2 language codes (per the plan); es-mx is a voice variant of es,
# not a separate curriculum language.
L2_LANGS = ("es", "fr", "it", "de", "pt", "ru", "uk", "nl")

SHORT_TO_TYPE = {"mc": "multiple_choice", "tr": "translate", "cl": "cloze"}
# API-facing codes are es/fr/it/de/pt/ru/uk/nl/en (per the plan); the content
# DB uses Tatoeba ISO 639-2 codes. Map once, use DB codes in every query.
DB_LANG = {
    "es": "spa", "fr": "fra", "it": "ita", "de": "deu",
    "pt": "por", "ru": "rus", "uk": "ukr", "nl": "nld",
}
DB_LANG_ALL = {"en": "eng", **DB_LANG}


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
_WS = re.compile(r"\s+")
_EDGE_PUNCT = ".,!?;:\"'()[]{}«»¿¡…-–"
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
_EX_RE = re.compile(r"^(mc|tr|cl)-(es|fr|it|de|pt|ru|uk|nl)-(\d+)$")


def normalize(text: str) -> str:
    """Lenient comparison form: lowercase, single spaces, edge punctuation
    stripped. Accents are KEPT: es/fr require them (decision logged in
    BUILD_LOG.md, step 3)."""
    t = _WS.sub(" ", text.strip().lower())
    return t.strip(_EDGE_PUNCT).strip()


# ---------------------------------------------------------------------------
# Exercise builders (deterministic: same inputs -> same exercise)
# ---------------------------------------------------------------------------
def get_sentence(sid: int):
    rows = _q("SELECT id, lang, text FROM sentences WHERE id=?", (sid,))
    return rows[0] if rows else None


def translations_of(sid: int, target_lang: str):
    # CROSS JOIN pins the join order (links by PK first): without it the
    # planner scans every sentence in target_lang per call.
    return _q(
        """SELECT s.id, s.text FROM links l
           CROSS JOIN sentences s ON s.id = l.translation_id
           WHERE l.sentence_id = ? AND s.lang = ?
           ORDER BY s.id""",
        (sid, target_lang),
    )


def build_mc(lang: str, sid: int):
    """Multiple choice: English prompt, 4 target-language choices."""
    dlang = DB_LANG[lang]
    s = get_sentence(sid)
    if not s or s["lang"] != dlang:
        return None
    engs = translations_of(sid, "eng")
    if not engs:
        return None
    prompt_row = engs[0]
    correct = s["text"]
    band = max(12, int(len(correct) * 0.35))
    exclude = {sid} | {r["id"] for r in translations_of(prompt_row["id"], dlang)}
    pool = _q(
        """SELECT id, text FROM sentences
           WHERE lang=? AND id != ? AND abs(length(text)-?) <= ?
           ORDER BY id LIMIT 400""",
        (dlang, sid, len(correct), band),
    )
    cands = [r for r in pool if r["id"] not in exclude]
    if len(cands) < 3:
        return None
    rng = random.Random(
        int(hashlib.sha256(f"mc-{lang}-{sid}".encode()).hexdigest(), 16)
    )
    distractors = rng.sample(cands, 3)
    ordered = [correct] + [d["text"] for d in distractors]
    idx = list(range(4))
    rng.shuffle(idx)
    choices = [ordered[i] for i in idx]
    correct_index = idx.index(0)
    return {
        "exercise_id": f"mc-{lang}-{sid}",
        "type": "multiple_choice",
        "prompt": prompt_row["text"],
        "choices": choices,
        "lang": lang,
        "_correct_index": correct_index,
        "_expected_display": correct,
    }


def build_translate(lang: str, sid: int):
    """Translate: English prompt, learner types the target language.
    Any linked translation in the DB is accepted."""
    dlang = DB_LANG[lang]
    s = get_sentence(sid)
    if not s or s["lang"] != "eng":
        return None
    targets = translations_of(sid, dlang)
    if not targets:
        return None
    expected = [t["text"] for t in targets]
    return {
        "exercise_id": f"tr-{lang}-{sid}",
        "type": "translate",
        "prompt": s["text"],
        "lang": lang,
        "_expected": [normalize(t) for t in expected],
        "_expected_display": expected[0],
    }


def build_cloze(lang: str, sid: int):
    """Cloze: target-language sentence with its most frequent top-5000
    word blanked."""
    s = get_sentence(sid)
    if not s or s["lang"] != DB_LANG[lang]:
        return None
    vocab = _VOCAB[lang]
    best = None
    best_rank = None
    for tok in _WORD_RE.findall(s["text"]):
        w = tok.lower().strip("'’")
        rank = vocab.get(w)
        if rank is not None and (best_rank is None or rank < best_rank):
            best, best_rank = tok, rank
    if best is None:
        return None
    pattern = re.compile(
        r"(?<![\w'’])" + re.escape(best) + r"(?![\w'’])", re.IGNORECASE
    )
    prompt, n = pattern.subn("____", s["text"], count=1)
    if n == 0:
        return None
    return {
        "exercise_id": f"cl-{lang}-{sid}",
        "type": "cloze",
        "prompt": prompt,
        "lang": lang,
        "_expected": [normalize(best)],
        "_expected_display": best,
    }


def build_exercise(exercise_id: str):
    m = _EX_RE.match(exercise_id or "")
    if not m:
        return None
    short, lang, sid = m.group(1), m.group(2), int(m.group(3))
    if short == "mc":
        return build_mc(lang, sid)
    if short == "tr":
        return build_translate(lang, sid)
    return build_cloze(lang, sid)


def public(ex: dict) -> dict:
    """Exercise payload safe to send to the learner: never the answer."""
    return {k: v for k, v in ex.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Learner + FSRS state
# ---------------------------------------------------------------------------
def get_learner(lid: str):
    rows = _q("SELECT id FROM learners WHERE id=?", (lid,))
    return rows[0] if rows else None


def load_card(lid: str, key: str) -> Card:
    rows = _q(
        "SELECT state, step, stability, difficulty, due, last_review"
        " FROM cards WHERE learner_id=? AND exercise_key=?",
        (lid, key),
    )
    if not rows:
        return Card()
    r = rows[0]
    return Card(
        state=State(r["state"]),
        step=r["step"],
        stability=r["stability"],
        difficulty=r["difficulty"],
        due=datetime.fromisoformat(r["due"]),
        last_review=(
            datetime.fromisoformat(r["last_review"]) if r["last_review"] else None
        ),
    )


def save_card(lid: str, key: str, card: Card) -> None:
    _w(
        """INSERT INTO cards
           (learner_id, exercise_key, state, step, stability, difficulty,
            due, last_review)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(learner_id, exercise_key) DO UPDATE SET
             state=excluded.state, step=excluded.step,
             stability=excluded.stability, difficulty=excluded.difficulty,
             due=excluded.due, last_review=excluded.last_review""",
        (
            lid,
            key,
            card.state.value,
            card.step,
            card.stability,
            card.difficulty,
            card.due.isoformat(),
            card.last_review.isoformat() if card.last_review else None,
        ),
    )


def _has_translation(sid: int, target_db_lang: str) -> bool:
    """True if sentence sid has at least one translation in target_db_lang.
    CROSS JOIN pins the join order (links by PK first, then sentences by
    PK): without it SQLite scans all 2M English sentences per check."""
    rows = _q(
        """SELECT 1 FROM links l CROSS JOIN sentences e ON e.id = l.translation_id
           WHERE l.sentence_id = ? AND e.lang = ? LIMIT 1""",
        (sid, target_db_lang),
    )
    return bool(rows)


def _walk_pool(dlang: str, target_db_lang: str, lo: int, hi: int,
               want: int) -> list[int]:
    """Easiest `want` sentence ids in language dlang (length lo..hi) that
    have a translation in target_db_lang. Walks length-ordered candidates
    in chunks and tests each with _has_translation; stops as soon as
    `want` are found, so the common case touches only a few hundred rows
    and never materializes the full candidate list."""
    out: list[int] = []
    offset = 0
    chunk = 2000
    while len(out) < want:
        rows = _q(
            """SELECT id FROM sentences WHERE lang=? AND length(text) BETWEEN ? AND ?
               ORDER BY length(text), id LIMIT ? OFFSET ?""",
            (dlang, lo, hi, chunk, offset),
        )
        if not rows:
            break
        offset += chunk
        for r in rows:
            if _has_translation(r["id"], target_db_lang):
                out.append(r["id"])
                if len(out) >= want:
                    break
    return out


def _compute_candidates() -> dict[str, dict[str, list[int]]]:
    """Easiest exercise candidates per language and type, computed once at
    startup. The content DB is static, so the process-lifetime cache is
    always correct. Uses walk-and-test (indexed PK lookups) instead of
    full-table joins: the 7M-row links table makes the join form take
    minutes per language at 8-language scale."""
    cands: dict[str, dict[str, list[int]]] = {}
    for api_lang, dlang in (
        ("es", "spa"), ("fr", "fra"),
        ("it", "ita"), ("de", "deu"), ("pt", "por"),
        ("ru", "rus"), ("uk", "ukr"), ("nl", "nld"),
    ):
        mc = _walk_pool(dlang, "eng", 8, 140, 500)
        tr = _walk_pool("eng", dlang, 8, 140, 500)
        cl_rows = _q(
            """SELECT id, text FROM sentences
               WHERE lang=? AND length(text) BETWEEN 12 AND 140
               ORDER BY length(text), id LIMIT 1000""",
            (dlang,),
        )
        vocab = _VOCAB[api_lang]
        cl = []
        for r in cl_rows:
            if any(
                t.lower().strip("'’") in vocab
                for t in _WORD_RE.findall(r["text"])
            ):
                cl.append(r["id"])
            if len(cl) >= 500:
                break
        cands[api_lang] = {"mc": mc, "tr": tr, "cl": cl}
    return cands


_CANDS = _compute_candidates()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Epea",
    version="0.5.0",
    description=(
        "Language learning for Muse. Curriculum content, exercise generation, "
        "grading, and spaced-repetition scheduling served over a simple API; "
        "the conversation is the interface. Eight languages: Spanish, French, "
        "Italian, German, Brazilian Portuguese, Russian, Ukrainian, Dutch."
    ),
    openapi_version="3.1.0",
)


@app.get(
    "/health",
    summary="Liveness probe (no auth)",
    description="Service liveness. No API key required.",
)
def health():
    return {
        "status": "ok",
        "version": "0.5.0",
        "audio_clips": _AUDIO_CLIPS,
        "languages": [
            "Spanish",
            "French",
            "Italian",
            "German",
            "Brazilian Portuguese",
            "Russian",
            "Ukrainian",
            "Dutch",
        ],
    }


@app.get(
    "/languages",
    dependencies=[Depends(require_api_key)],
    summary="Supported languages",
    description="Languages served by the v1 curriculum.",
)
def languages():
    return {
        "languages": [
            {
                "code": "es",
                "name": "Spanish",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types. Mexican Spanish voice via ?voice=es-mx on /audio.",
            },
            {
                "code": "fr",
                "name": "French",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
            {
                "code": "it",
                "name": "Italian",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
            {
                "code": "de",
                "name": "German",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
            {
                "code": "pt",
                "name": "Portuguese (Brazilian)",
                "status": "active",
                "note": "Brazilian Portuguese (pt_BR voice). Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
            {
                "code": "ru",
                "name": "Russian",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
            {
                "code": "uk",
                "name": "Ukrainian",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
            {
                "code": "nl",
                "name": "Dutch",
                "status": "active",
                "note": "Tatoeba sentence bank, FSRS scheduling, 3 exercise types.",
            },
        ]
    }


class GradeBody(BaseModel):
    learner_id: str
    exercise_id: str
    answer: str


@app.post(
    "/learners",
    dependencies=[Depends(require_api_key)],
    summary="Create a learner",
    description=(
        "Issues an opaque learner UUID. No name, no email, no PII of any kind. "
        "Muse stores the ID and passes it with every call."
    ),
)
def create_learner():
    lid = str(uuid.uuid4())
    _w("INSERT INTO learners(id, created_at) VALUES(?,?)",
       (lid, _now().isoformat()))
    return {"learner_id": lid}


@app.get(
    "/lesson/next",
    dependencies=[Depends(require_api_key)],
    summary="Next lesson for a learner",
    description=(
        "Returns up to 5 exercises: FSRS-due reviews first, then new items "
        "(easiest unseen sentences/words). Answers are never included."
    ),
)
def lesson_next(
    learner_id: str = Query(...),
    lang: str = Query(...),
):
    if lang not in L2_LANGS:
        raise HTTPException(
            status_code=400,
            detail="lang must be one of: es, fr, it, de, pt, ru, uk, nl.",
        )
    if not get_learner(learner_id):
        raise HTTPException(
            status_code=404,
            detail="Unknown learner_id. Create one with POST /learners.",
        )
    now_iso = _now().isoformat()
    items: list[dict] = []
    due_rows = _q(
        "SELECT exercise_key FROM cards WHERE learner_id=? AND due<=?"
        " ORDER BY due LIMIT 5",
        (learner_id, now_iso),
    )
    for r in due_rows:
        ex = build_exercise(r["exercise_key"])
        if ex:
            items.append(public(ex))
    if len(items) < 5:
        seen = {
            r["exercise_key"]
            for r in _q(
                "SELECT exercise_key FROM cards WHERE learner_id=?",
                (learner_id,),
            )
        }
        pools = [
            ("mc", _CANDS[lang]["mc"]),
            ("tr", _CANDS[lang]["tr"]),
            ("cl", _CANDS[lang]["cl"]),
        ]
        pos = [0, 0, 0]
        for pi in (0, 1, 2) * 6:
            if len(items) >= 5:
                break
            short, cands = pools[pi]
            while pos[pi] < len(cands):
                sid = cands[pos[pi]]
                pos[pi] += 1
                eid = f"{short}-{lang}-{sid}"
                if eid in seen:
                    continue
                ex = build_exercise(eid)
                if ex is None:
                    continue
                seen.add(eid)
                items.append(public(ex))
                break
    return {"learner_id": learner_id, "lang": lang, "exercises": items[:5]}


@app.get(
    "/exercise/{exercise_id}",
    dependencies=[Depends(require_api_key)],
    summary="Single exercise",
    description="Full exercise payload. The answer is never included.",
)
def get_exercise(exercise_id: str):
    ex = build_exercise(exercise_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Unknown exercise_id.")
    return public(ex)


@app.get(
    "/audio/{clip_id}.mp3",
    dependencies=[Depends(require_api_key)],
    summary="Pronunciation audio clip",
    description=(
        "Pre-generated Piper TTS audio for a lesson sentence, served as a "
        "static MP3. clip_id is deterministic: s-<sentence_id> for Spanish "
        "and French, <lang>-<sentence_id> for Italian, German, Portuguese, "
        "Russian, Ukrainian, Dutch. Optional ?voice=es (default, Spain "
        "Spanish) or ?voice=es-mx (Mexican Spanish, opt-in) applies to "
        "Spanish clips only. No TTS runs in the request path."
    ),
    responses={
        400: {"description": "Unknown voice or voice on a non-Spanish clip."},
        404: {"description": "Unknown clip_id."},
    },
)
def get_audio(clip_id: str, voice: str | None = Query(None)):
    m = _CLIP_RE.fullmatch(clip_id)
    if not m:
        raise HTTPException(status_code=404, detail="Unknown clip_id.")
    prefix = m.group(1)
    if prefix == "s":
        if voice is None:
            dirs = _CLIP_DIR["s"]
        else:
            v = voice.strip().lower()
            if v not in ("es", "es-mx"):
                raise HTTPException(
                    status_code=400,
                    detail="voice must be 'es' or 'es-mx'.",
                )
            dirs = ("es-mx" if v == "es-mx" else "es",)
    else:
        if voice is not None:
            raise HTTPException(
                status_code=400,
                detail="voice is only available for Spanish clips.",
            )
        dirs = _CLIP_DIR[prefix]
    for lang in dirs:
        clip = AUDIO_DIR / lang / f"{clip_id}.mp3"
        if clip.is_file():
            return FileResponse(
                clip, media_type="audio/mpeg", filename=f"{clip_id}.mp3"
            )
    raise HTTPException(status_code=404, detail="Unknown clip_id.")


@app.get(
    "/translate",
    dependencies=[Depends(require_api_key)],
    summary="Phrase lookup",
    description=(
        "Look up a phrase in one language and get its translations in "
        "another. Tries an exact match first, then a substring search "
        "(up to 10 matches). Each translation carries its audio clip id."
    ),
)
def translate(
    text: str = Query(...),
    from_lang: str = Query("en", alias="from"),
    to: str = Query("es"),
):
    f, t = from_lang.strip().lower(), to.strip().lower()
    if f not in DB_LANG_ALL or t not in DB_LANG_ALL or f == t:
        raise HTTPException(
            status_code=400,
            detail="from/to must differ and each be one of: "
            "en, es, fr, it, de, pt, ru, uk, nl.",
        )
    dfrom, dto = DB_LANG_ALL[f], DB_LANG_ALL[t]
    q = text.strip()
    if not q:
        raise HTTPException(status_code=400, detail="text must not be empty.")
    seen_ids: set[int] = set()
    ordered: list[sqlite3.Row] = []
    for r in _q(
        "SELECT id, text FROM sentences WHERE lang=? AND text=? LIMIT 10",
        (dfrom, q),
    ):
        seen_ids.add(r["id"])
        ordered.append(r)
    if len(ordered) < 10:
        esc = _like_escape(q)
        for r in _q(
            "SELECT id, text FROM sentences WHERE lang=? AND text LIKE ?"
            " ESCAPE '\\' LIMIT 40",
            (dfrom, f"%{esc}%"),
        ):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                ordered.append(r)
            if len(ordered) >= 10:
                break
    matches = []
    for r in ordered:
        trans = []
        for tr in translations_of(r["id"], dto):
            entry = {"text": tr["text"]}
            if t != "en":  # English has no pre-generated audio
                entry["audio"] = _clip_id(t, tr["id"]) + ".mp3"
            trans.append(entry)
        matches.append({"text": r["text"], "translations": trans})
    return {"matches": matches}


_WORD_BOUNDARY_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _word_in_text(word: str, text: str) -> bool:
    pat = _WORD_BOUNDARY_RE_CACHE.get(word)
    if pat is None:
        pat = re.compile(
            r"(?<![\w'’])" + re.escape(word) + r"(?![\w'’])", re.IGNORECASE
        )
        _WORD_BOUNDARY_RE_CACHE[word] = pat
    return pat.search(text) is not None


@app.get(
    "/words",
    dependencies=[Depends(require_api_key)],
    summary="Word lookup",
    description=(
        "Look up a word: CEFR level and frequency rank, plus up to 3 "
        "example sentences, each with one English translation."
    ),
)
def words(lang: str = Query(...), q: str = Query(...)):
    lang = lang.strip().lower()
    if lang not in L2_LANGS:
        raise HTTPException(
            status_code=400,
            detail="lang must be one of: es, fr, it, de, pt, ru, uk, nl.",
        )
    dlang = DB_LANG[lang]
    term = q.strip().lower()
    if not term:
        raise HTTPException(status_code=400, detail="q must not be empty.")
    esc = _like_escape(term)
    rows = _q(
        "SELECT word, cefr, rank FROM words WHERE lang=?"
        " AND (word=? OR word LIKE ? ESCAPE '\\')"
        " ORDER BY rank LIMIT 10",
        (dlang, term, f"{esc}%"),
    )
    out = []
    for r in rows:
        examples = []
        cands = _q(
            "SELECT id, text FROM sentences WHERE lang=?"
            " AND length(text) <= 200 AND text LIKE ? ESCAPE '\\' LIMIT 60",
            (dlang, f"%{esc}%"),
        )
        for c in cands:
            if not _word_in_text(r["word"], c["text"]):
                continue
            engs = translations_of(c["id"], "eng")
            examples.append(
                {
                    "text": c["text"],
                    "translation": engs[0]["text"] if engs else None,
                    "audio": _clip_id(lang, c["id"]) + ".mp3",
                }
            )
            if len(examples) >= 3:
                break
        out.append(
            {
                "word": r["word"],
                "cefr": r["cefr"],
                "rank": r["rank"],
                "examples": examples,
            }
        )
    return {"words": out}


@app.post(
    "/grade",
    dependencies=[Depends(require_api_key)],
    summary="Grade an answer",
    description=(
        "Layered grading: normalization (case, punctuation) then an accept "
        "list of known-good answers. Accents and diacritics are required in "
        "all languages (si vs si with accent, German umlauts, Portuguese "
        "tildes). Updates FSRS state and records the attempt."
    ),
)
def grade(body: GradeBody):
    if not get_learner(body.learner_id):
        raise HTTPException(
            status_code=404,
            detail="Unknown learner_id. Create one with POST /learners.",
        )
    ex = build_exercise(body.exercise_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Unknown exercise_id.")
    etype = ex["type"]
    correct = False
    if etype == "multiple_choice":
        a = body.answer.strip()
        idx = None
        if re.fullmatch(r"[0-3]", a):
            idx = int(a)
        elif re.fullmatch(r"[A-Da-d]", a):
            idx = ord(a.upper()) - 65
        else:
            na = normalize(a)
            for i, ch in enumerate(ex["choices"]):
                if normalize(ch) == na:
                    idx = i
                    break
        correct = idx == ex["_correct_index"]
    else:
        correct = normalize(body.answer) in ex["_expected"]
    expected_display = ex["_expected_display"]
    feedback = (
        "Correct."
        if correct
        else f"Not quite. The expected answer is: {expected_display}"
    )
    card = load_card(body.learner_id, body.exercise_id)
    new_card, _log = _scheduler.review_card(
        card, Rating.Good if correct else Rating.Again
    )
    if not correct:
        new_card.due = _now()  # missed items come back immediately
    save_card(body.learner_id, body.exercise_id, new_card)
    _w(
        "INSERT INTO attempts(learner_id, exercise_id, type, correct, at)"
        " VALUES(?,?,?,?,?)",
        (
            body.learner_id,
            body.exercise_id,
            etype,
            1 if correct else 0,
            _now().isoformat(),
        ),
    )
    return {"correct": correct, "feedback": feedback, "expected": expected_display}


@app.get(
    "/progress",
    dependencies=[Depends(require_api_key)],
    summary="Learner progress",
    description="Attempts, accuracy, due reviews, and per-type breakdown.",
)
def progress(learner_id: str = Query(...)):
    if not get_learner(learner_id):
        raise HTTPException(
            status_code=404,
            detail="Unknown learner_id. Create one with POST /learners.",
        )
    done, corr = _q(
        "SELECT COUNT(*), COALESCE(SUM(correct),0) FROM attempts"
        " WHERE learner_id=?",
        (learner_id,),
    )[0]
    due_now = _q(
        "SELECT COUNT(*) FROM cards WHERE learner_id=? AND due<=?",
        (learner_id, _now().isoformat()),
    )[0][0]
    by_type = {}
    for t, c, s in _q(
        "SELECT type, COUNT(*), COALESCE(SUM(correct),0) FROM attempts"
        " WHERE learner_id=? GROUP BY type",
        (learner_id,),
    ):
        by_type[t] = {"done": c, "correct": s}
    weakest = []
    for eid, t, c, s in _q(
        "SELECT exercise_id, type, COUNT(*), COALESCE(SUM(correct),0)"
        " FROM attempts WHERE learner_id=? GROUP BY exercise_id"
        " HAVING COUNT(*) >= 3"
        " ORDER BY (1.0*COALESCE(SUM(correct),0)/COUNT(*)) ASC,"
        " COUNT(*) DESC LIMIT 10",
        (learner_id,),
    ):
        weakest.append(
            {
                "exercise_id": eid,
                "type": t,
                "attempts": c,
                "correct": s,
                "accuracy": round(s / c, 3),
            }
        )
    return {
        "learner_id": learner_id,
        "exercises_done": done,
        "correct": corr,
        "accuracy": round(corr / done, 3) if done else 0.0,
        "due_now": due_now,
        "by_type": by_type,
        "weakest": weakest,
    }


@app.delete(
    "/learners/{learner_id}",
    dependencies=[Depends(require_api_key)],
    summary="Erase a learner",
    description="Wipes the learner record, all FSRS cards, and all attempts.",
)
def delete_learner(learner_id: str):
    if not get_learner(learner_id):
        raise HTTPException(status_code=404, detail="Unknown learner_id.")
    _w("DELETE FROM attempts WHERE learner_id=?", (learner_id,))
    _w("DELETE FROM cards WHERE learner_id=?", (learner_id,))
    _w("DELETE FROM placement_probes WHERE learner_id=?", (learner_id,))
    _w("DELETE FROM learners WHERE id=?", (learner_id,))
    return {"deleted": learner_id}


# ---------------------------------------------------------------------------
# Placement test: fixed probe set spanning easy -> hard, graded through
# the normal /grade path. CEFR suggestion from probe accuracy.
# ---------------------------------------------------------------------------
def _probe_set(lang: str) -> list[str]:
    """Deterministic 10-probe set: 4 easy MC, 3 medium (2 tr + 1 cl),
    3 hard (1 tr + 2 cl). Pools are length-ordered, so pool position is
    the difficulty knob. Walks each pool until enough valid exercises
    build (some ids fail to build; the walk is still deterministic)."""
    pools = _CANDS[lang]
    plan = [
        ("mc", 0, 4),
        ("tr", 200, 2),
        ("cl", 200, 1),
        ("tr", 450, 1),
        ("cl", 450, 2),
    ]
    picked: list[str] = []
    for short, start, want in plan:
        cands = pools[short]
        i, got = start, 0
        while got < want and i < len(cands):
            eid = f"{short}-{lang}-{cands[i]}"
            i += 1
            if build_exercise(eid) is None or eid in picked:
                continue
            picked.append(eid)
            got += 1
    return picked


# ---------------------------------------------------------------------------
# Grammar drills (step 9): GET /drill?learner_id=&lang=&topic=
# ---------------------------------------------------------------------------

DRILL_TOPICS = ("past-tense", "present-tense", "future-tense")
# A language/topic pair with fewer tagged sentences than this is not served.
DRILL_MIN_SENTENCES = 50


def _drill_tagged_count(dlang: str, topic: str) -> int:
    """Tagged-sentence count for a language/topic pair (0 if the tagger
    has not been run yet)."""
    try:
        rows = _q(
            """SELECT COUNT(*) AS c FROM sentence_topics st
               JOIN sentences s ON s.id = st.sentence_id
               WHERE s.lang = ? AND st.topic = ?""",
            (dlang, topic),
        )
    except sqlite3.OperationalError:
        return 0
    return rows[0]["c"] if rows else 0


def _drill_tagged_ids(dlang: str, topic: str, cap: int = 400) -> list[int]:
    """Tagged sentence ids for a language/topic pair, easiest first."""
    try:
        rows = _q(
            """SELECT s.id AS id FROM sentence_topics st
               JOIN sentences s ON s.id = st.sentence_id
               WHERE s.lang = ? AND st.topic = ?
               ORDER BY length(s.text), s.id LIMIT ?""",
            (dlang, topic, cap),
        )
    except sqlite3.OperationalError:
        return []
    return [r["id"] for r in rows]


def _drill_tr_candidates(dlang: str, tagged: set[int], want: int) -> list[int]:
    """English sentence ids whose every dlang translation carries the topic
    tag. Grading accepts any linked translation, so a translation exercise
    is only tense-clean when all of its translations are tagged."""
    out: list[int] = []
    for eng_sid in _walk_pool("eng", dlang, 8, 140, 150):
        if len(out) >= want:
            break
        trs = translations_of(eng_sid, dlang)
        if trs and all(t["id"] in tagged for t in trs):
            out.append(eng_sid)
    return out


@app.get(
    "/drill",
    dependencies=[Depends(require_api_key)],
    summary="Grammar-topic drill (verb tense)",
    description=(
        "Up to 10 exercises built only from sentences tagged with the "
        "requested grammar topic (past-tense, present-tense, future-tense) "
        "by the build-time taggers (data/tag_grammar.py: spaCy, and "
        "Stanza for Italian). Exercise IDs "
        "are the standard deterministic IDs, so grading, FSRS cards, and "
        "/progress work exactly as in lessons. Language/topic pairs with "
        "fewer than 50 tagged sentences are not served (HTTP 400)."
    ),
)
def drill(
    learner_id: str = Query(...),
    lang: str = Query(...),
    topic: str = Query(...),
):
    if lang not in L2_LANGS:
        raise HTTPException(
            status_code=400,
            detail="lang must be one of: es, fr, it, de, pt, ru, uk, nl.",
        )
    if topic not in DRILL_TOPICS:
        raise HTTPException(
            status_code=400,
            detail="topic must be one of: "
            + ", ".join(DRILL_TOPICS)
            + ".",
        )
    if not get_learner(learner_id):
        raise HTTPException(
            status_code=404,
            detail="Unknown learner_id. Create one with POST /learners.",
        )
    dlang = DB_LANG[lang]
    available = _drill_tagged_count(dlang, topic)
    if available < DRILL_MIN_SENTENCES:
        raise HTTPException(
            status_code=400,
            detail=f"Topic '{topic}' is not available for '{lang}' yet "
            f"({available} tagged sentences, need {DRILL_MIN_SENTENCES}).",
        )
    tagged_ids = _drill_tagged_ids(dlang, topic)
    tagged = set(tagged_ids)
    tr_candidates = _drill_tr_candidates(dlang, tagged, 10)

    exercises: list[dict] = []
    mc_i = tr_i = cl_i = 0
    turn = 0
    kinds = ("mc", "tr", "cl")
    while len(exercises) < 10 and (
        mc_i < len(tagged_ids) or tr_i < len(tr_candidates)
        or cl_i < len(tagged_ids)
    ):
        kind = kinds[turn % 3]
        turn += 1
        if kind == "mc" and mc_i < len(tagged_ids):
            eid = f"mc-{lang}-{tagged_ids[mc_i]}"
            mc_i += 1
        elif kind == "tr" and tr_i < len(tr_candidates):
            eid = f"tr-{lang}-{tr_candidates[tr_i]}"
            tr_i += 1
        elif kind == "cl" and cl_i < len(tagged_ids):
            eid = f"cl-{lang}-{tagged_ids[cl_i]}"
            cl_i += 1
        else:
            continue
        ex = build_exercise(eid)
        if ex is None:
            # pool sentence lost its translation link; skip it
            continue
        exercises.append(public(ex))
    return {
        "learner_id": learner_id,
        "lang": lang,
        "topic": topic,
        "exercises": exercises,
    }


_CEFR_BANDS = [(0.9, "B2"), (0.7, "B1"), (0.5, "A2"), (0.3, "A1")]


def _cefr_for_accuracy(acc: float) -> str:
    for bar, level in _CEFR_BANDS:
        if acc >= bar:
            return level
    return "A0"


class PlacementBody(BaseModel):
    lang: str


@app.post(
    "/placement",
    dependencies=[Depends(require_api_key)],
    summary="Start a placement test",
    description=(
        "Creates a learner and returns a fixed 10-probe set spanning "
        "easy to hard. Answers go through POST /grade; the result is read "
        "from GET /placement/result."
    ),
)
def start_placement(body: PlacementBody):
    lang = body.lang.strip().lower()
    if lang not in L2_LANGS:
        raise HTTPException(
            status_code=400,
            detail="lang must be one of: es, fr, it, de, pt, ru, uk, nl.",
        )
    probes = _probe_set(lang)
    if len(probes) < 10:
        raise HTTPException(
            status_code=500,
            detail="Could not build a full probe set for this language.",
        )
    lid = str(uuid.uuid4())
    _w("INSERT INTO learners(id, created_at) VALUES(?,?)",
       (lid, _now().isoformat()))
    for pos, eid in enumerate(probes):
        _w(
            "INSERT INTO placement_probes(learner_id, exercise_id, position)"
            " VALUES(?,?,?)",
            (lid, eid, pos),
        )
    items = []
    for eid in probes:
        ex = build_exercise(eid)
        items.append(public(ex))
    return {"learner_id": lid, "lang": lang, "probes": items}


@app.get(
    "/placement/result",
    dependencies=[Depends(require_api_key)],
    summary="Placement test result",
    description=(
        "CEFR suggestion from probe accuracy. Thresholds: 90%+ B2, 70%+ B1, "
        "50%+ A2, 30%+ A1, otherwise A0. Accuracy is computed over answered "
        "probes; unanswered probes do not count for or against."
    ),
)
def placement_result(learner_id: str = Query(...)):
    rows = _q(
        "SELECT exercise_id FROM placement_probes WHERE learner_id=?"
        " ORDER BY position",
        (learner_id,),
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No placement test for this learner_id.",
        )
    correct = 0
    answered = 0
    for r in rows:
        att = _q(
            "SELECT correct FROM attempts WHERE learner_id=? AND exercise_id=?"
            " ORDER BY id DESC LIMIT 1",
            (learner_id, r["exercise_id"]),
        )
        if att:
            answered += 1
            correct += att[0]["correct"]
    acc = correct / answered if answered else 0.0
    return {
        "learner_id": learner_id,
        "suggested_cefr": _cefr_for_accuracy(acc),
        "correct": correct,
        "answered": answered,
        "total": len(rows),
        "accuracy": round(acc, 3),
    }


# ---------------------------------------------------------------------------
# Public pages: privacy policy and terms of service (no auth required).
# ---------------------------------------------------------------------------
_PAGE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
line-height:1.65;color:#1a1a1a;background:#ffffff;max-width:46rem;margin:0 auto;padding:2rem 1.25rem;}
h1{font-size:2rem;margin-bottom:.25rem;} h2{font-size:1.25rem;margin-top:2rem;}
.tag{color:#555;font-size:1.1rem;margin-top:0;}
code{background:#f4f4f4;padding:.15rem .4rem;border-radius:4px;font-size:.9em;}
a{color:#0b5fff;} .fine{color:#666;font-size:.9rem;margin-top:2.5rem;border-top:1px solid #e2e2e2;padding-top:1rem;}
"""

_PRIVACY_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Epea - Privacy Policy</title>
<style>""" + _PAGE_CSS + """</style></head><body>
<h1>Epea - Privacy Policy</h1>
<p class="fine" style="border:none;margin-top:0;padding-top:0">Effective 2026-09-25. Version 1.0.</p>
<p>Epea is a free language-learning API. This policy explains what information
Epea handles when you use the API, and what it does not handle. Unlike a
read-only reference API, Epea is write-capable: it records your exercise
attempts so it can schedule your reviews. If you are the operator deploying
this API, you are responsible for telling your API users what extra logging
your own deployment performs (operator contact details are in the Contact
section below).</p>
<h2>What Epea does</h2>
<p>Epea serves language-learning content (exercises, sentences, listening
audio) and keeps learning progress. There are no accounts: when a learner
starts, the API issues an <strong>opaque learner ID</strong> (a random UUID)
via <code>POST /learners</code>. That ID is the only handle Epea has on a
learner. Exercise attempts and spaced-repetition scheduling state are stored
server-side, keyed to that opaque ID. No names, email addresses, or other
personal details are collected or stored.</p>
<h2>What is collected</h2>
<p><strong>The API key.</strong> Epea authenticates requests with a single
shared key sent in the <code>X-API-Key</code> header. The key is compared in
constant time against the <code>EPEA_API_KEY</code> environment variable on
the server. Because one key is shared, a request cannot be tied to a specific
person, account, or device. Epea has no way to know who you are from the key
alone.</p>
<p><strong>Learner progress.</strong> For each opaque learner ID, Epea stores:
the learner record (ID and creation time), exercise attempts (exercise ID,
answer given, whether it was correct, timestamp), and spaced-repetition
scheduling state (per-card review history and due dates). This data exists so
the API can pick the right exercises for the next lesson. It contains no
personal information.</p>
<p><strong>Request logs.</strong> The Epea application code itself does no
request logging. The server process that runs Epea (for example, uvicorn)
may write access logs that include the client's IP address and request path.
The operator's intent is that any such logs are kept for no longer than 30
days and then rotated or deleted, and are visible only to the operator for
debugging and abuse prevention. (The hosting platform keeps its own platform
logs under its own policies, outside the operator's control.)</p>
<h2>Full deletion</h2>
<p>All learner data can be wiped at any time with
<code>DELETE /learners/{id}</code>. This erases the learner record, all
exercise attempts, and all spaced-repetition cards for that ID. After a
successful delete, nothing of that learner remains on the server. This is the
erasure path: use it to delete a learner completely.</p>
<h2>What is NOT collected</h2>
<ul>
<li><strong>No accounts or personal details.</strong> There is no
registration, no login, no names, no email addresses, and no passwords. The
opaque learner ID cannot be linked back to a person by Epea.</li>
<li><strong>No learner voice.</strong> All listening audio is pre-generated
synthetic speech made at build time. Version 1 has no speaking exercises:
no microphone audio is recorded, transmitted, or stored.</li>
<li><strong>No tracking cookies.</strong> Epea is an API, not a website, and
sets no cookies.</li>
<li><strong>No advertising.</strong> No ads are served and no data is
collected for advertising.</li>
<li><strong>No payment data.</strong> Epea is free; no payment information is
ever requested or stored.</li>
</ul>
<h2>Data retention</h2>
<p>Learner progress data is kept until it is deleted via
<code>DELETE /learners/{id}</code>. There is no automatic expiry; the
erasure endpoint is the way to remove it. Any operator-kept access logs are
intended to be kept no longer than 30 days and then rotated or deleted, as
described above.</p>
<h2>Security</h2>
<p>The API key travels in the <code>X-API-Key</code> request header. Users of a
hosted Epea instance should confirm with the operator that the API is served
over HTTPS so the key is not transmitted in clear text. The server stores the
expected key in the <code>EPEA_API_KEY</code> environment variable, never in
the codebase or database.</p>
<h2>Children's privacy</h2>
<p>Epea collects no personal information from anyone, including children.
Progress is stored only against opaque random IDs that cannot be linked to a
person. There is no way to submit personal data through the API.</p>
<h2>International users</h2>
<p>Epea stores no personal data, so there is no personal data to transfer,
store, or process across borders. If the operator keeps access logs
containing IP addresses, the operator is responsible for complying with the
applicable rules of the relevant jurisdictions.</p>
<h2>Content licenses and attribution</h2>
<p>Epea's learning content and audio are built from openly licensed sources,
and attribution is given as each license requires. Sentence data: Tatoeba,
licensed <strong>CC BY 2.0 FR</strong> (attribution to Tatoeba contributors).
Spanish voice: sharvard dataset via rhasspy/piper-voices, licensed
<strong>CC BY 3.0</strong>. French voice: SIWIS French Speech Synthesis
Database via rhasspy/piper-voices, licensed <strong>CC BY 4.0</strong>. CEFR
word levels: Words-CEFR-Dataset, MIT. Full attribution texts live in the
API's <code>sources</code> table and in the API documentation. Share-alike
licensed content, where used, is kept in separately marked data tables so it
can never be confused with the API's own code.</p>
<h2>Educational disclaimer</h2>
<p>Epea provides educational reference content only. It is not a guarantee of
fluency, and no educational outcome is promised. Epea is not a substitute for
formal instruction or professional language teaching.</p>
<h2>Changes to this policy</h2>
<p>Material changes to this policy will be posted here with a new effective
date before they take effect.</p>
<h2>Contact</h2>
<p>Operator contact for privacy questions: dexiadigi@gmail.com</p>
</body></html>"""

_TERMS_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Epea - Terms of Service</title>
<style>""" + _PAGE_CSS + """</style></head><body>
<h1>Epea - Terms of Service</h1>
<p class="fine" style="border:none;margin-top:0;padding-top:0">Effective 2026-09-25. Version 1.0.</p>
<p>Epea is a free language-learning API. These terms govern your use of the
API and its content.</p>
<h2>1. The service is free</h2>
<p>Epea is provided free of charge. There are no fees, usage tiers, or
paywalls. If a fee or subscription is introduced in the future, updated terms
will be published and users will be notified before any charge applies.</p>
<h2>2. A write-capable API</h2>
<p>Unlike a read-only reference API, Epea records what you do: exercise
attempts and spaced-repetition scheduling state are stored server-side
against your opaque learner ID. This is how Epea schedules your reviews and
measures your progress. You can wipe everything at any time with
<code>DELETE /learners/{id}</code>; after a successful delete, no learner
data remains. No accounts, names, or email addresses are involved at any
point.</p>
<h2>3. Content licenses</h2>
<p>Epea's content is built from openly licensed sources, and attribution is
given as each license requires. Sentence data: Tatoeba, <strong>CC BY 2.0
FR</strong>. Spanish voice audio: sharvard dataset, <strong>CC BY
3.0</strong>. French voice audio: SIWIS French Speech Synthesis Database,
<strong>CC BY 4.0</strong>. CEFR word levels: Words-CEFR-Dataset, MIT. Full
attribution texts are in the API's sources table and documentation. The
audio is pre-generated synthetic speech; no human voice recordings of
learners are made or stored.</p>
<h2>4. Educational content only</h2>
<p>Epea provides educational reference content only. It is <strong>not a
guarantee of fluency</strong> and no educational outcome is promised. Do not
rely on Epea as a substitute for formal instruction or professional language
teaching. Sentence content comes from community-contributed sources and may
contain errors; report problems to the operator contact below.</p>
<h2>5. Prohibited uses</h2>
<ul>
<li>Do not use the Epea name or branding in a way that implies endorsement
of your product without written permission.</li>
<li>Do not abuse the service: excessive automated requests that degrade
availability for others, attempts to circumvent access controls, creating
large numbers of learner IDs to evade limits, or any use that violates
applicable law.</li>
</ul>
<h2>6. No warranty</h2>
<p>Epea and all its content are provided "as is" without warranty of any
kind, express or implied, including but not limited to warranties of accuracy,
completeness, merchantability, or fitness for a particular purpose. Learning
content is served as compiled from the listed sources; we make no claim that
any sentence, translation, or audio clip is free of errors.</p>
<h2>7. Limitation of liability</h2>
<p>To the maximum extent permitted by law, the operators of Epea are not
liable for any indirect, incidental, special, consequential, or punitive
damages arising from your use of the API, even if advised of the possibility
of such damages.</p>
<h2>8. Changes to these terms</h2>
<p>These terms may be updated. Material changes will be posted here with a new
effective date before they take effect. Continued use of the API after the
effective date constitutes acceptance of the updated terms.</p>
<h2>9. Contact</h2>
<p>Questions about these terms: dexiadigi@gmail.com</p>
</body></html>"""


@app.get(
    "/privacy",
    summary="Privacy policy (no auth)",
    description="Privacy policy. No API key required.",
)
def privacy_policy():
    return HTMLResponse(_PRIVACY_HTML)


@app.get(
    "/terms",
    summary="Terms of service (no auth)",
    description="Terms of service. No API key required.",
)
def terms_of_service():
    return HTMLResponse(_TERMS_HTML)
