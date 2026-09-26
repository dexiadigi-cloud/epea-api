#!/usr/bin/env python3
"""Epea content pipeline — STEP 2: content database.

Builds ~/workspace/epea/data/epea.db from cached raw downloads in
~/workspace/epea/data/raw/ (downloaded once, see BUILD_LOG.md):

  eng_sentences.tsv.bz2, spa_sentences.tsv.bz2, fra_sentences.tsv.bz2,
  ita/deu/por/rus/ukr/nld_sentences.tsv.bz2
      Tatoeba per-language sentence exports (id, lang, text). CC BY 2.0 FR.
  links.tar.bz2 -> links.csv
      Tatoeba sentence links (sentence_id, translation_id), used for
      eng<->spa and eng<->fra pairs (v1 snapshot). CC BY 2.0 FR.
  <lang>-eng_links.tsv.bz2 (ita/deu/por/rus/ukr/nld)
      Tatoeba per-language English link exports, used for the six step-7
      languages. CC BY 2.0 FR.
  words.csv, word_pos.csv
      Words-CEFR-Dataset (Maximax67, MIT). CEFR levels derived from CEFR-J.
  profanity.txt (../profanity.txt)
      Reviewable whole-word blocklist. Sections [eng]/[spa]/[fra] hand-built
      (v1); [ita]/[deu]/[por]/[rus] from LDNOOBW (CC BY 4.0), [ukr] from
      readme-SVG/Banned-words (Apache-2.0), [nld] from LDNOOBW (CC BY 4.0).

Re-runnable and idempotent: all content tables are dropped and rebuilt from
the raw inputs each run, so re-running yields identical row counts.

Schema:
  sources(name PK, license, attribution, url, share_alike)
  sentences(id PK, lang, text, license)            -- eng + 8 L2 languages
  links(sentence_id, translation_id)               -- both directions stored
  words(word, lang, cefr, rank, license)           -- PK(word, lang)

License posture B (decided 2026-09-25): BY-SA content allowed with
attribution, kept traceable via sources.share_alike. Step 7 adds no BY-SA
sources; the voice rows below keep audio attribution durable across
rebuilds (steps 4 and 6 recorded them by hand; from step 7 on they live
in this pipeline so a rebuild cannot drop them).

Data-quality rules applied (documented, reviewable):
  Sentences:
    - keep len 2..300 chars after strip; must contain at least one letter
    - drop rows containing http://, https://, www., .com/.net/.org (spam/URLs)
    - drop rows matching the profanity blocklist (whole word, case-insensitive;
      languages without a blocklist section skip this check)
    - dedupe exact duplicate text within a language (keep lowest Tatoeba id)
  Links:
    - keep only pairs where BOTH endpoints survived sentence filtering AND
      the pair is eng<->X for X in the 8 L2 languages (no L2<->L2 direct)
    - store both directions (a,b) and (b,a) for trivial lookup
  Words (English, from Words-CEFR-Dataset):
    - level is numeric 1..6 (fractional = averaged); 1->A1 2->A2 3->B1 4->B2 5->C1 6->C2
    - IMPORTANT: in this dataset level 6 is the default for the ungraded long
      tail (172k of 248k rows) and mixes proper nouns with true C2 words.
      v1 keeps level 6 ONLY when frequency_count >= 20M (common words where the
      grade is plausible; a few proper nouns slip in — documented, tolerable).
    - dedupe by word, keep the row with highest frequency_count
    - rank = 1-based order by frequency_count desc
    - word must be >=2 chars and contain a letter
  Words (all L2 languages):
    - derived from OUR filtered Tatoeba sentences (same CC BY 2.0 FR license)
    - tokens: lowercase, >=2 chars, contain a letter, count >= 2
    - top 30,000 per language by count; cefr NULL (no CEFR source outside English;
      frequency rank is the difficulty proxy for L2 words)
"""

import bz2
import csv
import re
import sqlite3
import sys
import tarfile
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent
RAW = DATA / "raw"
DB = DATA / "epea.db"

