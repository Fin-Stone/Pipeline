#!/usr/bin/env sh
# The version the commits since the last tag imply. Prints it on stdout;
# the reasoning goes to stderr so the script stays usable in a pipeline.
#
#   ./infra/scripts/next-version.sh            # what dev should be bumped to
#   ./infra/scripts/next-version.sh v0.2.0     # ...as if that were the last tag
#
# Used twice: by a human writing the chore(release) commit, and by release.yml
# to check that the number a human wrote matches what the commits say. Those
# two must never disagree quietly, which is why one script answers both.

set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

TYPES='feat|fix|patch|chore|docs|refactor|test|ci|build|perf|revert'

last_tag=${1:-$(git tag --list 'v*' --sort=-v:refname | head -n 1)}

if [ -n "$last_tag" ]; then
    current=${last_tag#v}
    range="$last_tag..HEAD"
else
    # No tags yet, so the floor is whatever pyproject says and every commit
    # counts. Reading it here rather than assuming 0.1.0 means the first
    # release does not silently reset the number.
    current=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
    range=HEAD
    printf 'no v* tag yet; starting from pyproject version %s\n' "$current" >&2
fi

major=$(printf '%s' "$current" | cut -d. -f1)
minor=$(printf '%s' "$current" | cut -d. -f2)
patch=$(printf '%s' "$current" | cut -d. -f3)

subjects=$(git log --format=%s "$range")
bodies=$(git log --format=%B "$range")

breaking=no
feature=no
# A trailing ! on the type, or the footer. Both are Conventional Commits;
# "breaking" is not a type, which is why neither form looks for one.
printf '%s' "$subjects" | grep -Eq "^($TYPES)(\([^)]*\))?!:" && breaking=yes
printf '%s' "$bodies"   | grep -Eq '^BREAKING[ -]CHANGE:'     && breaking=yes
printf '%s' "$subjects" | grep -Eq '^feat(\([^)]*\))?!?:'     && feature=yes

if [ "$major" -eq 0 ]; then
    # Below 1.0 the minor is the compatibility signal, so a breaking change
    # and a feature both move it. They are indistinguishable in the number
    # until 1.0 — the changelog is where the difference is recorded.
    if [ "$breaking" = yes ] || [ "$feature" = yes ]; then
        minor=$((minor + 1)); patch=0; reason='breaking change or feature, pre-1.0'
    else
        patch=$((patch + 1)); reason='fixes and chores only'
    fi
elif [ "$breaking" = yes ]; then
    major=$((major + 1)); minor=0; patch=0; reason='breaking change'
elif [ "$feature" = yes ]; then
    minor=$((minor + 1)); patch=0; reason='feature'
else
    patch=$((patch + 1)); reason='fixes and chores only'
fi

count=$(printf '%s' "$subjects" | grep -c '' || true)
printf '%s commits since %s: %s\n' "$count" "${last_tag:-the start}" "$reason" >&2
printf '%s.%s.%s\n' "$major" "$minor" "$patch"
