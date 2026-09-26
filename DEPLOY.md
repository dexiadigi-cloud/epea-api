# Epea deploy runbook

Target: Cloud Run service `epea`, project `winter-pivot-458405-a7`,
region `us-west1`. Repo: `dexiadigi-cloud/epea-api` (private).

## Prerequisites (Jeremiah provides)

- GCP deploy credential (service-account key or equivalent)
- GitHub token with repo scope (single-use, then revoked)

Neither is stored in chat, files, or memory.

## 1. Create the repo and push

```bash
cd ~/workspace/epea
git init -b main   # if not already initialized
git add -A
git commit -m "Epea v1: FastAPI + 8-language corpus"
gh repo create dexiadigi-cloud/epea-api --private --source=. --push
# or: git remote add origin https://github.com/dexiadigi-cloud/epea-api.git
#     git push -u origin main
```

Verify: all `data/dist/epea.db.part-*` chunks present on GitHub;
re-clone elsewhere and check `cat` reassembly against SHA256SUMS.

## 2. Deploy to Cloud Run

```bash
gcloud auth activate-service-account --key-file=<key.json>  # or user auth
gcloud config set project winter-pivot-458405-a7
gcloud config set run/region us-west1

gcloud run deploy epea \
  --source . \
  --region us-west1 \
  --allow-unauthenticated \
  --set-env-vars EPEA_API_KEY=<production-key> \
  --memory 2Gi --cpu 1 --timeout 300
```

Notes:

- Image is ~1.4GB (1.1GB DB + 179MB audio). Build takes several minutes.
- First request warms the DB (~21s startup observed locally); the
  `--timeout 300` covers it.
- `--allow-unauthenticated` matches Phos/Trophe: the API enforces its
  own `X-API-Key` auth per endpoint.

## 3. Post-deploy verification

- `GET /health` - version, sentence/pair/word counts, language list
- `GET /languages` with key - 8 languages
- Exercise flow: `/lesson/next`, `/grade`, `/progress`
- `/drill?lang=it&topic=past-tense` with key
- `/audio/<clip>.mp3` with key
- Live-health-check Phos and Trophe afterward (isolation rule)

## 4. Connector

- New production credential (one per API - never reuse)
- Skill at `~/workspace/skills/epea/`
- Register in the Muse app, end-to-end CLI test
- Add to `~/workspace/apis/REGISTRY.md`

## 5. Meta submission

Privacy policy and terms v1.0 approved 2026-09-25 (effective date on
pages). Served at `/privacy` and `/terms`; standalone copies in
`docs-privacy.html` / `docs-terms.html`. Submit only after production
testing is green.
