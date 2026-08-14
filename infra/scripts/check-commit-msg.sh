#!/usr/bin/env sh
# Conventional-commit subject check. One grammar, three callers.
#
#   check-commit-msg.sh <file>      # the commit-msg hook passes a path
#   check-commit-msg.sh "subject"   # CI passes the PR title, or a log subject
#
# POSIX sh rather than bash, and no dependency on commitlint: this runs as a
# git hook on the maintainer's Windows box as well as on an Ubuntu runner, and
# a check that needs a node toolchain installed to police a Python repository
# is a check that stops being installed.
#
# Why the subject and not the body: `dev` takes squash merges, so the subject
# that lands in the branch history is the pull request title, and the version
# bump in next-version.sh is computed from those subjects. The body is where
# the prose belongs — see docs/contributing.md.

set -eu

TYPES='feat|fix|patch|chore|docs|refactor|test|ci|build|perf|revert'
MAX=72

if [ $# -lt 1 ]; then
    printf 'usage: %s <commit-msg-file|subject>\n' "$0" >&2
    exit 2
fi

if [ -f "$1" ]; then
    # Skip git's own comment lines and any leading blanks; the subject is the
    # first line that survives that.
    subject=$(grep -v '^#' "$1" | grep -v '^[[:space:]]*$' | head -n 1 || true)
else
    subject=$(printf '%s' "$1" | head -n 1)
fi

[ -n "$subject" ] && [ "${subject#\#}" = "$subject" ] || exit 0

# Generated or transient subjects nobody types by hand. `Merge ...` matters
# most: the dev -> main merge commit is written by GitHub, and rejecting it
# would block every release.
case "$subject" in
    'Merge '*|'Revert '*|'fixup! '*|'squash! '*|'amend! '*) exit 0 ;;
esac

fail() {
    printf '\nRejected commit subject:\n  %s\n\n%s\n\n' "$subject" "$1" >&2
    cat >&2 <<EOF
Expected:  <type>[optional scope][!]: <subject>
Types:     feat, fix, patch, chore, docs, refactor, test, ci, build, perf, revert
Breaking:  a trailing ! on the type (feat!: ...) or a BREAKING CHANGE: footer.
           "breaking" is not itself a type.

  feat(parsers): read the OCBC 360 interest line
  fix!: stop widening SUM(bigint) to numeric on Postgres

Keep the reasoning in the commit body, not the subject.
EOF
    exit 1
}

if [ "${#subject}" -gt "$MAX" ]; then
    fail "Subject is ${#subject} characters; the limit is $MAX."
fi

if ! printf '%s' "$subject" \
    | grep -Eq "^($TYPES)(\([a-z0-9._/-]+\))?!?: .+"; then
    fail 'No recognised type prefix.'
fi
