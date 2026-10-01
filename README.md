# Epea API

☕ [Support on Ko-fi](https://ko-fi.com/dexiadigi)

Epea (Greek epea, "words" - Homer's "winged words") is a Duolingo-style
language-learning API: conversation-as-interface, exercises, spaced
repetition, pronunciation audio, and grammar drilling across 8 languages.

**Languages:** Spanish, French, Italian, German, Brazilian Portuguese,
Russian, Ukrainian, Dutch. English is the base language.

**Auth:** `X-API-Key` header on every endpoint except `/health`,
`/privacy`, and `/terms`.

## Run locally

```bash
cd api
EPEA_API_KEY=<key> ../.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8123
```

The API expects `../data/epea.db` and `../data/audio/` relative to `api/`
(see `DB_PATH` / `AUDIO_DIR` in `api/app.py`).

## Deploy (Cloud Run)

See `DEPLOY.md` for the exact commands. Summary:

1. The SQLite corpus ships as `<100MB` chunks under `data/dist/`
   (GitHub single-file limit); the Dockerfile reassembles them at build
   time with `cat`.
2. `EPEA_API_KEY` is injected as an environment variable in Cloud Run -
   never committed.
3. One repo, one Cloud Run service, one production credential, one skill
   per connector (project isolation rule).

## Layout

- `api/app.py` - FastAPI application
- `api/requirements.txt` - runtime deps (FastAPI, Uvicorn, FSRS only)
- `api/privacy.html`, `api/terms.html` - legal pages (also served at
  `/privacy` and `/terms`); `docs-privacy.html` / `docs-terms.html` are
  standalone copies for submission forms
- `data/dist/epea.db.part-*` - database chunks (+ SHA256SUMS)
- `data/audio/` - 18,000 pronunciation clips (MP3)
- `scripts/` - build provenance (content, audio, grammar tagging)
- `epea-logo-official.png` (1600px), `epea-logo-512.png` (512px)

## License

See `LICENSE`. Content-source licenses and attribution live in the
database's `sources` table; the code license is finalized before
publication.
