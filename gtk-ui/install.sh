#!/usr/bin/env bash
# Install apsta-gtk to /usr/local/bin and register the desktop entry
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh"
    exit 1
fi

# Check GTK4 + Libadwaita are available.
# Use the exact import chain the app uses — not "import python3" which is
# not valid Python and always fails regardless of what is installed.
python3 -c "
import gi
import qrcode
from PIL import Image
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
" 2>/dev/null || {
    echo "Required GTK or QR dependencies not found."
    echo "Run: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-qrcode python3-pil"
    exit 1
}

install -Dm755 "$SCRIPT_DIR/apsta-gtk" /usr/local/bin/apsta-gtk
rm -rf /usr/local/bin/apsta_gui
cp -r "$SCRIPT_DIR/../apsta_gui" /usr/local/bin/apsta_gui
find /usr/local/bin/apsta_gui -type f -name "*.py" -exec chmod 0644 {} \;
install -Dm644 "$SCRIPT_DIR/com.github.apsta.Gtk.desktop" \
    /usr/share/applications/com.github.apsta.Gtk.desktop

echo "  ✔  apsta-gtk installed"
echo "  ✔  Desktop entry registered"
echo ""
echo "Launch from your app menu: 'apsta Hotspot Manager'"
echo "Or from terminal:          apsta-gtk"