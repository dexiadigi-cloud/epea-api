# Language API — Plan v0.1

Date: 2026-09-25. Status: RESEARCH PLAN ONLY. No code written, nothing built.
Working title: `language-api`. Final name is Jeremiah's call (same naming loop as Trophe).
Pattern: same as Phos and Trophe — FastAPI, single API-key auth (`X-API-Key`), Cloud Run (us-west1), one private GitHub repo, OpenAPI spec, privacy/terms pages, stored Muse connector credential.

Verdict legend used below: **CLEAN** (use freely) / **ATTRIBUTION** (use with credit notice) / **SHARE-ALIKE** (needs Jeremiah's explicit call) / **BLOCKED** (do not use).

---

## 1. Concept: "Duolingo inside Muse"

Key insight: Muse's conversation IS the interface. A normal language app needs its own UI, notification system, and engagement loops. Here, the API supplies the structured learning machinery (curriculum content, exercise generation, grading, spaced-repetition scheduling, audio) and Muse delivers it conversationally as a tutor. There is no app to build.

### How a lesson flows through chat

1. Learner says: "Teach me Spanish for 10 minutes."
2. Muse calls `GET /lesson/next?learner_id=...&language=es`. The API consults the learner's FSRS review state, picks due reviews plus a few new items, and returns a small lesson payload: 5-8 exercises with prompts, options, and audio clip URLs.
3. Muse presents each exercise in chat, in its own tutor voice ("What does 'la manzana' mean?" or plays the audio: "Type what you hear").
4. The learner answers in plain chat text.
5. Muse calls `POST /grade` with the learner's response. The API returns correct/incorrect, a targeted explanation, and updated SRS parameters, which it persists against the learner ID.
6. Muse delivers the feedback conversationally and moves on. At the end: a two-line progress summary (streak, words seen, accuracy).

### Who owns what

| Layer | Owner |
|---|---|
| Curriculum content, exercise generation, grading logic | API |
| Spaced-repetition scheduling and per-learner memory | API |
| Pre-generated pronunciation audio (static files) | API |
| Tutor persona, conversation flow, encouragement, explanations in the learner's words | Muse |
| Storing the learner's opaque ID between sessions | Muse (memory) |

The API never needs to know who the human is. It knows an opaque learner ID and a review history.

---

## 2. Open-source component survey

Every license below was verified against the project's repo LICENSE file, package metadata, or official docs (2026-09-25). Anything unverifiable is marked.

### 2a. Sentence / translation corpora (exercise raw material)

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| Tatoeba | CC BY 2.0 FR (sentence text; audio per-contributor) | ATTRIBUTION | Primary sentence bank for translation + cloze exercises |
| Leipzig Corpora Collection | CC BY 4.0 | ATTRIBUTION | Monolingual sentences, 200+ languages (scrambled, sentence-level only) |
| ParaCrawl | CC0 | CLEAN | Parallel sentences, EN + EU languages (noisy, filter before use) |
| OpenSubtitles (via OPUS) | attribution to OpenSubtitles required | ATTRIBUTION | Colloquial dialogue sentences (profanity filter needed) |
| NLLB / CCMatrix | ODC-BY 1.0 | ATTRIBUTION | Mined parallel sentences incl. lower-resource languages |
| OPUS (general) | varies per corpus (some public domain, some BY-SA) | check each | Only pick public-domain or CC-BY sub-corpora |
| Wikipedia / Wikimedia dumps | CC BY-SA 4.0 | SHARE-ALIKE | Reading passages (needs Jeremiah's call) |
| Mozilla Common Voice (sentences + audio) | CC0 | CLEAN | Listening-exercise sentences and real human audio |

### 2b. Vocabulary

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| Princeton WordNet (English) | custom permissive (commercial explicitly allowed, retain notice) | ATTRIBUTION | Definitions, synonyms, relations for English-side exercises |
| Open Multilingual WordNet (ES, FR) | CC BY | ATTRIBUTION | Same for Spanish and French |
| Open Multilingual WordNet (PT, PL, NL, TH, RO) | CC BY-SA | SHARE-ALIKE | Only if Jeremiah approves SA |
| wordfreq (data files) | CC BY-SA 4.0 | SHARE-ALIKE | Frequency ordering of vocabulary |
| SUBTLEX | no formal license; informal email permission to one author | caution | Prefer wordfreq path or seek written clearance |
| Kaikki / Wiktionary-derived (wiktextract) | CC BY-SA + GFDL (Wiktionary terms); tool itself MIT | SHARE-ALIKE | Inflections, IPA, translations (needs Jeremiah's call) |
| Words-CEFR-Dataset | MIT (levels derived from CEFR-J, attribute both) | ATTRIBUTION | CEFR-graded English word lists |
| CEFR-J profile | informal prose license ("free for research and commercial use with citation") | ATTRIBUTION + legal read | CEFR grading source |
| Octanove C1/C2 extension | CC BY-SA 4.0 | SHARE-ALIKE | Advanced levels (needs Jeremiah's call) |
| Oxford 3000/5000, Cambridge EVP | proprietary | BLOCKED | do not touch |
| Kelly lists, Academic Word List, LibreTexts books, Grimm Grammar | various NC | BLOCKED | non-commercial, do not touch |

### 2c. Grammar content (explanations and reference)

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| Tex's French Grammar (UT Austin) | CC BY | ATTRIBUTION | French grammar explanations + exercises |
| Brazilpod (UT Austin) | CC BY | ATTRIBUTION | Portuguese teaching materials (fast-follow language) |
| G-FOL German lexicon | CC BY | ATTRIBUTION | German vocabulary + usage sentences (fast-follow) |
| Français interactif (UT Austin/COERLL) | believed CC BY family | verify on site before use | 600-page French grammar + self-correcting exercises |
| Wikibooks language grammars | CC BY-SA 3.0/4.0 | SHARE-ALIKE | Coverage is broad but quality uneven; needs editorial review |
| Public-domain 19th-c. grammars (Internet Archive) | public domain | CLEAN but dated | Fallback only; language has shifted |

### 2d. NLP (exercise generation + answer checking)

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| spaCy (+ pretrained models) | MIT code; MODELS VARY (verified 2026-09-26 from installed METADATA: es GPL-3.0, fr LGPL-LR, de/pt/ru/uk MIT, nl CC BY-SA 4.0, it CC BY-NC-SA 3.0) | build-time tool only, never in runtime | Tokenize/POS/lemma/parse; cloze generation (POS masking), answer analysis |
| Stanza | Apache-2.0, models Apache-2.0 | CLEAN | Best multilingual morphology; avoid the optional CoreNLP wrapper (GPL-3) |
| Trankit | Apache-2.0 | CLEAN | Lightweight multilingual pipeline |
| simplemma | MIT code; linguistic data mixed (ODbL, CC BY 4.0) | ATTRIBUTION + flag ODbL to counsel | Cheap lemma lookup for grading |
| HuggingFace tokenizers/transformers | Apache-2.0 (libraries; check each model checkpoint) | CLEAN (library) | Inference plumbing |
| UDPipe | MPL-2.0 code, but pretrained models CC BY-NC-SA 4.0 | BLOCKED (models) | Only usable if we train our own models |

### 2e. TTS (pronunciation audio)

Strategy: PRE-GENERATE all audio at build time, serve static MP3s from the API. Program output is not a derivative work of the program's license, so the synthesis engine's license does not touch the served audio. The voice model's license is what matters.

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| Piper (rhasspy/piper, archived) | MIT | CLEAN | Build-time synthesis engine |
| Piper voices (rhasspy/piper-voices) | per-voice: mostly CC BY, some CC0 | ATTRIBUTION | Pick CC0/CC-BY voices; avoid Blizzard-licensed (non-commercial) voices |
| espeak-ng | GPL-3.0 | CLEAN server-side only | Phonemizer backend; too robotic for final audio |
| Coqui TTS code | MPL-2.0 | CLEAN | Alternative engine; use only Apache-2.0 checkpoints |
| Coqui XTTS-v2 weights | Coqui Public Model License (non-commercial incl. outputs) | BLOCKED | do not use |
| Mimic 3 | AGPL-3.0 (code), CC BY-SA 4.0 (voices) | BLOCKED | AGPL triggers on network use |
| Meta MMS TTS | CC BY-NC 4.0 | BLOCKED | non-commercial |

### 2f. ASR + pronunciation scoring (v2, not v1)

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| OpenAI Whisper (code + weights) | MIT | CLEAN | Learner speech transcription |
| faster-whisper | MIT | CLEAN | Production ASR inference |
| whisper.cpp | MIT | CLEAN | Native-binary ASR |
| Montreal Forced Aligner | MIT code; models CC BY 4.0 | ATTRIBUTION | Phoneme-level alignment for pronunciation scoring |
| SpeechBrain | Apache-2.0 | CLEAN | Scoring recipes (verify each recipe's model license) |
| wav2vec2 CTC checkpoints | Apache-2.0 (recheck card field before shipping) | CLEAN | Alignment/ASR alternative |
| Meta MMS ASR | CC BY-NC 4.0 | BLOCKED | non-commercial |

### 2g. Spaced repetition

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| py-fsrs | MIT | CLEAN | FSRS scheduler for the Python/FastAPI backend (recommended) |
| fsrs-rs | BSD-3-Clause | CLEAN | Rust FSRS (scheduler + optimizer), if a Rust service is ever wanted |
| fsrs-optimizer | BSD-3-Clause | CLEAN | Nightly per-learner parameter fitting |
| ts-fsrs | MIT | CLEAN | Only if scheduling ever moves client-side |
| SM-2 (algorithm concept) | published openly by Wozniak (1987); newer SM-17/18 proprietary | CLEAN (reimplement from the description) | Simple deterministic fallback |
| Ebisu | public domain | CLEAN | Auxiliary forgetting-curve model (academic, not a full scheduler) |
| Anki core / scheduler | AGPL-3.0-or-later | BLOCKED | Study as reference only; never embed in a hosted service |
| Mnemosyne | conflicting reports (AGPL vs GPL-2.0) | unverified, avoid | — |

### 2h. Grammar checking (feedback on learner writing)

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| LanguageTool | LGPL-2.1-or-later | CLEAN server-side | Multi-language proofreading; deploy as its own container. LGPL does not infect surrounding service code; share modifications if we fork it. |
| Harper | Apache-2.0 | CLEAN | Fast native/WASM English linting |
| GECToR (Grammarly) | Apache-2.0 code; some HF checkpoints NC | CLEAN code, verify checkpoints | High-throughput GEC engine |
| Gramformer | MIT (code; verify model weights) | CLEAN code | Lightweight fallback; unmaintained since ~2023 |

### 2i. Open Duolingo-clones / exercise engines

| Component | License (verified) | Verdict | Role |
|---|---|---|---|
| LibreLingo | AGPL-3.0 (code); course data per-course, default CC BY-SA 4.0 | code BLOCKED, CC BY-SA data usable with attribution | Re-implement its YAML skill format independently; borrow exercise-type ideas |
| OmniLingo | AGPL-3.0 (code); Common Voice data CC0 | code BLOCKED, CC0 data CLEAN | Listening-exercise concepts; CC0 audio/sentences |
| OpenLingo | MIT | CLEAN | Only fully permissive end-to-end language app found; architecture reference, code reusable |
| Anki ecosystem | core AGPL; add-ons any license; decks per-deck terms | core BLOCKED | Import/export compatibility target, not a building block |
| HF language-learning datasets | per-dataset data licenses | check each | Tatoeba/OPUS pairs usable; Duolingo SLAM 2018 is CC BY-NC (research only, BLOCKED for product) |

Honest gap: there is no mature open "exercise engine" library. The exercise layer (generation, distractors, difficulty sequencing) is where the proprietary product gets built. Borrow formats, build the engine.

### Recommended v1 stack (all CLEAN or ATTRIBUTION, no Jeremiah call needed)

| Need | Pick | License |
|---|---|---|
| Scheduling | py-fsrs | MIT |
| Grammar feedback | LanguageTool (own container) + Harper for English | LGPL-2.1 / Apache-2.0 |
| NLP / exercise generation | spaCy (MIT code; models vary per language, build-time only) | build-time tool |
| Lemma lookup for grading | simplemma | MIT (attribute data sources) |
| Audio (pre-generated static files) | Piper + CC0/CC-BY voices | engine MIT, voices CC |
| Sentences | Tatoeba + Leipzig + ParaCrawl | CC BY / CC BY / CC0 |
| Listening audio (human) | Mozilla Common Voice | CC0 |
| Vocab data | Princeton WordNet + OMW (ES/FR) + Words-CEFR | attribution |
| French grammar reference | Tex's French Grammar | CC BY |
| App architecture reference | OpenLingo | MIT |

---

## 3. License strategy — DECIDED 2026-09-25: Posture B
Jeremiah picked **Posture B**: allow CC BY-SA content with proper attribution
(the Phos precedent), unlocking Wiktionary inflections/IPA, Wikibooks grammars,
and wordfreq. Requirement: attribution must actually be done — SA content lives
in separately-marked tables from day one, attribution in the API docs/code.
Blocked regardless: all NC items, all AGPL in the service path,
Oxford/Cambridge lists.

Two postures, same as the Phos call (Phos allowed CC BY with attribution in docs/code):

**Posture A — clean only.** Ship with the recommended stack above: MIT/Apache/BSD/CC0/CC BY. Attribution notices live in the API docs and repo. Nothing share-alike enters the product.

**Posture B — also allow CC BY-SA with attribution.** Unlocks meaningfully better content: Kaikki/Wiktionary (inflections, IPA, translations for every language), Wikibooks grammars (broad coverage), wordfreq frequency data, Wikipedia-derived reading passages, Octanove C1/C2 lists, LibreLingo CC BY-SA course data. Cost: share-alike applies to the DATA. Keep SA-derived content in separately-marked tables/fields so a SA claim can never be argued to cover the proprietary exercise engine or API code. This is a data-hygiene rule, not just a legal nicety.

**Blocked regardless of posture:** anything NC (LibreTexts books, Kelly/AWL lists, Grimm Grammar, Meta MMS, UDPipe pretrained models, Coqui XTTS-v2), anything AGPL in the service path (LibreLingo code, OmniLingo code, Mimic 3, Anki core), Oxford/Cambridge proprietary lists.

**Needs a legal read before shipping either way:** SUBTLEX commercial terms (informal email permission does not clearly transfer), CEFR-J's prose license, simplemma's ODbL-sourced data, wav2vec2 checkpoint card field.

Recommendation: start Posture A for v1 (Spanish + French are fully servable on clean content), and let Posture B be the unlock for v2 breadth.

---

## 4. API sketch (Trophe-style, ~9 endpoints)

Auth: same pattern as Phos/Trophe — API key in `X-API-Key`, stored Muse connector credential. Read-mostly plus the small set of writes that learning requires (see section 5).

| # | Endpoint | Purpose |
|---|---|---|
| 1 | `GET /health` | Status, version, counts (languages, exercises, audio clips) |
| 2 | `GET /languages` | Supported languages, CEFR levels covered, voice availability |
| 3 | `POST /learners` | Issue an opaque learner UUID. No name, no email, no account. |
| 4 | `GET /lesson/next?learner_id=&language=` | FSRS-scheduled lesson: due reviews + new items, exercise payloads with audio URLs |
| 5 | `GET /exercise/{id}` | Single exercise detail (prompt, options, audio, hints) |
| 6 | `POST /grade` | `{learner_id, exercise_id, response}` → correct/incorrect, explanation, updated SRS state (persisted) |
| 7 | `GET /audio/{clip_id}.mp3` | Pre-generated Piper audio, static files |
| 8 | `GET /progress?learner_id=` | Streak, items seen/mastered, accuracy, CEFR estimate |
| 9 | `DELETE /learner/{id}` | Erase a learner's data (privacy story) |

Possible v2 additions: `/speak` (Whisper transcription + MFA pronunciation score), `/write` (LanguageTool feedback on free writing), `/import/anki` (deck import).

---

## 5. Hard design questions (flagged, not resolved)

### Q1. Per-learner progress persistence without user accounts

Our connectors so far are read-only and account-free. SRS needs durable per-learner state. Three options:

- **Option A — API-issued opaque learner IDs (recommended).** `POST /learners` returns a UUID; Muse stores it in the user's memory and passes it each call. No PII, no passwords, no accounts in any human sense. `DELETE /learner/{id}` gives a clean erasure story for the privacy page. Tradeoff: if the ID is lost, progress is lost; Muse must reliably persist one UUID per user per language.
- **Option B — fully stateless SRS.** Muse passes the complete review state with every call; the API stays pure and read-only. Tradeoff: FSRS state grows with vocabulary size; round-tripping kilobytes of state through chat is fragile and leaks implementation detail into the conversation.
- **Option C — client-supplied learner keys.** Muse derives a stable per-user key and the API stores against it. Tradeoff: key-ownership and privacy semantics get murky; no better than A in practice.

A keeps the "no accounts" promise while making SRS real. It does mean the API is no longer strictly read-only (grades and learner records are writes), which should be stated plainly in the privacy page.

### Q2. Grading translations without frustrating the learner

Exact-match grading fails on valid paraphrases ("the red apple" vs "a red apple"). v1 grading should be layered: normalization (case, punctuation, articles) → lemma-overlap via simplemma/spaCy → an accept-list of known-good variants per exercise (curated at build time). Anything below threshold comes back as "not quite" with the expected answer, never as a hard fail with no explanation. Embedding-similarity grading is a v2 research item, not v1.

### Q3. Curated vs generated curriculum

Two sources of exercises: curated (Tatoeba sentences hand-filtered and leveled at build time — high quality, slow) and generated (spaCy-driven cloze/multiple-choice from corpora — infinite, variable quality). v1 should be curated-first for the core path (every exercise a learner sees in the first 500 has been through a filter) with generated exercises filling the long tail of review. An LLM pass at build time can draft explanations and distractors, but a human-readable audit trail per exercise is worth keeping.

### Q4. Audio scale

Pre-generating audio for every sentence in two languages is a storage and build-time cost question, not a license question. Generate lazily: pre-generate the v1 curriculum audio at build time, generate-on-first-request with caching for the long tail. Cloud Run + Cloud Storage handles this; keep the runtime container free of TTS engines entirely.

---

## 6. V1 scope recommendation

### Languages: Spanish + French (2)

Chosen on open-resource quality, not just popularity:

- **Spanish:** deepest Tatoeba coverage, excellent Piper voices, strong spaCy/Stanza models, CC BY teaching materials (Brazilpod is Portuguese, but Spanish has comparable OER depth), OMW Spanish CC BY, largest US learner demand.
- **French:** same story — Tex's French Grammar (CC BY) is the single best openly-licensed grammar reference found, strong Piper voices, deep Tatoeba, OMW French CC BY.

Fast-follow: German, Portuguese (both CC BY-friendly: G-FOL, Brazilpod). Explicitly not v1: Japanese and Mandarin — large Tatoeba pools but materially harder tokenization, TTS voice quality variance, and writing-system exercises are a separate product surface.

### Exercise types in v1

1. Multiple choice (vocabulary, L1↔L2 both directions; distractors from same frequency band)
2. Translate L2→L1 and L1→L2 (layered grading per Q2)
3. Cloze / fill-in-the-blank (spaCy POS-masked Tatoeba sentences)
4. Listening: transcribe or multiple-choice on pre-generated Piper audio (+ Common Voice clips)
5. FSRS review queue mixing all of the above

### Explicitly OUT of v1

- Speaking / pronunciation scoring (Whisper + MFA — real compute cost, v2)
- Free-writing correction as a primary exercise (LanguageTool runs as feedback infra, not a v1 exercise type)
- Stories, leaderboards, XP economies, social features (Muse's conversation replaces the engagement layer)
- Handwriting / character exercises (matters for the non-v1 languages anyway)
- Offline mode (connector is inherently online)

---

## 7. Risks and open questions

- **Grading leniency vs correctness.** Too strict and learners quit; too loose and they learn wrong. The layered grading in Q2 needs real learner testing, not just unit tests.
- **Tatoeba quality variance.** Community sentences include errors, unnatural phrasing, and occasional profanity. Budget a filtering pass (length, vocab-level, blocklist, LanguageTool self-check) before anything is learner-facing.
- **BY-SA data hygiene.** If Posture B is ever adopted, SA-derived content must live in separately-marked tables from day one. Retrofitting separation is painful.
- **AGPL traps.** LibreLingo, OmniLingo, Mimic 3, and Anki core are all AGPL — useful as references, toxic as dependencies. No AGPL code in the repo, ever; add a license check to CI.
- **TTS voice quality per language.** Piper voices vary by language; audition the Spanish and French voices before committing the curriculum audio to them.
- **Cloud Run cold starts.** Keep the runtime container light: no TTS engines, no Whisper, no Java (LanguageTool) in the main service. LanguageTool runs as a separate service; everything heavy happens at build time.
- **Audio storage costs.** Small at v1 curriculum scale; recheck before the long-tail cache grows.
- **The "Duolingo" word.** Never in the name, tagline, or marketing. Working title `language-api` is a placeholder; the final name goes through the same clearance loop as Trophe.
- **Legal reads still owed:** SUBTLEX terms, CEFR-J prose license, simplemma ODbL data, wav2vec2 checkpoint field, Français interactif footer license.
- **Scope discipline.** The exercise engine is the product. Resist pulling in chatbots, stories, or gamification servers — Muse already does that job.

---

## Appendix: what greenlight would start (not started)

1. Jeremiah picks Posture A or B (section 3) and a final name.
2. Build-time pipeline: corpus filtering → exercise generation → Piper audio render → SQLite/Postgres load.
3. FastAPI service per the endpoint sketch, learner-ID persistence design per Q1.
4. Privacy/terms pages (v1.0, same pattern as Phos/Trophe), then Cloud Run deploy, connector registration, Meta submission.

No code has been written. Nothing has been downloaded. No money spent.
