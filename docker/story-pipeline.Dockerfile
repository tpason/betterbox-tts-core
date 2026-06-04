FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY docker/story-pipeline-requirements.txt /tmp/story-pipeline-requirements.txt
RUN pip install -r /tmp/story-pipeline-requirements.txt \
    && playwright install chromium --with-deps

CMD ["python", "-m", "story_db.story_pipeline_db.cli", "--help"]
