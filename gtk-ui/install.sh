#!/usr/bin/env bash
# Install apsta-gtk to /usr/local/bin and register the desktop entry
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh"
    exit 1
fi

# Check deps
python3 -c "
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
" 2>/dev/null || {
    echo "GTK4 or Libadwaita not found."
    echo "Run: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    exit 1
}


python3 -c "
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
" 2>/dev/null || {
    echo "GTK4 or Libadwaita not found."
    echo "Run: sudo apt install gir1.2-gtk-4.0 gir1.2-adw-1"
    exit 1
}

install -Dm755 "$SCRIPT_DIR/apsta-gtk" /usr/local/bin/apsta-gtk
install -Dm644 "$SCRIPT_DIR/com.github.apsta.Gtk.desktop" \
    /usr/share/applications/com.github.apsta.Gtk.desktop

echo "  ✔  apsta-gtk installed"
echo "  ✔  Desktop entry registered"
echo ""
echo "Launch from your app menu: 'apsta Hotspot Manager'"
echo "Or from terminal:          apsta-gtk"
