#!/usr/bin/env bash
# Stream Guard installer (per user, no root).   ./install.sh  |  ./install.sh --uninstall
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
LIB=$HOME/.local/share/streamguard
BIN=$HOME/.local/bin
APPS=$HOME/.local/share/applications
DESKTOP=$APPS/io.github.mishabvibes.StreamGuard.desktop

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root"

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$LIB" "$BIN/streamguard" "$DESKTOP"
    say "removed (settings kept in ~/.config/streamguard, reports in ~/Documents/StreamGuard)"
    exit 0
fi

say "checking dependencies"
missing=()
python3 -c 'import dbus' 2>/dev/null || missing+=(python3-dbus)
python3 -c 'import gi; gi.require_version("Adw", "1")' 2>/dev/null || missing+=(gir1.2-adw-1)
python3 -c 'import gi; gi.require_version("Gst", "1.0")' 2>/dev/null || missing+=(gir1.2-gstreamer-1.0)
python3 -c 'import gi; gi.require_version("Soup", "3.0")' 2>/dev/null || missing+=(gir1.2-soup-3.0)
gst-inspect-1.0 level >/dev/null 2>&1 || missing+=(gstreamer1.0-plugins-good)
if [ ${#missing[@]} -gt 0 ]; then
    die "missing packages, install with: sudo apt install ${missing[*]}"
fi

say "installing to $LIB"
rm -rf "$LIB"
install -d "$LIB/streamguard" "$BIN" "$APPS"
install -m 0644 "$SRC"/streamguard/*.py "$LIB/streamguard/"
install -m 0755 "$SRC/bin/streamguard" "$BIN/streamguard"
sed "s|@BIN@|$BIN|" "$SRC/data/io.github.mishabvibes.StreamGuard.desktop" > "$DESKTOP"
update-desktop-database -q "$APPS" 2>/dev/null || true

say "done. Open \"Stream Guard\" from the app launcher (nothing runs until you open it)."
