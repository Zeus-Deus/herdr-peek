#!/bin/sh
# herdr-peek installer: installs the plugin and adds its two key bindings.
#
#   curl -fsSL https://raw.githubusercontent.com/Zeus-Deus/herdr-peek/main/install.sh | sh
#
# Options (after `sh -s --` when piping):
#   --no-keys   plugin only (for a devbox you connect to with `herdr machine add`;
#               keys belong on the computer you type on)
#   --keys-only key bindings only (for a laptop that only views remote machines)
#   --link DIR  link a local checkout instead of installing from GitHub
#
# Safe to run again: it never adds the keys twice, and it backs up config.toml
# before changing it.
set -eu

REPO="Zeus-Deus/herdr-peek"
herdr="${HERDR_BIN_PATH:-herdr}"
plugin=1
keys=1
link=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-keys) keys=0 ;;
    --keys-only) plugin=0 ;;
    --link) shift; link="$1" ;;
    -h|--help) sed -n '2,15p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$herdr" >/dev/null 2>&1 || die "herdr is not installed (https://herdr.dev)"

if [ "$plugin" = 1 ]; then
  command -v python3 >/dev/null 2>&1 || die "peek needs python3"
  if [ -n "$link" ]; then
    say "Linking $link"
    "$herdr" plugin link "$(cd "$link" && pwd)"
  else
    say "Installing the peek plugin from GitHub"
    "$herdr" plugin install "$REPO" --yes
  fi
  if [ -n "$link" ]; then
    main="$link/peek/main.py"
  else
    main="$(ls -dt "${XDG_CONFIG_HOME:-$HOME/.config}"/herdr/plugins/github/peek-*/peek/main.py 2>/dev/null | head -n 1 || true)"
  fi
  [ -n "$main" ] && [ -f "$main" ] && python3 "$main" doctor || true
fi

if [ "$keys" = 1 ]; then
  config="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/config.toml"
  mkdir -p "$(dirname "$config")"
  touch "$config"
  if grep -q 'command = "peek.pick"' "$config"; then
    say "Key bindings already in $config"
  else
    backup="$config.bak.$(date +%Y%m%d%H%M%S)"
    cp "$config" "$backup"
    cat >> "$config" <<'EOF'

# herdr-peek
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "peek.pick"
description = "peek: pick a file on screen"

[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "peek.last"
description = "peek: open newest file"
EOF
    say "Added prefix+f and prefix+shift+f to $config (backup: $backup)"
  fi
  check="$("$herdr" config check 2>&1 || true)"
  case "$check" in
    *keys.command*disabled*|*disabled*keys.command*)
      printf '%s\n' "$check"
      say "Another binding already uses one of peek's keys. Change the key lines under '# herdr-peek' in $config."
      ;;
  esac
  if "$herdr" server reload-config >/dev/null 2>&1; then
    say "Reloaded herdr"
  else
    say "herdr isn't running; the keys apply next time you start it"
  fi
fi

say "Done. In herdr press prefix+f, then the letter next to a file."
