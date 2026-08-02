# Pinned to 3.12 rather than tracking latest: pdfplumber and psycopg wheel
# availability on 3.14 is still patchy, and the parser is the part of this
# system least worth debugging against a moving toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/finstone

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./

RUN pip install --no-cache-dir ".[postgres]"

RUN useradd --create-home --uid 10001 finstone \
    && mkdir -p /srv/finstone/data \
    && chown -R finstone:finstone /srv/finstone
USER finstone

CMD ["finstone", "status"]
