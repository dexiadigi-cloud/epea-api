# Epea — Build Log

## Step 1: project scaffold with verified health + auth (2026-09-25)
Built in ~/workspace/epea/ (PLAN.md and NAME_SHORTLIST.md untouched):
- `api/app.py`: FastAPI app, OpenAPI 3.1. `GET /health` public returns
  {"status":"ok","version":"0.1.0"}. `GET /languages` requires X-API-Key,
  returns Spanish (es) and French (fr), each marked "planned" with the note
  that curriculum content arrives in step 2. Auth: X-API-Key header compared
  with hmac.compare_digest against EPEA_API_KEY env var; 401 JSON detail
  on missing/wrong key. App refuses to start without EPEA_API_KEY set.
- `api/requirements.txt`: fastapi==0.141.1, uvicorn==0.53.0 (same pins as
  Trophe, verified installing cleanly).
- `.venv/`: Python 3.12 venv with the requirements installed.
- `Dockerfile`: modeled on Trophe's (python:3.12-slim, WORKDIR /srv/epea/api,
  pip install, uvicorn CMD, EPEA_API_KEY from host env). Not built, not pushed.
- `.gitignore`: .venv, __pycache__, *.pyc, *.db, .env.

Verification (uvicorn on 127.0.0.1:8124, EPEA_API_KEY=test-key-123, all curls
with --noproxy '*' to bypass the egress proxy):
- GET /health (no key) -> 200 {"status":"ok","version":"0.1.0"} PASS
- GET /languages (no key) -> 401 {"detail":"Invalid or missing API key. ..."} PASS
- GET /languages (wrong key) -> 401 PASS
- GET /languages (correct key) -> 200, es + fr with planned notes PASS
- GET /openapi.json -> 200, valid JSON, paths ['/health', '/languages'] PASS
Server shut down after verification. No fixes needed; everything passed first try.

Hard boundaries respected: stayed inside ~/workspace/epea/. Nothing deployed,
no repo created, no money spent, Phos/Trophe untouched.

## 2026-09-25 — STEP 2: content database (epea.db)

Pipeline: `data/build_content.py` (re-runnable, idempotent — tables are
dropped and rebuilt from cached raw downloads each run; two consecutive runs
produced identical counts).

Raw downloads (cached in `data/raw/`, downloaded once):
- Tatoeba per-language exports (2026-09-19 snapshot): eng_sentences.tsv.bz2
  (24.9 MB), spa_sentences.tsv.bz2 (6.4 MB), fra_sentences.tsv.bz2 (9.7 MB),
  links.tar.bz2 (149.8 MB -> links.csv). CC BY 2.0 FR.
- Words-CEFR-Dataset (Maximax67, MIT; LICENSE verified in repo): words.csv,
  word_pos.csv, word_categories.csv, categories.csv, pos_tags.csv.
  CEFR levels derived from CEFR-J (cited in attribution).

Schema: sources(name, license, attribution, url, share_alike);
sentences(id, lang, text, license); links(sentence_id, translation_id) stored
both directions; words(word, lang, cefr, rank, license).

Final counts (epea.db, 358.7 MB):
- sentences: eng 2,029,844 / spa 440,740 / fra 723,173 (3,193,757 total;
  3,205,874 read, 12,117 dropped by filters, 0 exact dupes)
- links: 1,439,516 directed rows = 719,758 eng<->spa / eng<->fra pairs
  (28,478,682 link rows scanned; 0 orphan links)
- words: eng 46,808 (CEFR A1 6,236 / A2 10,082 / B1 11,337 / B2 13,380 /
  C1 5,759 / C2 14) / spa 30,000 / fra 30,000 (freq-ranked, cefr NULL)

Filters: length 2..300 chars, must contain a letter, drop URLs/spam
(http/www/.com/.net/.org), whole-word profanity blocklist
(`data/profanity.txt`: eng/spa/fra sections, reviewable), exact-dupe removal
per language. Spot-checked 3 eng-spa + 3 eng-fra pairs — all genuine.

License hygiene (Posture B): sources rows = Tatoeba (CC BY 2.0 FR,
share_alike=0, full attribution text) + Words-CEFR-Dataset (MIT,
share_alike=0). No BY-SA sources in v1. share_alike_for() maps any
BY-SA/BY-NC-SA license -> 1; self-test in the script asserts the mapping and
that every content license traces to a sources row.

Fixes during build:
- Words-CEFR-Dataset "level 6" is the ungraded default for the 172k-word long
  tail (mixes proper nouns with true C2 words). v1 keeps level 6 only at
  frequency >= 20M, then hand-removed 14 proper nouns (california, texas,
  kansas, ...) via the L6_PROPER_NOUNS denylist in the script (documented,
  re-review on data refresh). C2 band is now 14 legitimate words.