LANGS = ("eng", "spa", "fra", "ita", "deu", "por", "rus", "ukr", "nld")
# Step-7 languages use per-language English link exports (v1's spa/fra keep
# using links.tar.bz2 so their counts stay identical across rebuilds).
NEW_LANGS = ("ita", "deu", "por", "rus", "ukr", "nld")
L2_LANGS = ("spa", "fra") + NEW_LANGS
CEFR = {1: "A1", 2: "A2", 3: "B1", 4: "B2", 5: "C1", 6: "C2"}
ENG_L6_MIN_FREQ = 20_000_000  # see docstring: level 6 is the ungraded default

# Hand-reviewed 2026-09-25 for the level-6 band of THIS data snapshot:
# these are proper nouns (states, places, surnames), not C2 vocabulary.
# Re-review if the source data is ever refreshed.
L6_PROPER_NOUNS = frozenset({
    "california", "texas", "la", "columbia", "kansas", "wales", "trans",
    "va", "orleans", "evans", "cincinnati", "collins", "phillips", "moses",
})
TOP_WORDS_L2 = 30_000
MIN_TOKEN_COUNT = 2

TATOEBA_LICENSE = "CC BY 2.0 FR"
CEFR_WORDS_LICENSE = "MIT"

SOURCES = [
    {
        "name": "Tatoeba",
        "license": TATOEBA_LICENSE,
        "attribution": (
            "Sentence data by Tatoeba contributors, https://tatoeba.org — "
            "licensed CC BY 2.0 FR "
            "(https://creativecommons.org/licenses/by/2.0/fr/)."
        ),
        "url": "https://downloads.tatoeba.org/exports/",
        "share_alike": 0,
    },
    {
        "name": "Words-CEFR-Dataset",
        "license": CEFR_WORDS_LICENSE,
        "attribution": (
            "Words-CEFR-Dataset by Belikov Maxim, "
            "https://github.com/Maximax67/Words-CEFR-Dataset — MIT License. "
            "CEFR levels derived from the CEFR-J dataset (https://www.cefr-j.org/)."
        ),
        "url": "https://github.com/Maximax67/Words-CEFR-Dataset",
        "share_alike": 0,
    },
    # Piper voice models (build-time only; the voice licenses cover the
    # served MP3s). All verified from the voice MODEL_CARD in
    # rhasspy/piper-voices. Recorded here so DB rebuilds keep them.
    {
        "name": "piper-voice-es-sharvard",
        "license": "CC BY 3.0",
        "attribution": (
            "Spanish voice sharvard (es_ES-sharvard-medium) via "
            "rhasspy/piper-voices; source dataset CC BY 3.0, Edinburgh "
            "DataShare"
        ),
        "url": "https://datashare.ed.ac.uk/handle/10283/574",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-fr-siwis",
        "license": "CC BY 4.0",
        "attribution": (
            "French voice siwis (fr_FR-siwis-medium) via rhasspy/piper-voices; "
            "SIWIS French Speech Synthesis Database CC BY 4.0, University of "
            "Edinburgh CSTR"
        ),
        "url": "https://datashare.ed.ac.uk/items/1de74991-eede-4b48-8fbe-6c2abaed88d8",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-es-mx-ald",
        "license": "Unlicense (public domain)",
        "attribution": (
            "Ald Mexican Spanish speech dataset (rmcpantoja), dedicated to "
            "the public domain under the Unlicense. Voice model "
            "es_MX-ald-medium via rhasspy/piper-voices."
        ),
        "url": "https://huggingface.co/datasets/rmcpantoja/Ald_Mexican_Spanish_speech_dataset",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-it-serena",
        "license": "CC BY 4.0",
        "attribution": (
            "Italian voice serena (it_IT-serena-medium) via "
            "rhasspy/piper-voices, licensed CC BY 4.0."
        ),
        "url": "https://huggingface.co/rhasspy/piper-voices",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-de-thorsten",
        "license": "CC0",
        "attribution": (
            "German voice thorsten (de_DE-thorsten-medium) via "
            "rhasspy/piper-voices, dedicated to the public domain (CC0)."
        ),
        "url": "https://huggingface.co/rhasspy/piper-voices",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-pt-cadu",
        "license": "CC0",
        "attribution": (
            "Brazilian Portuguese voice cadu (pt_BR-cadu-medium) via "
            "rhasspy/piper-voices, dedicated to the public domain (CC0)."
        ),
        "url": "https://huggingface.co/rhasspy/piper-voices",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-ru-denis",
        "license": "CC0",
        "attribution": (
            "Russian voice denis (ru_RU-denis-medium) via "
            "rhasspy/piper-voices, dedicated to the public domain (CC0)."
        ),
        "url": "https://huggingface.co/rhasspy/piper-voices",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-uk-ukrainian_tts",
        "license": "CC0",
        "attribution": (
            "Ukrainian voice ukrainian_tts (uk_UA-ukrainian_tts-medium) via "
            "rhasspy/piper-voices, dedicated to the public domain (CC0)."
        ),
        "url": "https://huggingface.co/rhasspy/piper-voices",
        "share_alike": 0,
    },
    {
        "name": "piper-voice-nl-pim",
        "license": "CC0",
        "attribution": (
            "Dutch voice pim (nl_NL-pim-medium) via rhasspy/piper-voices, "
            "dedicated to the public domain (CC0)."
        ),
        "url": "https://huggingface.co/rhasspy/piper-voices",
        "share_alike": 0,
    },
    # Profanity blocklists (content build only; never served or shipped).
    # Licenses verified 2026-09-25 from each repo's LICENSE / API record.
    {
        "name": "ldnoobw-profanity",
        "license": "CC BY 4.0",
        "attribution": (
            "List of Dirty, Naughty, Obscene, and Otherwise Bad Words by "
            "LDNOOBW, https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words — "
            "CC BY 4.0. Used: de, it, pt, nl lists in full; ru list "
            "Cyrillic-script entries only."
        ),
        "url": "https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words",
        "share_alike": 0,
    },
    {
        "name": "banned-words-uk",
        "license": "Apache-2.0",
        "attribution": (
            "Banned-words multilingual list by readme-SVG, "
            "https://github.com/readme-SVG/Banned-words — Apache-2.0. "
            "Used: uk.txt (Ukrainian) only."
        ),
        "url": "https://github.com/readme-SVG/Banned-words",
        "share_alike": 0,
    },
]

