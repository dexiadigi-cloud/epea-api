#!/usr/bin/env python3
"""Epea step 4: build-time pronunciation audio pre-generation.

Synthesizes MP3 clips with Piper TTS for the new-learner lesson pools
(v1: es/fr; step 6: es-mx; step 7: it/de/pt/ru/uk/nl).
Piper is a BUILD-TIME tool only -- it is NOT part of the runtime API
(see api/requirements.txt, which deliberately excludes it). The served
MP3s are program output, not a derivative work of the synthesis engine;
the voice model licenses (CC BY / CC0 / public domain, recorded in the
sources table) are what cover the audio.

Each voice model is loaded ONCE per language; sentences are synthesized
in-process (no per-clip subprocess for TTS). WAV bytes go to ffmpeg
over a pipe for MP3 encoding.

Selection query (per language) -- the easiest L2 sentences eligible for
new-learner lessons. This mirrors the multiple-choice candidate pool in
api/app.py::_compute_candidates (identical eligibility rule, larger LIMIT
so the listening pool covers the first ~2,000 items per language):

    SELECT s.id, s.text FROM sentences s WHERE s.lang = ?
       AND length(s.text) BETWEEN 8 AND 140
       AND s.id IN (SELECT l.sentence_id FROM links l
                    JOIN sentences e ON e.id = l.translation_id
                    WHERE e.lang = 'eng')
    ORDER BY length(s.text), s.id
    LIMIT 2000

clip_id is deterministic from the language and sentence id: es/fr/es-mx
use the legacy `s-<sentence_id>` form; step-7 languages use
`<api_lang>-<sentence_id>` (e.g. it-123, de-456) so clip ids are
self-describing across language dirs.
Output: data/audio/{lang}/<clip_id>.mp3
Re-runnable: clips that already exist are skipped (no dupes, fast rerun).

Voices (one per language, licenses in sources table, share_alike=0):
    es:    es_ES-sharvard-medium    (CC BY 3.0), speaker 0 (consistent voice)
    fr:    fr_FR-siwis-medium       (CC BY 4.0)
    es-mx: es_MX-ald-medium         (Unlicense, public domain), speaker 0
           (step 6: Mexican Spanish opt-in; same sentence pool as es)
    it:    it_IT-serena-medium      (CC BY 4.0)
    de:    de_DE-thorsten-medium    (CC0)
    pt:    pt_BR-cadu-medium        (CC0, Brazilian Portuguese)
    ru:    ru_RU-denis-medium       (CC0)
    uk:    uk_UA-ukrainian_tts-medium (CC0)
    nl:    nl_NL-pim-medium         (CC0)

Usage:
    tools/.venv/bin/python data/build_audio.py [--lang es|fr|es-mx|it|de|pt|ru|uk|nl] [--limit N]
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "epea.db"
AUDIO_DIR = ROOT / "data" / "audio"
VOICES_DIR = ROOT / "tools" / "voices"

DB_LANG = {
    "es": "spa", "fr": "fra", "es-mx": "spa",
    "it": "ita", "de": "deu", "pt": "por",
    "ru": "rus", "uk": "ukr", "nl": "nld",
}

# api_lang -> clip-id prefix. es/fr/es-mx keep the legacy "s-" form;
# step-7 languages use their own code so clip ids are self-describing.
CLIP_PREFIX = {
    "es": "s", "fr": "s", "es-mx": "s",
    "it": "it", "de": "de", "pt": "pt",
    "ru": "ru", "uk": "uk", "nl": "nl",
}

# api_lang -> (voice onnx file, piper speaker id)
VOICES = {
    "es": (VOICES_DIR / "es" / "es_ES-sharvard-medium.onnx", 0),
    "fr": (VOICES_DIR / "fr" / "fr_FR-siwis-medium.onnx", 0),
    # Mexican Spanish (step 6): same es sentence pool, MX voice.
    # es_MX-ald-medium, Unlicense (public domain), speaker 0.
    "es-mx": (VOICES_DIR / "es-mx" / "es_MX-ald-medium.onnx", 0),
    # Step 7 (all licenses verified from MODEL_CARD, 2026-09-25).
    "it": (VOICES_DIR / "it" / "it_IT-serena-medium.onnx", 0),        # CC BY 4.0
    "de": (VOICES_DIR / "de" / "de_DE-thorsten-medium.onnx", 0),      # CC0
    "pt": (VOICES_DIR / "pt" / "pt_BR-cadu-medium.onnx", 0),          # CC0, pt_BR
    "ru": (VOICES_DIR / "ru" / "ru_RU-denis-medium.onnx", 0),         # CC0
    "uk": (VOICES_DIR / "uk" / "uk_UA-ukrainian_tts-medium.onnx", 0), # CC0
    "nl": (VOICES_DIR / "nl" / "nl_NL-pim-medium.onnx", 0),            # CC0
}

def _select_sentence_ids(db, dlang: str, limit: int) -> list[tuple[int, str]]:
    """Easiest `limit` sentence ids+texts in language dlang (length 8..140)
    that have an English translation. Walk-and-test with indexed PK
    lookups: the IN-subquery join form takes minutes on the 7M-row links
    table, and CROSS JOIN pins the join order (links by PK first)."""
    out: list[tuple[int, str]] = []
    offset = 0
    chunk = 2000
    while len(out) < limit:
        rows = db.execute(
            """SELECT id, text FROM sentences WHERE lang=? AND length(text) BETWEEN 8 AND 140
               ORDER BY length(text), id LIMIT ? OFFSET ?""",
            (dlang, chunk, offset),
        ).fetchall()
        if not rows:
            break
        offset += chunk
        for sid, text in rows:
            hit = db.execute(
                """SELECT 1 FROM links l CROSS JOIN sentences e ON e.id = l.translation_id
                   WHERE l.sentence_id = ? AND e.lang = 'eng' LIMIT 1""",
                (sid,),
            ).fetchone()
            if hit:
                out.append((sid, text))
                if len(out) >= limit:
                    break
    return out


def wav_bytes_for(voice, text: str, speaker: int) -> bytes:
    from piper.config import SynthesisConfig

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        voice.synthesize_wav(
            text, w, syn_config=SynthesisConfig(speaker_id=speaker)
        )
    return buf.getvalue()


def wav_to_mp3(wav_bytes: bytes, mp3_path: Path) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", "pipe:0",
         "-codec:a", "libmp3lame", "-b:a", "64k",
         str(mp3_path)],
        input=wav_bytes,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0 or not mp3_path.exists():
        raise RuntimeError(
            f"ffmpeg failed for {mp3_path.name}: "
            f"{proc.stderr.decode()[-500:]}"
        )


def build(lang: str, limit: int) -> tuple[int, int]:
    from piper import PiperVoice

    dlang = DB_LANG[lang]
    model, speaker = VOICES[lang]
    prefix = CLIP_PREFIX[lang]
    if not model.exists():
        raise SystemExit(f"voice model missing: {model} (download it first)")
    print(f"[{lang}] loading voice {model.name} ...", flush=True)
    voice = PiperVoice.load(str(model))

    outdir = AUDIO_DIR / lang
    outdir.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(DB_PATH)
    rows = _select_sentence_ids(db, dlang, limit)
    db.close()

    made, skipped = 0, 0
    for i, (sid, text) in enumerate(rows, 1):
        clip = outdir / f"{prefix}-{sid}.mp3"
        if clip.exists():
            skipped += 1
            continue
        try:
            wav_to_mp3(wav_bytes_for(voice, text, speaker), clip)
        except Exception as e:  # noqa: BLE001 - keep the batch going
            print(f"  [{lang}] SKIP {prefix}-{sid}: {e}")
            continue
        made += 1
        if made % 250 == 0:
            print(f"  [{lang}] {made} generated, {skipped} skipped "
                  f"({i}/{len(rows)})", flush=True)
    print(f"[{lang}] done: {made} generated, {skipped} skipped "
          f"({len(rows)} selected)")
    return made, skipped


_ALL_LANGS = ["es", "fr", "es-mx", "it", "de", "pt", "ru", "uk", "nl"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=_ALL_LANGS, default=None)
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args()
    langs = [args.lang] if args.lang else _ALL_LANGS
    total_made = total_skipped = 0
    for lang in langs:
        made, skipped = build(lang, args.limit)
        total_made += made
        total_skipped += skipped
    print(f"TOTAL: {total_made} generated, {total_skipped} skipped")


if __name__ == "__main__":
    main()
