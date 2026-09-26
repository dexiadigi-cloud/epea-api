#!/usr/bin/env python3
"""Epea step 9: build-time grammar-topic tagging (verb tense) with spaCy.

Reads lesson-pool sentences per language, runs a spaCy pipeline, and labels
each sentence with grammar topics from verb morphology:

    past-tense     any VERB/AUX with Tense=Past
    present-tense  any VERB/AUX with Tense=Pres
    future-tense   any VERB/AUX with Tense=Fut

A sentence can carry several topics ("I ate and I will eat"). Output table:

    sentence_topics(sentence_id INTEGER, topic TEXT,
                    PRIMARY KEY(sentence_id, topic))

Plus a compound-tense heuristic for German and Dutch, whose spoken past
(Perfekt: "ich habe gemacht") and future ("er wird kommen") are analytic:
the finite auxiliary alone carries Tense=Pres, so the plain morphology
rule would label them present-tense only.

    de/nl past:   AUX lemma in {haben, sein} / {hebben, zijn}
                  + a VERB/AUX with VerbForm=Part  -> past-tense
    de/nl future: AUX lemma in {werden} / {zullen}
                  + a VERB with VerbForm=Inf      -> future-tense

LICENSE POSTURE (verified 2026-09-25 against the installed packages; the
PLAN.md survey claim "models MIT" was wrong for most of them):

    es_core_news_sm 3.8.0 : GNU GPL 3.0
    fr_core_news_sm 3.8.0 : LGPL-LR
    de_core_news_sm 3.8.0 : MIT
    pt_core_news_sm 3.8.0 : MIT
    ru_core_news_sm 3.8.0 : MIT
    uk_core_news_sm 3.8.0 : MIT
    nl_core_news_sm 3.8.0 : CC BY-SA 4.0
    it_core_news_sm 3.8.0 : CC BY-NC-SA 3.0  -> NOT USABLE (see below)

Italian is tagged with Stanza instead: the stanza-it model card published
by the model authors declares license: apache-2.0 (verified 2026-09-26
against huggingface.co/stanfordnlp/stanza-it, the exact repo stanza
downloads from). Same build-time-only treatment as the spaCy models.

Treatment of the usable models follows the Piper GPL-3.0 precedent
(BUILD_LOG step 4): they are BUILD-TIME tools only. They live in
tools/.venv, are never installed into the runtime api venv, never appear
in api/requirements.txt, and never ship in the Docker image (the Dockerfile
installs only api/requirements.txt). Per the FSF position on program
output, the tense tags we generate are facts we computed, not covered by
the models' copyrights. Each model is recorded in the sources table for
transparency (share_alike follows the same mapping as build_content.py).

Pool: starts from the 2,000-sentence audio lesson pool per language (same
selection as build_audio.py: length 8..140, has an English translation,
easiest first). If any topic ends up with fewer than 200 sentences, the
pool for that language expands to 20,000 and is re-tagged.

Re-runnable and idempotent: per-language tags are deleted before insert.
NOTE: build_content.py rebuilds epea.db from scratch (it unlinks the file),
so re-run this script after any content rebuild. Sentence ids are
deterministic (same raw data, same import order), so tags re-attach cleanly.

Usage:
    tools/.venv/bin/python data/tag_grammar.py [--lang de] [--limit 2000]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "epea.db"

# Stanza looks here for downloaded models; keep them inside the project so a
# rebuild never depends on $HOME. (Set before stanza is imported/used.)
os.environ.setdefault(
    "STANZA_RESOURCES_DIR", str(ROOT / "tools" / "stanza_resources")
)

# api_lang -> (tatoeba lang code, spacy model, model license from METADATA)
LANGS = {
    "es": ("spa", "es_core_news_sm", "3.8.0", "GNU GPL 3.0"),
    "fr": ("fra", "fr_core_news_sm", "3.8.0", "LGPL-LR"),
    "de": ("deu", "de_core_news_sm", "3.8.0", "MIT"),
    "pt": ("por", "pt_core_news_sm", "3.8.0", "MIT"),
    "ru": ("rus", "ru_core_news_sm", "3.8.0", "MIT"),
    "uk": ("ukr", "uk_core_news_sm", "3.8.0", "MIT"),
    "nl": ("nld", "nl_core_news_sm", "3.8.0", "CC BY-SA 4.0"),
    # it SKIPPED for spaCy: it_core_news_sm is CC BY-NC-SA 3.0
    # (non-commercial -> blocked). Italian is tagged with Stanza instead
    # (see STANZA_LANGS below).
}

# api_lang -> (tatoeba lang code, model repo, model license)
# Stanza-based tagging. License verified 2026-09-26: the stanza-it model
# card published by the model authors (stanfordnlp/stanza-it, the exact
# repo stanza downloads from) declares license: apache-2.0 in its
# frontmatter; the stanza 1.14.0 package itself is Apache-2.0 per its
# installed METADATA. No separate license file ships inside the .pt
# checkpoints (verified by inspection). Same build-time-only treatment
# as the spaCy models: tools/.venv only, never in the runtime.
STANZA_LANGS = {
    "it": ("ita", "stanfordnlp/stanza-it", "Apache-2.0"),
}

TOPICS = ("past-tense", "present-tense", "future-tense")
TENSE_MAP = {"Past": "past-tense", "Pres": "present-tense", "Fut": "future-tense"}

# de/nl compound-tense heuristic (see module docstring).
_HAVE_AUX = {"de": {"haben", "sein"}, "nl": {"hebben", "zijn"}}
_FUT_AUX = {"de": {"werden"}, "nl": {"zullen"}}

POOL_START = 2000
POOL_EXPANDED = 20000
MIN_TOPIC_START = 200  # below this after the first pass -> expand the pool


def share_alike_for(license_name: str) -> int:
    n = license_name.upper().replace(" ", "")
    return 1 if ("BY-SA" in n or "BY-NC-SA" in n) else 0


def select_pool(db: sqlite3.Connection, dlang: str, limit: int):
    """Easiest `limit` (id, text) in dlang with an English translation.
    Walk-and-test with indexed PK lookups (same pattern as build_audio.py:
    the join form takes minutes on the 7M-row links table)."""
    out: list[tuple[int, str]] = []
    offset, chunk = 0, 2000
    while len(out) < limit:
        rows = db.execute(
            """SELECT id, text FROM sentences
               WHERE lang=? AND length(text) BETWEEN 8 AND 140
               ORDER BY length(text), id LIMIT ? OFFSET ?""",
            (dlang, chunk, offset),
        ).fetchall()
        if not rows:
            break
        offset += chunk
        for sid, text in rows:
            hit = db.execute(
                """SELECT 1 FROM links l
                   CROSS JOIN sentences e ON e.id = l.translation_id
                   WHERE l.sentence_id = ? AND e.lang = 'eng' LIMIT 1""",
                (sid,),
            ).fetchone()
            if hit:
                out.append((sid, text))
                if len(out) >= limit:
                    break
    return out


def topics_for(doc, api_lang: str) -> set[str]:
    topics: set[str] = set()
    for t in doc:
        if t.pos_ in ("VERB", "AUX"):
            for ten in t.morph.get("Tense"):
                if ten in TENSE_MAP:
                    topics.add(TENSE_MAP[ten])
    if api_lang in _HAVE_AUX or api_lang in _FUT_AUX:
        aux_lemmas = {t.lemma_ for t in doc if t.pos_ in ("VERB", "AUX")}
        if api_lang in _HAVE_AUX and (aux_lemmas & _HAVE_AUX[api_lang]):
            if any("Part" in t.morph.get("VerbForm")
                   for t in doc if t.pos_ in ("VERB", "AUX")):
                topics.add("past-tense")
        if api_lang in _FUT_AUX and (aux_lemmas & _FUT_AUX[api_lang]):
            if any("Inf" in t.morph.get("VerbForm")
                   for t in doc if t.pos_ == "VERB"):
                topics.add("future-tense")
    return topics


def tag_language(db: sqlite3.Connection, api_lang: str, limit: int) -> dict:
    dlang, model_name, model_ver, model_lic = LANGS[api_lang]
    import spacy

    print(f"[{api_lang}] loading {model_name} ...", flush=True)
    nlp = spacy.load(model_name, disable=["parser", "ner", "senter"])
    pool = select_pool(db, dlang, limit)
    print(f"[{api_lang}] tagging {len(pool)} sentences ...", flush=True)
    counts = {t: 0 for t in TOPICS}
    tagged: list[tuple[int, str]] = []
    for (sid, text), doc in zip(pool, nlp.pipe(
            (t for _, t in pool), batch_size=256)):
        for topic in topics_for(doc, api_lang):
            tagged.append((sid, topic))
            counts[topic] += 1
    # idempotent: wipe this language's tags, then insert
    db.execute(
        """DELETE FROM sentence_topics WHERE sentence_id IN
           (SELECT id FROM sentences WHERE lang=?)""",
        (dlang,),
    )
    db.executemany(
        "INSERT OR IGNORE INTO sentence_topics(sentence_id, topic)"
        " VALUES(?,?)",
        tagged,
    )
    db.commit()
    # record the model in sources (build-time tool, not distributed)
    db.execute(
        "INSERT OR REPLACE INTO sources(name, license, attribution, url,"
        " share_alike) VALUES(?,?,?,?,?)",
        (
            f"spacy-model-{model_name}",
            model_lic,
            f"Explosion spaCy {model_name} {model_ver}: build-time"
            " grammar-topic tagging only; the model is not distributed"
            " with the API.",
            f"https://spacy.io/models/{api_lang}",
            share_alike_for(model_lic),
        ),
    )
    db.commit()
    return {"pool": len(pool), "counts": counts}


def _stanza_tenses(feats) -> set[str]:
    """Tense=Past/Pres/Fut -> topic names, from a UD feats string."""
    out: set[str] = set()
    if not feats:
        return out
    for kv in str(feats).split("|"):
        if kv.startswith("Tense="):
            ten = kv.split("=", 1)[1]
            if ten in TENSE_MAP:
                out.add(TENSE_MAP[ten])
    return out


def topics_for_stanza(doc) -> set[str]:
    """Same three topics as the spaCy path, from Stanza UD morphology.

    Italian needs no compound-tense heuristic: in UD Italian the past
    participle itself carries Tense=Past (verified: "Ho mangiato" ->
    mangiato Tense=Past; "Mangerò" -> Tense=Fut), so the plain rule
    catches passato prossimo with both avere and essere."""
    topics: set[str] = set()
    for sent in doc.sentences:
        for w in sent.words:
            if w.upos in ("VERB", "AUX"):
                topics |= _stanza_tenses(w.feats)
    return topics


def tag_language_stanza(db: sqlite3.Connection, api_lang: str,
                        limit: int) -> dict:
    dlang, model_repo, model_lic = STANZA_LANGS[api_lang]
    import stanza

    print(f"[{api_lang}] loading stanza pipeline ({model_repo}) ...",
          flush=True)
    nlp = stanza.Pipeline(
        api_lang, processors="tokenize,mwt,pos,lemma",
        verbose=False, use_gpu=False,
    )
    pool = select_pool(db, dlang, limit)
    print(f"[{api_lang}] tagging {len(pool)} sentences ...", flush=True)
    counts = {t: 0 for t in TOPICS}
    tagged: list[tuple[int, str]] = []
    batch, bs = [], 64
    docs: list[tuple[list[tuple[int, str]], object]] = []

    def flush_batch():
        if not batch:
            return
        doc = nlp("\n\n".join(t for _, t in batch))
        if len(doc.sentences) == len(batch):
            pairs = list(zip(batch, doc.sentences))
        else:
            # sentence segmentation drifted; fall back to one doc each
            pairs = [((sid, t), nlp(t).sentences[0])
                     for sid, t in batch]
        for (sid, _), sent in pairs:
            for topic in topics_for_stanza(
                    type("D", (), {"sentences": [sent]})):
                tagged.append((sid, topic))
                counts[topic] += 1
        batch.clear()

    for item in pool:
        batch.append(item)
        if len(batch) >= bs:
            flush_batch()
    flush_batch()
    # idempotent: wipe this language's tags, then insert
    db.execute(
        """DELETE FROM sentence_topics WHERE sentence_id IN
           (SELECT id FROM sentences WHERE lang=?)""",
        (dlang,),
    )
    db.executemany(
        "INSERT OR IGNORE INTO sentence_topics(sentence_id, topic)"
        " VALUES(?,?)",
        tagged,
    )
    db.commit()
    # record the model in sources (build-time tool, not distributed)
    db.execute(
        "INSERT OR REPLACE INTO sources(name, license, attribution, url,"
        " share_alike) VALUES(?,?,?,?,?)",
        (
            f"stanza-model-{api_lang}",
            model_lic,
            "Stanford Stanza Italian model: build-time grammar-topic"
            " tagging only; the model is not distributed with the API."
            " License per the publisher's model card on"
            " huggingface.co/stanfordnlp/stanza-it (apache-2.0).",
            f"https://huggingface.co/{model_repo}",
            0,
        ),
    )
    db.commit()
    return {"pool": len(pool), "counts": counts}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang",
                    choices=sorted(set(LANGS) | set(STANZA_LANGS)),
                    default=None)
    ap.add_argument("--limit", type=int, default=POOL_START)
    args = ap.parse_args()

    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS sentence_topics(
               sentence_id INTEGER NOT NULL,
               topic TEXT NOT NULL,
               PRIMARY KEY(sentence_id, topic))"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_topics_topic"
        " ON sentence_topics(topic)"
    )
    db.commit()

    langs = [args.lang] if args.lang else sorted(set(LANGS) | set(STANZA_LANGS))
    summary = {}
    for api_lang in langs:
        start = args.limit if args.limit else POOL_START
        tagger = (tag_language_stanza if api_lang in STANZA_LANGS
                  else tag_language)
        res = tagger(db, api_lang, start)
        thin = [t for t in TOPICS if res["counts"][t] < MIN_TOPIC_START]
        if thin and res["pool"] < POOL_EXPANDED:
            print(f"[{api_lang}] topics thin {thin} -> expanding pool to"
                  f" {POOL_EXPANDED}", flush=True)
            res = tagger(db, api_lang, POOL_EXPANDED)
        summary[api_lang] = res
        c = res["counts"]
        print(f"[{api_lang}] pool={res['pool']}"
              f" past={c['past-tense']} pres={c['present-tense']}"
              f" fut={c['future-tense']}", flush=True)
    db.close()
    print("SUMMARY:", summary)


if __name__ == "__main__":
    main()
