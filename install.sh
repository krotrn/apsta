#!/usr/bin/env bash
# Install apsta from a source checkout into /usr/local (for distros without a package).
#
#   sudo ./install.sh              install CLI, GUI, service files, polkit policy
#   sudo ./install.sh --uninstall  remove everything this script installed
#
# Prefer your distribution's package (see README) when one exists.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/usr/local}"
LIB="$PREFIX/lib/apsta"
BIN="$PREFIX/bin"

say() { printf '  %s  %s\n' "$1" "$2"; }

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh $*" >&2
    exit 1
fi

render() {  # render <data file>: point packaged paths at this prefix
    sed "s|/usr/bin/apsta|$BIN/apsta|g" "$1"
}

uninstall() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now apsta.service >/dev/null 2>&1 || true
    fi
    "$BIN/apsta" stop >/dev/null 2>&1 || true
    rm -rf "$LIB"
    rm -f "$BIN/apsta" "$BIN/apsta-gtk" \
          /etc/systemd/system/apsta.service \
          /usr/lib/systemd/system-sleep/apsta-sleep \
          /usr/share/polkit-1/actions/com.github.apsta.policy \
          /usr/share/applications/com.github.apsta.Gtk.desktop \
          /usr/share/metainfo/com.github.apsta.Gtk.metainfo.xml \
          /usr/share/icons/hicolor/scalable/apps/com.github.apsta.Gtk.svg \
          /usr/share/icons/hicolor/symbolic/apps/com.github.apsta.Gtk-symbolic.svg \
          /etc/bash_completion.d/apsta \
          /usr/local/share/zsh/site-functions/_apsta \
          /etc/fish/completions/apsta.fish
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || true
    fi
    say "✔" "apsta removed (configuration kept in /etc/apsta)"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

echo "Installing apsta into $PREFIX ..."

# Python packages go to a private directory, not onto the system site-packages.
rm -rf "$LIB"
install -d "$LIB"
cp -R "$SRC/apsta_cli" "$SRC/apsta_gui" "$LIB/"
install -m644 "$SRC/install.sh" "$LIB/install.sh"  # `apsta uninstall` runs its --uninstall
find "$LIB" -name '__pycache__' -prune -exec rm -rf {} +
say "✔" "Python packages → $LIB"

for tool in apsta:apsta_cli apsta-gtk:apsta_gui; do
    name="${tool%%:*}" module="${tool##*:}"
    cat > "$BIN/$name" <<SH
#!/bin/sh
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $module "\$@"
SH
    chmod 755 "$BIN/$name"
    say "✔" "$BIN/$name"
done

if [[ -d /usr/lib/systemd ]]; then
    render "$SRC/apsta_cli/data/apsta.service" > /etc/systemd/system/apsta.service
    install -d /usr/lib/systemd/system-sleep
    render "$SRC/apsta_cli/data/apsta-sleep" > /usr/lib/systemd/system-sleep/apsta-sleep
    chmod 755 /usr/lib/systemd/system-sleep/apsta-sleep
    systemctl daemon-reload || true
    say "✔" "systemd service + sleep hook (enable with: sudo apsta enable)"
fi

if [[ -d /usr/share/polkit-1/actions ]]; then
    render "$SRC/apsta_cli/data/com.github.apsta.policy" > /usr/share/polkit-1/actions/com.github.apsta.policy
    say "✔" "polkit policy"
fi

install -Dm644 "$SRC/apsta_gui/data/com.github.apsta.Gtk.desktop" /usr/share/applications/com.github.apsta.Gtk.desktop
install -Dm644 "$SRC/apsta_gui/data/com.github.apsta.Gtk.metainfo.xml" /usr/share/metainfo/com.github.apsta.Gtk.metainfo.xml
for kind in scalable symbolic; do
    for icon in "$SRC"/apsta_gui/data/icons/hicolor/"$kind"/apps/*.svg; do
        install -Dm644 "$icon" "/usr/share/icons/hicolor/$kind/apps/$(basename "$icon")"
    done
done
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
say "✔" "desktop entry and icon"

completion() {  # completion <shell> <file>: install it when that shell is installed
    if command -v "$1" >/dev/null 2>&1 && "$BIN/apsta" completion "$1" | install -Dm644 /dev/stdin "$2"; then
        say "✔" "$1 completion"
    fi
}
completion bash /etc/bash_completion.d/apsta
completion zsh /usr/local/share/zsh/site-functions/_apsta
completion fish /etc/fish/completions/apsta.fish

echo
echo "Done. Next: apsta detect"
