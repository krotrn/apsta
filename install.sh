#!/usr/bin/env bash
# apsta installer
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/usr/local/bin/apsta"
CLI_PACKAGE_DIR="/usr/local/bin/apsta_cli"

install_completion() {
    local shell_name="$1"
    local out_path=""

    case "$shell_name" in
        bash)
            out_path="/etc/bash_completion.d/apsta"
            mkdir -p "$(dirname "$out_path")"
            python3 "$SCRIPT_DIR/apsta.py" completion bash > "$out_path"
            ;;
        zsh)
            out_path="/usr/local/share/zsh/site-functions/_apsta"
            mkdir -p "$(dirname "$out_path")"
            python3 "$SCRIPT_DIR/apsta.py" completion zsh > "$out_path"
            ;;
        fish)
            out_path="/etc/fish/completions/apsta.fish"
            mkdir -p "$(dirname "$out_path")"
            python3 "$SCRIPT_DIR/apsta.py" completion fish > "$out_path"
            ;;
        *)
            echo "  →  Unsupported shell for completion install: $shell_name"
            return 1
            ;;
    esac

    echo "  ✔  $shell_name completion installed → $out_path"
}

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh"
    exit 1
fi

echo ""
echo "Installing apsta..."
cp "$SCRIPT_DIR/apsta.py" "$TARGET"
chmod +x "$TARGET"
echo "  ✔  apsta installed → $TARGET"

rm -rf "$CLI_PACKAGE_DIR"
cp -r "$SCRIPT_DIR/apsta_cli" "$CLI_PACKAGE_DIR"
find "$CLI_PACKAGE_DIR" -type f -name "*.py" -exec chmod 0644 {} \;
echo "  ✔  CLI package installed → $CLI_PACKAGE_DIR"

echo ""
read -r -p "Install shell completion? [y/N] " completion_answer
if [[ "$completion_answer" =~ ^[Yy]$ ]]; then
    default_shell="${SHELL##*/}"
    read -r -p "Choose shell (bash/zsh/fish) [${default_shell:-bash}]: " chosen_shell
    chosen_shell="${chosen_shell:-$default_shell}"
    chosen_shell="${chosen_shell:-bash}"
    install_completion "$chosen_shell" || echo "  →  Skipped completion install"
else
    echo "  →  Skipped. Install later with: apsta completion <bash|zsh|fish>"
fi

echo ""
read -r -p "Enable auto-start on boot and sleep/wake persistence? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    python3 "$SCRIPT_DIR/apsta.py" enable
else
    echo "  →  Skipped. Enable later with:  sudo apsta enable"
fi

echo ""
echo "Done. Run: apsta detect"
