#!/usr/bin/env bash
# Turn a directory of built packages into a pacman repository named "twinfi".
# Used by .github/workflows/release.yml before uploading to the arch-repo release.
#
#   ci-repo.sh <dir>
#
# If GPG_KEY_ID is set, packages and the database are signed with that key
# and its public key is exported as twinfi.asc for users to import.
# The database is also published as apsta.db (the project's name until 0.13).
set -euo pipefail

cd "${1:?usage: ci-repo.sh <dir>}"
shopt -s nullglob
pkgs=(*.pkg.tar.zst)
if (( ${#pkgs[@]} == 0 )); then
    echo "no packages in $PWD" >&2
    exit 1
fi

sign=()
if [[ -n ${GPG_KEY_ID:-} ]]; then
    for pkg in "${pkgs[@]}"; do
        gpg --batch --yes --detach-sign --no-armor --local-user "$GPG_KEY_ID" "$pkg"
    done
    sign=(--sign --key "$GPG_KEY_ID")
    gpg --export --armor "$GPG_KEY_ID" > twinfi.asc
fi

rm -f twinfi.db* twinfi.files*
repo-add "${sign[@]}" twinfi.db.tar.gz "${pkgs[@]}"

# repo-add makes twinfi.db -> twinfi.db.tar.gz symlinks; release assets must be files.
for link in twinfi.db twinfi.files twinfi.db.sig twinfi.files.sig; do
    if [[ -L $link ]]; then
        cp --remove-destination "$(readlink -f "$link")" "$link"
    fi
done

# pacman.conf files from before the rename say [apsta], and pacman fetches
# <section>.db: serve the same database under that name too.
for f in twinfi.db twinfi.files twinfi.db.sig twinfi.files.sig; do
    if [[ -e $f ]]; then
        cp "$f" "apsta${f#twinfi}"
    fi
done
ls -1