- wordfreq considered for spa/fra vocab but rejected: its README discourages
  CSV conversion and spa/fra ranks derived from our filtered Tatoeba sentences
  are cleaner-licensed (same CC BY 2.0 FR) and better aligned to learner text.

## Step 3 (2026-09-25): exercise engine + learner endpoints (v0.2.0)

`api/app.py` extended; `api/requirements.txt` += `fsrs==6.3.2`
(the py-fsrs PyPI package, MIT; `pip install py-fsrs` is wrong, the
distribution name is `fsrs`).

New tables in `data/epea.db` (created on startup, IF NOT EXISTS):
- learners(id TEXT PK, created_at)
- cards(learner_id, exercise_key PK, state, step, stability, difficulty,
  due, last_review) — persists fsrs 6.x Card fields 1:1 (reps/lapses do
  not exist in fsrs 6.x; state/step cover scheduling)
- attempts(id, learner_id, exercise_id, type, correct, at)

Endpoints (all 401 without X-API-Key except /health):
- POST /learners -> {"learner_id": uuid4}; opaque, no PII
- GET /lesson/next?learner_id=&lang=es|fr -> up to 5 exercises, FSRS-due
  cards first, then new items round-robin mc/tr/cl from the easiest
  unseen candidates. Answers never included.
- GET /exercise/{id} -> single exercise payload, no answer
- POST /grade {learner_id, exercise_id, answer} -> {correct, feedback,
  expected}; layered grading (normalize, then accept-list); MC accepts
  index 0-3, letter A-D, or choice text
- GET /progress?learner_id= -> done/correct/accuracy/due_now/by_type
- DELETE /learners/{id} -> wipes learners+cards+attempts rows

Exercise identity is deterministic: mc-es-<sid> / tr-es-<sid> /
cl-es-<sid> (sid = L2 sentence for mc/cloze, eng prompt sentence for
translate), so cards persist and grading is reproducible. MC distractors
are seeded by exercise_id (same-lang, similar length, never the correct
sentence nor another translation of the prompt). Cloze blanks the most
frequent top-5000 word in the sentence. Missed cards get due=now so they
reappear immediately.

Language codes: API surface is es/fr (per plan); content DB uses Tatoeba
codes spa/fra/eng. Mapped once via DB_LANG in app.py.

Accent-leniency DECISION (plan Q2): accents are REQUIRED for es/fr.
normalize() lowercases, collapses whitespace, strips edge punctuation,
but does NOT strip diacritics. Rationale: dropping accents teaches
wrong spelling in languages where they are orthographically meaningful
(si vs sí, papa vs papá). Leniency can be revisited with learner data.

Performance fix: candidate-pool queries with correlated EXISTS took
minutes on the 3.2M-row table. Pools are now computed once at startup
(~seconds, non-correlated IN-subquery form) and cached in _CANDS; the
content DB is static so the cache is always correct.

Verification (server 127.0.0.1:8125, throwaway key, all 9 checks passed):
1. POST /learners -> uuid returned (35e723bb-...).
2. lesson/next (new learner, es) -> 5 items, types
   [multiple_choice, translate, cloze, multiple_choice, translate];
   no correct_index/_expected fields in JSON.
3. GET /exercise/tr-es-1340 -> identical to lesson item.
4. Grade correct ("¿Entonces qué?") -> correct:true; grade wrong
   ("xyzzy nonsense") -> correct:false, expected "¿Entonces qué?" shown.
