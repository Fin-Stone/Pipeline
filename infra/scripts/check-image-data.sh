#!/usr/bin/env sh
# Reject a container image that carries real financial data.
#
#   docker run --rm --user 0 --entrypoint sh <image> -c 'find / ...' \
#     | check-image-data.sh
#
# Reads a filesystem listing on stdin, one path per line, and fails if anything
# that should have been left outside the build context is in it.
#
# .dockerignore already excludes uploads/, data/, category/ and the
# proposal/residual dumps, with a comment naming this exact threat. Nothing
# verified it held. A later negation, or a restructure that moved real data
# outside those prefixes, would start baking statements into a published layer
# — first noticed as a public image, which is the one place a leak cannot be
# taken back from.
#
# A script rather than inline workflow yaml so that the logic can be run
# against a captured listing by hand, and tested without a Docker daemon. The
# first version of this check was inline, and shipped two bugs that a single
# test against a planted path would have caught.

set -eu

listing=$(cat)

# The vacuous pass is the failure mode this whole file exists to prevent, so it
# is the first thing tested. An empty or truncated listing satisfies every
# assertion below, reports success, and means nothing — and that is not
# hypothetical here: the inline version ran `find` as the image's own
# unprivileged user, which cannot read every directory, so it exited non-zero
# with its stderr discarded and killed the step before any assertion ran.
#
# The image carries a full CPython install, so its real listing is tens of
# thousands of paths. A hundred is a floor no working inspection can fall
# below and no broken one can reach.
count=$(printf '%s\n' "$listing" | grep -c . || true)
if [ "$count" -lt 100 ]; then
    echo "::error::the image listing has ${count} entries, so the inspection did"
    echo "::error::not run. A pass would mean nothing, so this fails instead."
    exit 1
fi

status=0

# /srv/finstone is the Dockerfile's WORKDIR. The root-level paths cost nothing
# and cover the install root moving.
#
# The Dockerfile creates data/ and uploads/ deliberately, empty, as the mount
# points for the volume and the statement drop. So this asserts they have no
# *contents*, not that they are absent — `^dir/.+` matches an entry inside the
# directory and never the directory itself.
for dir in \
    /srv/finstone/uploads /srv/finstone/data /srv/finstone/category \
    /uploads /data /category
do
    if printf '%s\n' "$listing" | grep -qE "^${dir}/.+"; then
        echo "::error::${dir} has contents in the image. Check .dockerignore."
        printf '%s\n' "$listing" | grep -E "^${dir}/.+" | head -n 20
        status=1
    fi
done

# Unanchored, because .dockerignore names these by pattern rather than by
# location: a dump could land wherever the build context is copied to.
if printf '%s\n' "$listing" | grep -qE '/(proposal|residual)[^/]*\.json$'; then
    echo "::error::a proposal or residual dump is in the image. Check .dockerignore."
    printf '%s\n' "$listing" | grep -E '/(proposal|residual)[^/]*\.json$' | head -n 20
    status=1
fi

if [ "$status" -eq 0 ]; then
    printf 'no real data in the image (%s paths inspected)\n' "$count"
fi
exit "$status"
