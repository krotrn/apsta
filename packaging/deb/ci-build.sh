#!/usr/bin/env bash
# Build Ubuntu/Debian packages into packaging/deb/out/.
# Used by .github/workflows/packages.yml; also runs in an ubuntu container.
#
#   ci-build.sh binary   .deb from git HEAD (PR / main checks, release asset)
#   ci-build.sh source   source package for the Launchpad PPA
#
# `source` signs the .changes when DEBSIGN_KEY is set. If the key has a
# passphrase, put it in the file named by DEBSIGN_PASSPHRASE_FILE.
set -euo pipefail

mode=${1:?usage: ci-build.sh binary|source}
here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
out="$here/out"

version=$(dpkg-parsechangelog -l "$root/debian/changelog" -SVersion)
upver=${version%-*}
if [[ ${GITHUB_REF_NAME:-} == v* && $GITHUB_REF_NAME != "v$upver" ]]; then
    echo "Tag $GITHUB_REF_NAME does not match debian/changelog $version (run scripts/bump_version.py)" >&2
    exit 1
fi

rm -rf "$out"
mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$root" archive --prefix="apsta-$upver/" HEAD | tar -x -C "$work"

case $mode in
    binary)
        (cd "$work/apsta-$upver" && dpkg-buildpackage -us -uc -b)
        cp "$work"/*.deb "$out/"
        ;;
    source)
        # Upstream tarball = the tree minus debian/, so quilt sees no stray changes.
        git -C "$root" archive --prefix="apsta-$upver/" -o "$work/apsta_$upver.orig.tar.gz" HEAD -- . ':(exclude)debian'
        (cd "$work/apsta-$upver" && dpkg-buildpackage -S -sa -us -uc)
        if [[ -n ${DEBSIGN_KEY:-} ]]; then
            signer=(gpg --batch --pinentry-mode loopback)
            if [[ -n ${DEBSIGN_PASSPHRASE_FILE:-} ]]; then
                signer+=(--passphrase-file "$DEBSIGN_PASSPHRASE_FILE")
            fi
            debsign -p"${signer[*]}" -k"$DEBSIGN_KEY" "$work"/apsta_*_source.changes
        fi
        cp "$work"/apsta_* "$out/"
        ;;
    *)
        echo "unknown mode: $mode" >&2
        exit 2
        ;;
esac
ls -1 "$out"