# ---------------------------------------------------------------------------
# share-alike flagging (Posture B hygiene). BY is not SA.

def share_alike_for(license_name: str) -> int:
    """1 if the license carries a ShareAlike condition, else 0."""
    n = license_name.upper().replace(" ", "")
    return 1 if ("BY-SA" in n or "BY-NC-SA" in n) else 0


def _self_test_share_alike() -> None:
    assert share_alike_for("CC BY-SA 4.0") == 1
    assert share_alike_for("CC BY-NC-SA 4.0") == 1
    assert share_alike_for("CC BY 2.0 FR") == 0
    assert share_alike_for("CC BY 4.0") == 0
    assert share_alike_for("MIT") == 0
    assert share_alike_for("CC0") == 0
    # every declared source row must agree with the function
    for s in SOURCES:
        assert s["share_alike"] == share_alike_for(s["license"]), s["name"]


# ---------------------------------------------------------------------------
# filters

def load_profanity(path: Path):
    prof = {}
    section = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            prof[section] = []
        elif section:
            prof[section].append(line)
    return {lang: re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b",
                             re.IGNORECASE | re.UNICODE)
            for lang, words in prof.items() if words}


LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
SPAM = re.compile(r"https?://|www\.|\.(com|net|org)\b", re.IGNORECASE)
TOKEN = re.compile(r"[^\W\d_]{2,}", re.UNICODE)  # letter-runs, len>=2


def sentence_ok(text: str, lang: str, profanity) -> bool:
    t = text.strip()
    if not (2 <= len(t) <= 300):
        return False
    if not LETTER.search(t):
        return False
    if SPAM.search(t):
        return False
    rx = profanity.get(lang)
    if rx and rx.search(t):
        return False
    return True


# ---------------------------------------------------------------------------
# loaders

