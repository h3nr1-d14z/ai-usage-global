#!/usr/bin/env bash
# Link this checkout into the Omarchy plugin dir for hot-reload development.
# Edit files here; the shell picks up saves. Use `omarchy plugin add <git url>`
# for a real install (this symlink workflow is development-only).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="h3nr1.d14z.ai-usage"
DEST_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

NO_TAIL=0
RESTART=0
REMOVE=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) echo "Usage: ./dev.sh [--no-tail] [--restart] [--remove]"; exit 0 ;;
    --no-tail) NO_TAIL=1 ;;
    --restart) RESTART=1 ;;
    --remove) REMOVE=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [[ "$REMOVE" -eq 1 ]]; then
  if [[ -L "$DEST_DIR" ]]; then
    rm "$DEST_DIR"
    echo "==> Unlinked $DEST_DIR"
  else
    echo "==> No dev symlink at $DEST_DIR (nothing to remove)"
  fi
  command -v omarchy-shell >/dev/null 2>&1 && omarchy-shell shell rescanPlugins || true
  exit 0
fi

mkdir -p "$(dirname "$DEST_DIR")"
if [[ -L "$DEST_DIR" ]]; then
  echo "==> Already linked: $DEST_DIR → $SRC_DIR"
elif [[ -e "$DEST_DIR" ]]; then
  echo "==> Replacing copied install at $DEST_DIR with symlink"
  rm -rf "$DEST_DIR"
  ln -sfn "$SRC_DIR" "$DEST_DIR"
else
  echo "==> Linking $DEST_DIR → $SRC_DIR"
  ln -sfn "$SRC_DIR" "$DEST_DIR"
fi

if command -v omarchy >/dev/null 2>&1; then
  echo "==> Validating"
  omarchy plugin validate "$DEST_DIR"
  echo "==> Enabling $PLUGIN_ID"
  omarchy plugin enable "$PLUGIN_ID" 2>/dev/null || true
fi

if command -v qmllint >/dev/null 2>&1; then
  SHELL_PATH="${OMARCHY_PATH:-/usr/share/omarchy}/shell"
  echo "==> qmllint"
  qmllint -I "$SHELL_PATH" "$SRC_DIR/Panel.qml" && echo "   Panel.qml OK"
fi

if [[ "$RESTART" -eq 1 ]] || ! command -v omarchy-shell >/dev/null 2>&1; then
  echo "==> Restart shell"
  omarchy restart shell
else
  echo "==> Rescan plugins"
  omarchy-shell shell rescanPlugins
fi

echo
echo "Edit files in $SRC_DIR — saves hot-reload."
echo "Panel lifecycle: omarchy-shell shell summon $PLUGIN_ID '{}'  /  hide $PLUGIN_ID"
echo

if [[ "$NO_TAIL" -eq 1 ]]; then
  echo "Done (no journal follow)."
  exit 0
fi

echo "==> journalctl -t omarchy-shell -f  (Ctrl-C to stop)"
exec journalctl --user -t omarchy-shell -f --no-hostname
