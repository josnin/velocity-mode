#!/bin/sh
set -eu

REPOSITORY="https://github.com/josnin/velocity-mode.git"

if [ -z "${HOME:-}" ]; then
  echo "install.sh: HOME must be set." >&2
  exit 1
fi

PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
  echo "install.sh: Python 3.11 or newer is required." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "install.sh: Python 3.11 or newer is required." >&2
  exit 1
fi
if ! command -v rg >/dev/null 2>&1; then
  echo "install.sh: ripgrep (rg) is required. Install it and ensure it is on PATH." >&2
  exit 1
fi

DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
case "$DATA_HOME" in
  /*) ;;
  *)
    echo "install.sh: XDG_DATA_HOME must be an absolute path." >&2
    exit 1
    ;;
esac

INSTALL_DIR=${VMODE_INSTALL_DIR:-"$DATA_HOME/vmode"}
BIN_DIR=${VMODE_BIN_DIR:-"$HOME/.local/bin"}
VENV_DIR=$INSTALL_DIR/venv

case "${0##*/}" in
  sh|dash|bash)
    PACKAGE_SOURCE="git+$REPOSITORY@main"
    ;;
  *)
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    if [ -f "$SCRIPT_DIR/pyproject.toml" ] &&
      { [ -d "$SCRIPT_DIR/.git" ] || [ -f "$SCRIPT_DIR/.git" ]; }; then
      PACKAGE_SOURCE=$SCRIPT_DIR
    else
      PACKAGE_SOURCE="git+$REPOSITORY@main"
    fi
    ;;
esac

mkdir -p "$INSTALL_DIR" "$BIN_DIR"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install --upgrade "$PACKAGE_SOURCE"
ln -sf "$VENV_DIR/bin/vmode" "$BIN_DIR/vmode"

echo "Velocity-Mode is installed at $BIN_DIR/vmode"
case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add $BIN_DIR to your PATH to run 'vmode' from any directory." ;;
esac