5. Second linked translation ("Intentemos algo." for "Let's try
   something.") -> correct:true (accept-list works).
6. progress -> done=3, correct=2, accuracy=0.667, due_now=1,
   by_type.translate={done:3, correct:2}. Math verified.
7. Fresh learner, failed first card -> next lesson/next returns the
   failed exercise first (due=now override on Rating.Again).
8. DELETE learner -> progress 404s; SELECT on learners/cards/attempts
   for that id all return 0.
9. Wrong API key -> 401 on POST /learners and GET /progress.

Fixes during step 3:
- DB lang-code mismatch (spa/fra vs es/fr) -> DB_LANG map.
- correct_index leaked in public payload -> renamed to _correct_index.
- Correlated EXISTS candidate queries too slow -> startup cache.

Server shut down after verification. Step 4 (audio) NOT started.

## Step 4 — pronunciation audio (2026-09-25, ~23:30-24:05 HST)

### Voices (one per v1 language)
- es: `es_ES-sharvard-medium` (2 speakers; using speaker 0 consistently), **CC BY 3.0** —
  verified in the voice's official MODEL_CARD from rhasspy/piper-voices
  (dataset: Edinburgh DataShare https://datashare.ed.ac.uk/handle/10283/574).
- fr: `fr_FR-siwis-medium` (1 speaker), **CC BY 4.0** — verified in the
  voice's official MODEL_CARD (SIWIS French Speech Synthesis Database,
  https://datashare.ed.ac.uk/items/1de74991-eede-4b48-8fbe-6c2abaed88d8;
  corroborated by the SIWIS report and Edinburgh DataShare record).
- Both recorded in the `sources` table (share_alike=0) with attribution text.

### Engine note
- Piper installed per official docs into an isolated build venv
  (`tools/.venv`, NOT the runtime `.venv`, NOT in api/requirements.txt).
- Installed piper-tts 1.8.0 is **GPL-3.0-or-later** (OHF-voice fork), not MIT
  as the plan's table said for the archived rhasspy/piper. This is fine:
  Piper is a pure build-time tool — never distributed, never linked into or
  shipped with the API. Per FSF's position, program output is not covered by
  the program's copyright, so the served MP3s are not GPL-derived. The voice
  model licenses (CC BY above) are what cover the audio. Runtime venv
  verified clean: `pip freeze` shows no piper/onnxruntime/espeak.
- Voice downloads: es voice (76.7MB) came from HuggingFace; fr voice (63.1MB)
  came via the k2-fsa/sherpa-onnx GitHub release mirror
  (vits-piper-fr_FR-siwis-medium.tar.bz2) after the HF egress proxy proved
  too flaky — identical official voice files, MODEL_CARD verified inside.

### Build
- `data/build_audio.py`: pre-generates MP3s for the v1 new-learner lesson
  pool. Selection query (documented in the script) mirrors the MC candidate
  pool in app.py (`lang`, length 8-140, has an eng translation link, ordered
  by length, LIMIT 2000 per language). clip_id = `s-<sentence_id>`,
  output `data/audio/{es,fr}/s-<id>.mp3`. Re-runnable, skips existing.
- Loads each voice ONCE and synthesizes in-process via the piper Python API
  (WAV bytes piped to ffmpeg, libmp3lame 64k) — ~14 clips/sec.
- Result: **2000 es + 2000 fr = 4000 clips, 34MB on disk.**

### API
- `GET /audio/{clip_id}.mp3` (X-API-Key protected): serves the static MP3
  with `content-type: audio/mpeg`; clip_id validated against `^s-\d+$`
  (path traversal -> 404); missing clip -> 404 JSON. No TTS in the request
  path — pure static file serving.
- `GET /health` now also reports `audio_clips` (4000).

### Verification (server 127.0.0.1:8124, throwaway key; server shut down after)
1. build_audio.py end-to-end: es 2000 clips, fr 2000 clips, 34MB total.
2. Spot-checked 5 clips (3 es, 2 fr) with ffprobe: all valid MP3,
   22050Hz mono, durations 0.60-0.94s, sizes 5-8KB.
3. `/audio/s-2757.mp3` with key -> 200, content-type audio/mpeg, bytes
   identical to the file on disk (cmp). French clip likewise 200 + match.
   No key -> 401. Nonexistent clip -> 404 JSON. Traversal attempt -> 404.
4. Runtime `.venv` pip freeze: no piper, onnxruntime, or espeak —
   TTS lives only in `tools/.venv` (build-time).
5. Re-ran build_audio.py -> 0 generated, 4000 skipped (19s). No dupes.

## Step 5 — legal pages: privacy policy + terms of service (2026-09-25)

### What was added
- `GET /privacy` and `GET /terms` in `api/app.py` (public, no API key required —
  registered without `include_in_schema=False` so both appear in /openapi.json,
  matching the Trophe format/quality bar: same CSS, section structure, plain
  short sentences, contact dexiadigi@gmail.com).
- Both pages marked **v1.0 DRAFT (pending operator approval)** — Jeremiah must
  approve before Meta submission, same as Trophe's legal review step.
- Key Epea-specific coverage (not in Trophe's pages):
  - Opaque UUID learner IDs; no accounts, no names/emails, no PII.
  - Write-capable API stated plainly: attempts + FSRS scheduling state stored
    server-side keyed to the UUID.
  - Erasure path: `DELETE /learners/{id}` wipes learner record, attempts, and
    FSRS cards; documented as the full-deletion mechanism.
  - Audio: pre-generated synthetic speech (Piper); no learner voice recorded
    or stored; v1 has no speaking exercises.
  - Content licenses with attribution (posture B): sentences Tatoeba
    CC BY 2.0 FR; es voice sharvard CC BY 3.0; fr voice SIWIS CC BY 4.0;
    CEFR word levels MIT; share-alike content kept in separately-marked
    tables; attribution texts in the API `sources` table and docs.
  - Educational disclaimer: reference/educational info only, not a guarantee
    of fluency, no educational-outcome claims.

### Verification (server 127.0.0.1:8124, throwaway key; server shut down after)
1. `GET /privacy` (no key) -> 200 text/html; contains "DELETE /learners" (2x),
   "opaque" (5x), contact email, "write-capable", "synthetic".
2. `GET /terms` (no key) -> 200 text/html; contains "effective" (3x), contact
   email, "DELETE /learners", "fluency".
3. Em-dash grep over both responses: 0. En-dash grep: 0.
4. `/openapi.json` paths include `/privacy` and `/terms`.
5. Auth regression: `GET /languages` without key -> 401; with key -> 200.
6. Server shut down after verification.

## 2026-09-25 — STEP 6: Mexican Spanish voice + top-use-case endpoints (v0.3.0)

### Part A — Mexican Spanish (es-mx), opt-in
- Voice found in rhasspy/piper-voices: es_MX-ald-medium (1 speaker, 22050Hz).
  License verified from the voice MODEL_CARD: **Unlicense (public domain)**
  via dataset rmcpantoja/Ald_Mexican_Spanish_speech_dataset. (The alternative
  es_MX-claude-high is Apache-2.0; ald-medium was chosen to match the other
  voices' medium quality tier.) Recorded in sources table as
  'piper-voice-es-mx-ald', share_alike=0.
- Generated the same 2,000-sentence es lesson pool with the MX voice into
  data/audio/es-mx/s-<id>.mp3 (23MB). Spot-checked 5 clips with mutagen: all
  valid MP3, 22050Hz, 0.7-1.2s. Re-run: 0 generated, 2000 skipped (idempotent).
- GET /audio/{clip}.mp3 now takes optional ?voice=: 'es' (default, Spain
  voice) or 'es-mx' (Mexican voice). Unknown voice -> 400. No param ->
  legacy behavior (es/ then fr/). es-ES stays the default; es-mx is opt-in
  per request. es language note in /languages mentions the MX option.
- build_audio.py extended: --lang es-mx, VOICES/DB_LANG entries; Piper stays
  build-time only (not in api/requirements.txt).

### Part B — new endpoints (all X-API-Key protected)
- GET /translate?text=&from=en&to=es: exact text match first, then LIKE
  substring fallback (limit 10, LIKE wildcards escaped). Each match lists
  translations with audio clip ids. from/to in {en,es,fr}, must differ.
  Empty query set -> {"matches":[]}.
- GET /words?lang=es&q=hola: word, cefr, rank + up to 3 example sentences,
  each with one English translation and audio id. Word-boundary matching in
  Python after a LIKE prefilter. Empty -> {"words":[]}.
- GET /progress extended with "weakest": bottom 10 exercise keys by accuracy
  (min 3 attempts each), each with exercise_id/type/attempts/correct/accuracy.
- POST /placement {"lang":"es"}: creates a learner, returns a fixed 10-probe
  set (4 easy MC from pool start, 2 medium tr + 1 medium cloze from pool
  offset 200, 1 hard tr + 2 hard cloze from pool offset 450; pools are
  length-ordered so position is the difficulty knob; walk skips ids that fail
  to build). Answers go through POST /grade. Probe ids stored in new
  placement_probes table (also wiped by DELETE /learners).
- GET /placement/result?learner_id=: suggested_cefr from probe accuracy over
  ANSWERED probes (unanswered neither help nor hurt). Thresholds: >=0.90 B2,
  >=0.70 B1, >=0.50 A2, >=0.30 A1, else A0. Returns
  {suggested_cefr, correct, answered, total, accuracy}.

### Verification (server 127.0.0.1:8124, throwaway key, all curls --noproxy '*')
- /translate "where is the bathroom" en->es: exact match + 3 es translations
  with audio ids, plus LIKE fallback row; nonsense query -> {"matches":[]}, 200.
- /words q=hola lang=es: word hola, rank 1084, 3 examples each with en
  translation + audio; q=zzzznonsense -> {"words":[]}, 200.
- 5 wrong answers on one exercise key -> /progress "weakest" lists it with
  attempts=5, correct=0, accuracy=0.0.
- Placement: 10 probes returned (4 MC + 3 medium + 3 hard, types verified);
  all 10 wrong -> {correct:0, answered:10, total:10, suggested_cefr:"A0"};
  mixed run (4 easy MC right via index brute-force, rest wrong) ->
  {correct:4, answered:10, suggested_cefr:"A1"}; math verified against
  independent count.
- Audio: default clip -> 200 audio/mpeg; ?voice=es -> 200; ?voice=es-mx ->
  200 (MX bytes differ from es bytes for same sentence); ?voice=xx -> 400;
  ?voice=ES-MX (case) -> 200.
- Auth regression: no-key on /translate, /words, /audio, /placement,
  /placement/result -> 401 on all.
- Zero em dashes in user-facing strings (grep count 0). Server shut down
  after verification.

### Fixed during the step
- Curl glob bug: `--noproxy *` unquoted expanded to filenames and requests
  went through the egress proxy (returned a parked-domain page). Always quote:
  `--noproxy '*'`.
- /health audio_clips now counts 6000 (es 2000 + fr 2000 + es-mx 2000).

## Step 7 (2026-09-26): six-language expansion (it, de, pt, ru, uk, nl) - v0.4.0

### Content DB (data/build_content.py, rebuilt twice - idempotent)
- Sentences kept: 7,017,773 (read 7,034,334; dropped_filter 16,561; dropped_dupe 0).
  Per language: deu 779,926; eng 2,029,844; fra 723,173; ita 987,224;
  nld 201,054; por 442,745; rus 1,224,276; spa 440,740; ukr 188,791.
- Links: 31,304,203 rows read -> 7,075,436 directed rows (3,537,718 pairs).
  Per-language pair survival (both endpoints alive after filtering):
  ita 719,507/721,563 (99.7%); deu 583,681/584,787 (99.8%);
  por 299,877/301,911 (99.3%); rus 826,782/828,127 (99.8%);
  ukr 217,151/217,645 (99.8%); nld 170,962/171,488 (99.7%).
- Words: deu/it/por/rus 30,000 each; nld 20,752; ukr 25,333 (smaller corpora,
  MIN_TOKEN_COUNT=2); eng 46,808; spa/fra 30,000 each. New-language words
  have cefr=NULL; frequency rank is the difficulty proxy.
- Sources (11): Tatoeba CC BY 2.0 FR; Words-CEFR-Dataset MIT; Piper voices -
  es sharvard CC BY 3.0, fr siwis CC BY 4.0, it serena CC BY 4.0,
  de thorsten CC0, pt cadu CC0, ru denis CC0, uk ukrainian_tts CC0,
  nl pim CC0, es-mx ald Unlicense (public domain).
- Spot checks (2 per new language, by eye): all genuine translations
  (e.g. deu "Es regnet."/"It is raining.", rus "Ty uveren?"/"Are you sure?",
  ukr "Snig znik."/"The snow has disappeared.", nld "Waarom ik?"/"Why me?").
- Rebuild #2 produced byte-identical row counts (sentences 7,017,773;
  links 7,075,436; words 272,893; sources 11). New index
  idx_sentences_lang_len(lang, length(text), id) added to schema and live DB.
- Profanity-filter gap: data/profanity.txt has sections only for eng/spa/fra;
  the six new languages skip profanity filtering (documented in code).
  Same-quality bar not fully met here - flagged, not papered over.

### Audio (data/build_audio.py, 12,000 new clips)
- Voices: it serena/medium CC BY 4.0; de thorsten/medium CC0;
  pt cadu/medium CC0 (pt_BR); ru denis/medium CC0;
  uk ukrainian_tts/medium CC0; nl pim/medium CC0. All .onnx + .onnx.json
  verified present (61MB each, uk 74MB).
- Generated 2,000 MP3s per new language (12,000 total; 18,000 with es/fr/es-mx).
- Clip verification (3 per language, 18 total): all valid MP3, 22,050 Hz,
  mono, duration 0.6-1.7s (all > 0.5s).
- Idempotency: re-ran all six languages -> 0 generated, 2,000 skipped each.

### API (api/app.py v0.4.0)
- Eight active curriculum languages: es, fr, it, de, pt, ru, uk, nl.
  Portuguese labeled Brazilian Portuguese. /translate supports en + all eight
  L2; /words supports all eight L2; new-language audio ids <lang>-<sid>.mp3.
- Startup: ~21s uncontended (v0.3.0 was ~15s for 2 languages).

### Verification (server 127.0.0.1:8126, throwaway key, all curls --noproxy '*')
- 27/27 endpoint tests passed: de + ru full lesson/grade-right/grade-wrong/
  progress loops; it + pt translate (10 matches each) and word lookup;
  nl placement (10 probes, all-wrong -> suggested_cefr A0);
  es/fr lesson regression; es audio 200, ?voice=es-mx 200, ?voice=xx 400;
  it audio 200; 401 without key and with wrong key (3 endpoints).
- Diacritics: 15/15 normalization tests passed (de Grusse/fur, pt coracao/nao,
  fr ou/cafe, es si differ from unaccented; case/punct/space lenient).
  No grading fix needed.

### Fixed during the step
- Startup join blowup: the 7M-row links table made the per-language candidate
  IN-subquery joins take 10+ minutes (server never came up). Replaced with
  walk-and-test over an indexed length ordering; new index
  idx_sentences_lang_len. Startup now ~21s.
- SQLite join-order trap: the planner chose sentences-first (2M-row scan per
  call) for links joins. Fixed with CROSS JOIN (links by PK first) in
  _has_translation and translations_of. Per-call cost went from ~0.3s to
  microseconds. Same fix applied to build_audio.py selection query.
- Stale -journal lock: a SIGTERM-killed import test left a hot journal that
  blocked all DB writes ("database is locked"). Cleared by stopping all DB
  users and letting SQLite roll it back; integrity_check ok, counts intact.
- Test-script bugs (not app bugs): /placement/result field is suggested_cefr
  (not cefr_estimate); exercise payloads carry no audio field - clip id is
  derived from exercise_id (<lang>-<sid>); python stdout buffering hid output
  until -u was used.
- Server shut down after verification.

## Step 8 - profanity lists for the six new languages (2026-09-25, v0.4.0)

Closed the step-7 gap: ita/deu/por/rus/ukr/nld sentences now pass through the
profanity filter. Real lists only; nothing invented.

### Sources (licenses verified 2026-09-25)
- LDNOOBW "List of Dirty, Naughty, Obscene, and Otherwise Bad Words"
  (https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words),
  CC BY 4.0 (LICENSE file + GitHub API record). Used: de (66), it (168),
  pt (76), nl (190) lists in full; ru list Cyrillic-script entries only
  (82 of 151; the 69 Latin-transliterated entries never match Cyrillic
  Tatoeba text, so they were dropped, not kept as dead weight).
  Recorded in sources as `ldnoobw-profanity`, share_alike=0.
- readme-SVG/Banned-words (https://github.com/readme-SVG/Banned-words),
  Apache-2.0 (GitHub API record). Used: uk.txt (133 entries) only.
  Recorded in sources as `banned-words-uk`, share_alike=0.
- Rejected: kateryna-bobrovnyk/obscene-ukr and bohdan1/AbusiveLanguageDataset
  (both have NO license statement - "no license info found" is a blocker).

### Sanity checks
- All lists non-trivial (66-190 entries). Spot-checked samples per language;
  all plausibly profane. Short standalone entries (nl kut/lul/pik, pt cu/pau)
  are genuine vulgarisms; multi-word phrases match as whole phrases via the
  existing \b regex, so embedded articles (it "il"/"di", nl "de"/"het",
  pt "de"/"que") cannot nuke the corpora.

### Integration
- Appended [ita]/[deu]/[por]/[rus]/[ukr]/[nld] sections to
  data/profanity.txt (same convention as [eng]/[spa]/[fra]); no code change
  needed - load_profanity/sentence_ok already key off section names.
- Updated the stale step-7 NOTE in build_content.py; added the two sources
  rows to SOURCES.

### Rebuild results (two identical runs - idempotent)
Additional sentences dropped by the new filters (before -> after):
- ita: 3,260 (0.33%); por: 3,353 (0.76%); deu: 667 (0.09%);
  nld: 568 (0.28%); rus: 513 (0.04%); ukr: 212 (0.11%).
- eng/spa/fra: 0 dropped (unchanged). Total sentences now 7,009,200.
- Links directed: 7,066,718 (was 7,075,436; pairs whose sentences were
  dropped are gone). Words: 272,766 (was 272,893; derived from sentences).

### Verification
- Filter function: 3 profane words caught per new language (18/18),
  3 ordinary words pass per new language (18/18),
  eng/spa/fra regression (fuck/mierda/merde) still caught. ALL OK.
- Both rebuilds produced byte-identical per-language counts.

## Step 9 - grammar-topic drilling (2026-09-26, v0.5.0)

"Test me on past tense": build-time spaCy tagging + authenticated
GET /drill?learner_id=&lang=&topic= serving up to 10 tense drills from
the existing exercise builders. Grading, FSRS cards, and /progress are
unchanged (standard deterministic exercise IDs).

### Tagger: data/tag_grammar.py (new, build-time only)
- Rule: any VERB/AUX with Tense=Past/Pres/Fut -> past-tense /
  present-tense / future-tense. A sentence can carry several topics.
- de/nl heuristic (their spoken past/future are analytic, the finite aux
  alone reads Tense=Pres): haben/sein + participle -> past-tense;
  werden/zullen + infinitive -> future-tense. Verified on samples
  ("Er wird gehen." future; "Ich habe das schon gemacht." past).
- Pool: the 2,000-sentence audio lesson pool per language (length 8-140,
  has an English translation, easiest first); expands to 20,000 when any
  topic has < 200 sentences (all 7 languages expanded).
- Output: sentence_topics(sentence_id, topic). Idempotent per language
  (delete-then-insert). NOTE: build_content.py unlinks epea.db on
  rebuild, so re-run tag_grammar.py after any content rebuild
  (sentence ids are deterministic, tags re-attach cleanly).

### License finding: spaCy models are NOT all MIT
The PLAN.md survey claim was wrong. Verified from each installed
package's METADATA (3.8.0): es GPL-3.0, fr LGPL-LR, de MIT, pt MIT,
ru MIT, uk MIT, nl CC BY-SA 4.0, it CC BY-NC-SA 3.0.
- Treatment follows the Piper GPL-3.0 precedent (step 4): models are
  build-time tools only, in tools/.venv, never in api/requirements.txt,
  never in the runtime venv (verified: `import spacy` fails there),
  never in the Docker image. Tags are program output (facts we computed).
- it_core_news_sm is CC BY-NC-SA 3.0: non-commercial is blocked
  regardless of posture, so Italian has NO usable model and is SKIPPED
  (never fake tags). /drill?lang=it returns 400 "not available".
  A Stanza-based Italian tagger (Apache-2.0 models) is a possible
  follow-up; not built.
- All 7 usable models recorded in sources as spacy-model-<name>
  (share_alike=1 only for nl CC BY-SA 4.0, same mapping as
  build_content.py).

### Tag counts (pool=20,000 per language; 92,503 topic rows total)
- de: past 3612 / pres 12327 / fut 128
- es: past 2820 / pres 10468 / fut 506
- fr: past 3378 / pres 9876 / fut 328
- nl: past 3841 / pres 13508 / fut 160
- pt: past 3604 / pres 11327 / fut 383
- ru: past 4116 / pres 2890 / fut 790
- uk: past 3831 / pres 3888 / fut 722
All 21 language/topic pairs >= 50 sentences (min: de future 128), so all
are served. Note: sm models are noisy on very short sentences
(e.g. de "Fang an!" tagged past); drills reflect the tags as stored.

### API: GET /drill (v0.5.0)
- Validates lang (400), topic (400), learner (404); requires the
  X-API-Key (401 without). Pairs with < 50 tagged sentences -> 400
  "not available yet".
- Builds up to 10 exercises round-robin mc/tr/cl from tagged sentences
  via build_exercise + public() (no answer leaks). Translation
  exercises use only English sentences whose EVERY dlang translation
  carries the topic tag (grading accepts any linked translation, so
  this keeps drills tense-clean).
- Same response shape as /lesson/next plus "topic".

### Verification (local server, port 8124)
- DB: all 10 drill exercises (de past-tense) verified tense-clean
  against sentence_topics (mc/cl sids tagged; tr's German translation
  "Er ging." tagged).
- Grading: POST /grade on drill IDs works; /progress shows
  exercises_done=2, accuracy 0.5, due_now=1 (FSRS unified).
- Errors: bad topic 400, bad lang 400, it/past-tense 400 (0 tags),
  no key 401, unknown learner 404.
- Regression: es/fr /lesson/next return 5 exercises (mc/tr/cl);
  /audio 200 for es, es-mx, fr, de; bad voice 400.

## Step 10 - Italian grammar tagging with Stanza (2026-09-26, v0.5.0, no bump)

Italian drills are live: /drill?lang=it now serves past-tense,
present-tense, and future-tense (was 400 "not available"). No API
contract change, so the version stays 0.5.0; only the /drill
description string was updated (taggers are spaCy, Stanza for Italian).

### License verification (done FIRST, before any tagging)
- stanza 1.14.0 + torch 2.14.0+cpu installed into tools/.venv only.
  Verified absent from the runtime api venv (`import stanza/spacy/torch`
  all fail there) and absent from api/requirements.txt.
- stanza-it model license: the publisher's own model card on
  huggingface.co/stanfordnlp/stanza-it (the exact repo stanza 1.14
  downloads from) declares `license: apache-2.0` in its frontmatter
  (confirmed via the README and the HF API). The downloaded .pt
  checkpoints were inspected and carry no separate license file; the
  model card is the publisher's license statement on the artifact.
  Apache-2.0 is clean under our posture. Same build-time-only
  treatment as the spaCy models (Piper precedent).
- Recorded in sources as stanza-model-it (Apache-2.0, share_alike=0).

### Tagger: data/tag_grammar.py gains a Stanza path
- STANZA_LANGS = {"it": ("ita", "stanfordnlp/stanza-it", "Apache-2.0")};
  tag_language_stanza() mirrors tag_language() (same pool rule, same
  delete-then-insert idempotency, same sentence_topics table).
- Rule: any VERB/AUX with Tense=Past/Pres/Fut -> topic. Italian needs
  NO compound-tense heuristic: in UD Italian the past participle
  itself carries Tense=Past (verified on samples: "Ho mangiato" ->
  participle Tense=Past; "Mangerò"/"Verrà" -> Tense=Fut; works with
  both avere and essere auxiliaries).
- Pool: 2,000 -> auto-expanded to 20,000 (all topics < 200 at 2,000).
  Tagging 20k sentences took ~10 min on CPU.
- Models live in tools/stanza_resources (STANZA_RESOURCES_DIR set in
  the script; 183 MB). Never in the runtime.

### Tag counts (it, pool=20,000; 17,757 topic rows)
- it: past 2905 / pres 13430 / fut 1422
All three >= 50, so all are served. Total sentence_topics is now
110,260 (92,503 + 17,757); the other 7 languages' tags are untouched.
Spot checks: "Lo vidi." / "È morta." past; "Tornerà." / "Lo farò." /
"Mangerò." future.

### Verification (local server, port 8125, throwaway key, shut down after)
- /drill it/past-tense, it/present-tense, it/future-tense: 200, 10
  exercises each, 30/30 verified tense-clean against sentence_topics
  (mc/cl sentence ids tagged; tr exercises' every Italian translation
  tagged).
- Grading: two POST /grade on drill exercises (wrong answers) -> 200,
  /progress shows exercises_done=2, accuracy 0.0, due_now=2 (FSRS
  unified, same as step 9).
- Errors: topic=bogus 400, lang=xx 400, no key 401, unknown learner 404.
- Regression: /drill de/past-tense 200 (10 exercises); /lesson/next it
  200 (5 exercises).
- PRAGMA integrity_check: ok.

### Environment notes for future build runs
- stanza/huggingface_hub download breaks on this VM unless
  no_proxy/NO_PROXY are sanitized: the stock no_proxy list contains
  bare IPv6 entries (fd8b:4f84:7d32:99::1) that crash httpx2's vendored
  URL parser (InvalidURL "Invalid port: ':1]'"). Override with
  no_proxy=localhost,127.0.0.1 for downloads.
- /tmp is a 512 MB tmpfs and was full (stale 479 MB step-9 backup DB);
  cleaned. Model downloads go to tools/stanza_resources, not /tmp.

## Public language listing (2026-09-25)
Jeremiah: "Somewhere we need to mention all languages covered." Added the
8-language list (Spanish, French, Italian, German, Brazilian Portuguese,
Russian, Ukrainian, Dutch) to the two public surfaces: the OpenAPI
description and the /health response. /languages stays key-protected with
per-language detail. Syntax-checked.

## Deploy prep (2026-09-25)
Jeremiah: "Yes, start deploy prep." Prepared the epea-api repo locally
(Trophe playbook: one repo + one Cloud Run service per connector).
- Dockerfile: python:3.12-slim, reassembles epea.db from data/dist/
  chunks at build time (cat), COPYs data/audio/ (18,000 MP3s, 179 MB).
  Runtime deps stay FastAPI + Uvicorn + FSRS. EPEA_API_KEY from env.
- DB chunked: 13 x 90 MB parts under data/dist/ + SHA256SUMS.
  Reassembly verified byte-identical (sha256 match with data/epea.db).
- Repo layout: api/, data/dist/, data/audio/, scripts/ (build provenance),
  docs-privacy.html / docs-terms.html (standalone copies of the approved
  v1.0 pages), LICENSE (content-source attribution; code license terms
  to be finalized by Jeremiah before publication), README.md, DEPLOY.md
  (exact gcloud commands), official logos.
- Committed locally as a711a1b (18,038 files). NOT pushed: needs
  Jeremiah's GitHub token + repo creation (dexiadigi-cloud/epea-api).
- Still needs Jeremiah: GCP deploy credential, production API key,
  connector registration + skill, Meta submission (after prod testing).
