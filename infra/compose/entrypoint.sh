#!/bin/sh
# Migrate, then serve. The image's default command.
#
# Migrations run on every start rather than in a separate step somebody has to
# remember. The schema guard refuses to run against a database at the wrong
# revision — deliberately — so without this an upgraded image comes up only to
# fail with a message nobody is watching for.
#
# `exec`, so uvicorn becomes PID 1 and a `docker stop` reaches it. Without that
# the shell swallows SIGTERM and every stop is a ten-second timeout.

set -e

alembic upgrade head

exec uvicorn app.api.main:app \
    --host 0.0.0.0 \
    --port "${FINSTONE_PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips "${FINSTONE_FORWARDED_FOR:-*}"