def load_sentences(profanity):
    """Return {id: (lang, text)} after filtering + per-language dedupe."""
    sentences = {}
    seen_text = set()
    stats = {"read": 0, "kept": 0, "dropped_filter": 0, "dropped_dupe": 0}
    for lang in LANGS:
        path = RAW / f"{lang}_sentences.tsv.bz2"
        with bz2.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                stats["read"] += 1
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    stats["dropped_filter"] += 1
                    continue
                try:
                    sid = int(parts[0])
                except ValueError:
                    stats["dropped_filter"] += 1
                    continue
                slang, text = parts[1], parts[2]
                if slang != lang or not sentence_ok(text, lang, profanity):
                    stats["dropped_filter"] += 1
                    continue
                key = (lang, text)
                if key in seen_text:
                    stats["dropped_dupe"] += 1
                    continue
                seen_text.add(key)
                sentences[sid] = (lang, text.strip())
                stats["kept"] += 1
    return sentences, stats


def _keep_pair(a, b, sentences, pairs, stats):
    """Keep (a,b) both directions if both endpoints survived filtering and
    the pair is eng<->L2 for one of the 8 L2 languages."""
    if a not in sentences or b not in sentences:
        return
    la, lb = sentences[a][0], sentences[b][0]
    langs = {la, lb}
    if "eng" in langs and len(langs) == 2 and langs - {"eng"} <= set(L2_LANGS):
        pairs.add((a, b))
        pairs.add((b, a))
        stats["kept_pairs"] += 1


def _scan_link_stream(f, sentences, pairs, stats):
    for line in f:
        stats["read"] += 1
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        try:
            a, b = (int(x) for x in line.rstrip("\n").split("\t")[:2])
        except ValueError:
            continue
        _keep_pair(a, b, sentences, pairs, stats)


def load_links(sentences):
    """Return set of (a, b) both directions for eng<->L2 pairs.

    v1 languages (spa, fra) come from links.tar.bz2 (keeps v1 counts
    identical); the six step-7 languages come from their per-language
    <lang>-eng_links.tsv.bz2 exports."""
    pairs = set()
    stats = {"read": 0, "kept_pairs": 0}
    with tarfile.open(RAW / "links.tar.bz2", "r:bz2") as tar:
        member = tar.getmember("links.csv")
        f = tar.extractfile(member)
        _scan_link_stream(f, sentences, pairs, stats)
    for lang in NEW_LANGS:
        path = RAW / f"{lang}-eng_links.tsv.bz2"
        with bz2.open(path, "rt", encoding="utf-8") as f:
            _scan_link_stream(f, sentences, pairs, stats)
    stats["kept_directed"] = len(pairs)
    return pairs, stats


