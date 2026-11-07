#!/usr/bin/env bash
# Installs Denise Station for the current user (no root needed, except for the packages named in the hints).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.local/share/denise-station-app"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

# --- distribution-specific install hints -------------------------------------------------
. /etc/os-release 2>/dev/null || true
DISTRO="${ID:-unknown} ${ID_LIKE:-}"
pkg_cmd() {  # the GTK3 Python bindings
    case " $DISTRO " in
        *" debian "*|*" ubuntu "*|*" linuxmint "*|*" pop "*|*" raspbian "*) echo "sudo apt install python3-gi gir1.2-gtk-3.0";;
        *" fedora "*|*" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*|*" ol "*) echo "sudo dnf install python3-gobject gtk3";;
        *" arch "*|*" manjaro "*|*" endeavouros "*|*" cachyos "*) echo "sudo pacman -S python-gobject gtk3";;
        *" suse "*|*" opensuse "*|*" opensuse-leap "*|*" opensuse-tumbleweed "*|*" sles "*) echo "sudo zypper install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0";;
        *" alpine "*) echo "sudo apk add py3-gobject3 gtk+3.0";;
        *" void "*) echo "sudo xbps-install python3-gobject gtk+3";;
        *" gentoo "*) echo "sudo emerge dev-python/pygobject x11-libs/gtk+:3";;
        *" nixos "*) echo "nix-shell -p 'python3.withPackages(p:[p.pygobject3])' gtk3 gobject-introspection";;
        *) echo "install PyGObject and GTK 3 (python3-gi / python3-gobject / gtk3) with your package manager";;
    esac
}

if ! python3 -c 'import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk' 2>/dev/null; then
    echo "Missing GTK3 Python bindings. Install them with:"
    echo "  $(pkg_cmd)"
    exit 1
fi

echo "Amiberry is not bundled: start Denise Station and use Settings -> Download / update Amiberry."

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
for f in denise_core.py denise_scraper.py denise_i18n.py denise_retroplay.py whdload_dialog.py setup_wizard.py; do
    install -m 644 "$SRC/$f" "$APP_DIR/$f"
done
install -m 755 "$SRC/denise_station.py" "$APP_DIR/denise_station.py"
mkdir -p "$APP_DIR/locale"
install -m 644 "$SRC"/locale/*.po "$APP_DIR/locale/"
mkdir -p "$APP_DIR/assets"
install -m 644 "$SRC"/assets/*.png "$APP_DIR/assets/"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
mkdir -p "$ICON_DIR/512x512/apps" "$ICON_DIR/scalable/apps"
install -m 644 "$SRC/assets/denise-station.png" "$ICON_DIR/512x512/apps/denise-station.png"
install -m 644 "$SRC/assets/denise-station.svg" "$ICON_DIR/scalable/apps/denise-station.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true

cat > "$BIN_DIR/denise-station" <<EOF
#!/usr/bin/env sh
exec python3 "$APP_DIR/denise_station.py" "\$@"
EOF
chmod 755 "$BIN_DIR/denise-station"

# The launcher file is named after the application id, so the dock matches the running window to it
# (and shows its icon instead of a generic one).
sed "s|@EXEC@|$BIN_DIR/denise-station|" "$SRC/denise-station.desktop" > "$DESKTOP_DIR/local.denisestation.App.desktop"
rm -f "$DESKTOP_DIR/denise-station.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" || true

# Remove the program files of the old name (iGame for Linux). Your library and settings are kept
# in ~/.local/share/igame-linux and ~/.config/igame-linux and are copied over on first start.
rm -rf "$HOME/.local/share/igame-linux-app"
rm -f "$BIN_DIR/igame-linux" "$DESKTOP_DIR/igame-linux.desktop"

echo "Installed. Start it from the menu (Games → Denise Station) or run: denise-station"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Note: $BIN_DIR is not on your PATH yet (log out and in once).";; esac
