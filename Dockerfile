# Epea API — production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Application code
WORKDIR /srv/epea/api
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ .

# The SQLite corpus ships as <100MB chunks (GitHub single-file limit);
# reassemble here. The app resolves it as ../data/epea.db relative to
# api/ (see DB_PATH in app.py). Pronunciation audio ships as regular
# files under ../data/audio (see AUDIO_DIR in app.py).
COPY data/dist/ /tmp/dist/
RUN mkdir -p ../data \
 && cat /tmp/dist/epea.db.part-* > ../data/epea.db \
 && rm -rf /tmp/dist
COPY data/audio/ ../data/audio/

EXPOSE 8000

# EPEA_API_KEY must be set in the host environment.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
