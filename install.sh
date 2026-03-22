#!/usr/bin/env bash
# apsta installer
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/usr/local/bin/apsta"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh"
    exit 1
fi

echo ""
echo "Installing apsta..."
cp "$SCRIPT_DIR/apsta.py" "$TARGET"
chmod +x "$TARGET"
echo "  ✔  apsta installed → $TARGET"

echo ""
read -r -p "Enable auto-start on boot and sleep/wake persistence? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    python3 "$SCRIPT_DIR/apsta.py" enable
else
    echo "  →  Skipped. Enable later with:  sudo apsta enable"
fi

echo ""
echo "Done. Run: apsta detect"