def load_english_words():
    """Words-CEFR-Dataset -> [(word, cefr, rank)]. See docstring for level-6 rule."""
    names = {}
    with open(RAW / "words.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            names[row["word_id"]] = row["word"]
    best = {}  # word -> (freq, level)
    with open(RAW / "word_pos.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                freq = int(row["frequency_count"] or 0)
                lvl = round(float(row["level"]))
            except (ValueError, TypeError):
                continue
            lvl = min(max(lvl, 1), 6)
            if lvl == 6 and freq < ENG_L6_MIN_FREQ:
                continue  # ungraded long-tail default; not trustworthy C2
            word = names.get(row["word_id"], "").strip().lower()
            if len(word) < 2 or not LETTER.search(word):
                continue
            if lvl == 6 and word in L6_PROPER_NOUNS:
                continue  # hand-reviewed proper nouns, not C2 vocab
            if word not in best or freq > best[word][0]:
                best[word] = (freq, lvl)
    ordered = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(w, CEFR[lvl], rank + 1) for rank, (w, (freq, lvl)) in enumerate(ordered)]


def load_l2_words(sentences):
    """Token frequencies from our filtered sentences -> per-lang
    [(word, None, rank)]. Top 30,000 per L2 language, cefr NULL (no CEFR
    source outside English); same CC BY 2.0 FR license as the sentences."""
    counts = {lang: Counter() for lang in L2_LANGS}
    for lang, text in sentences.values():
        if lang in counts:
            counts[lang].update(t.lower() for t in TOKEN.findall(text))
    out = {}
    for lang, c in counts.items():
        top = [(w, n) for w, n in c.most_common(TOP_WORDS_L2) if n >= MIN_TOKEN_COUNT]
        out[lang] = [(w, None, rank + 1) for rank, (w, n) in enumerate(top)]
    return out


# ---------------------------------------------------------------------------
# build

def build():
    _self_test_share_alike()
    profanity = load_profanity(DATA / "profanity.txt")

    sentences, s_stats = load_sentences(profanity)
    links, l_stats = load_links(sentences)
    eng_words = load_english_words()
    other_words = load_l2_words(sentences)

    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE sources(
            name TEXT PRIMARY KEY,
            license TEXT NOT NULL,
            attribution TEXT NOT NULL,
            url TEXT NOT NULL,
            share_alike INTEGER NOT NULL CHECK(share_alike IN (0,1)));
        CREATE TABLE sentences(
            id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL,
            text TEXT NOT NULL,
            license TEXT NOT NULL);
        CREATE TABLE links(
            sentence_id INTEGER NOT NULL,
            translation_id INTEGER NOT NULL,
            PRIMARY KEY(sentence_id, translation_id));
        CREATE TABLE words(
            word TEXT NOT NULL,
            lang TEXT NOT NULL,
            cefr TEXT,
            rank INTEGER NOT NULL,
            license TEXT NOT NULL,
            PRIMARY KEY(word, lang));
        CREATE INDEX idx_sentences_lang ON sentences(lang);
        CREATE INDEX idx_sentences_lang_len ON sentences(lang, length(text), id);
        CREATE INDEX idx_links_tid ON links(translation_id);
        CREATE INDEX idx_words_lang_rank ON words(lang, rank);
    """)
    cur.executemany(
        "INSERT INTO sources(name, license, attribution, url, share_alike) VALUES(?,?,?,?,?)",
        [(s["name"], s["license"], s["attribution"], s["url"], s["share_alike"]) for s in SOURCES])
    cur.executemany(
        "INSERT INTO sentences(id, lang, text, license) VALUES(?,?,?,?)",
        [(sid, lang, text, TATOEBA_LICENSE) for sid, (lang, text) in sentences.items()])
    cur.executemany(
        "INSERT INTO links(sentence_id, translation_id) VALUES(?,?)", sorted(links))
    word_rows = [(w, "eng", cefr, rank, CEFR_WORDS_LICENSE) for w, cefr, rank in eng_words]
    for lang in L2_LANGS:
        word_rows += [(w, lang, cefr, rank, TATOEBA_LICENSE) for w, cefr, rank in other_words[lang]]
    cur.executemany(
        "INSERT INTO words(word, lang, cefr, rank, license) VALUES(?,?,?,?,?)", word_rows)
    con.commit()

    # license traceability: every content license must have a sources row
    src_licenses = {r[0] for r in cur.execute("SELECT license FROM sources")}
    for table in ("sentences", "words"):
        used = {r[0] for r in cur.execute(f"SELECT DISTINCT license FROM {table}")}
        missing = used - src_licenses
        assert not missing, f"{table} licenses missing from sources: {missing}"
    # share-alike hygiene: any BY-SA content must sit under a share_alike=1 source
    sa_sources = {r[0] for r in cur.execute("SELECT license FROM sources WHERE share_alike=1")}
    for table in ("sentences", "words"):
        for (lic,) in cur.execute(f"SELECT DISTINCT license FROM {table}"):
            assert share_alike_for(lic) == (1 if lic in sa_sources else 0), lic

    counts = {
        "sentences": {r[0]: r[1] for r in cur.execute("SELECT lang, COUNT(*) FROM sentences GROUP BY lang")},
        "links_directed": cur.execute("SELECT COUNT(*) FROM links").fetchone()[0],
        "words": {r[0]: r[1] for r in cur.execute("SELECT lang, COUNT(*) FROM words GROUP BY lang")},
    }
    con.close()
    return s_stats, l_stats, counts


def main():
    s_stats, l_stats, counts = build()
    print("sentences:", s_stats)
    print("links:", l_stats)
    print("counts:", counts)
    print("OK ->", DB)


if __name__ == "__main__":
    sys.exit(main())
