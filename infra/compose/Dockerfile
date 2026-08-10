# One image: the API, the client it serves, and the CLI that fills it.
#
# One container is the shape a self-hoster expects — `docker run`, a volume, a
# port — and the difference between that and a compose file with three services
# is most of what makes a thing feel installable. It also removes CORS from the
# default path entirely: served from one origin, the browser has nothing to ask
# permission for.
#
# Nothing is baked in. No API address is compiled into the client at any stage;
# it reads the origin it was served from at runtime and the user can still point
# it elsewhere. Architecture §5.2 survives intact.

# --------------------------------------------------------------- the client ---
FROM node:22-alpine AS client
WORKDIR /build

# Dependencies first, so a source change does not re-resolve the tree.
COPY ui/pwa/package.json ui/pwa/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY ui/pwa/ ./
RUN npm run build


# --------------------------------------------------------------- the server ---
# Pinned to 3.12 rather than tracking latest: pdfplumber and psycopg wheel
# availability on 3.14 is still patchy, and the parser is the part of this
# system least worth debugging against a moving toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Defaults, so `docker run` with no environment at all is a working install.
# SQLite in the data volume; point DATABASE_URL at Postgres to move.
ENV FINSTONE_DATA_DIR=/srv/finstone/data \
    FINSTONE_UPLOADS_DIR=/srv/finstone/uploads \
    FINSTONE_WEB_ROOT=/srv/finstone/web
# Deliberately absent: FINSTONE_ALLOW_PROD. Processing real statements is an
# explicit act each time — see the development rules in README.md, Rule 2.

WORKDIR /srv/finstone

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./

# Both extras: one image serves the API and runs the CLI, so it needs the web
# server as well as the Postgres driver. Splitting them would mean two images
# that must be upgraded together, which is a worse trade than a few megabytes.
RUN pip install --no-cache-dir ".[api,postgres]"

COPY --from=client /build/dist ./web
COPY infra/compose/entrypoint.sh /usr/local/bin/finstone-server
RUN chmod +x /usr/local/bin/finstone-server

RUN useradd --create-home --uid 10001 finstone \
    && mkdir -p /srv/finstone/data /srv/finstone/uploads \
    && chown -R finstone:finstone /srv/finstone
USER finstone

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/v1/health')"

# CMD rather than ENTRYPOINT, so `docker compose run … finstone backup` and
# `docker run … finstone status` replace it outright instead of having to argue
# with it.
CMD ["/usr/local/bin/finstone-server"]
