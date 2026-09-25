#!/usr/bin/env bash
# Build the Arch package into packaging/arch/out/ (PKGBUILD, .SRCINFO, *.pkg.tar.zst).
# Used by .github/workflows/arch.yml; also runs locally as a non-root user.
#
#   ci-build.sh release   build from the GitHub tag tarball and pin its sha256
#                         (the out/ PKGBUILD + .SRCINFO are what goes to the AUR)
#   ci-build.sh local     build from the current git HEAD (PR / main checks)
#
# Extra makepkg flags can be passed via MAKEPKG_FLAGS (e.g. --nodeps).
set -euo pipefail

mode=${1:?usage: ci-build.sh release|local}
here=$(cd "$(dirname "$0")" && pwd)
out="$here/out"

rm -rf "$out"
mkdir -p "$out"
cp "$here/PKGBUILD" "$here/apsta.install" "$out/"
cd "$out"

# shellcheck source=/dev/null
pkgver=$(source ./PKGBUILD && echo "$pkgver")

case $mode in
    release)
        tag=${GITHUB_REF_NAME:-v$pkgver}
        if [[ $tag != "v$pkgver" ]]; then
            echo "Tag $tag does not match PKGBUILD pkgver $pkgver (run scripts/bump_version.py)" >&2
            exit 1
        fi
        updpkgsums
        ;;
    local)
        # archive from the repo root: inside a subdirectory git archives only that subtree
        root=$(git -C "$here" rev-parse --show-toplevel)
        git -C "$root" archive --prefix="apsta-$pkgver/" -o "$out/apsta-$pkgver.tar.gz" HEAD
        sed -i "s|^source=.*|source=(\"apsta-$pkgver.tar.gz\")|" PKGBUILD
        ;;
    *)
        echo "unknown mode: $mode" >&2
        exit 2
        ;;
esac

# shellcheck disable=SC2086  # MAKEPKG_FLAGS is intentionally word-split
makepkg --force --noconfirm ${MAKEPKG_FLAGS:-}
makepkg --printsrcinfo > .SRCINFO
ls -1 "$out"/*.pkg.tar.zst
