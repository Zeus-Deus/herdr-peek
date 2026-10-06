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
#   --uninstall remove the plugin, its key bindings and its cache
#               (combine with --no-keys / --keys-only to remove just one part)
#
# Safe to run again: it never adds the keys twice, and it backs up config.toml
# before changing it. Running it again also updates the plugin.
set -eu

REPO="Zeus-Deus/herdr-peek"
herdr="${HERDR_BIN_PATH:-herdr}"
plugin=1
keys=1
link=""
uninstall=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-keys) keys=0 ;;
    --keys-only) plugin=0 ;;
    --link) shift; link="$1" ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,15p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$herdr" >/dev/null 2>&1 || die "herdr is not installed (https://herdr.dev)"

config="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/config.toml"

reload() {
  if "$herdr" server reload-config >/dev/null 2>&1; then
    say "Reloaded herdr"
  else
    say "herdr isn't running; changes apply next time you start it"
  fi
}

backup_config() {
  backup="$config.bak.$(date +%Y%m%d%H%M%S)"
  cp "$config" "$backup"
}

# exactly what the installer appends, so uninstall can take exactly it back out
peek_block() {
  cat <<'EOF'

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
}

# --- uninstall ---------------------------------------------------------------

if [ "$uninstall" = 1 ]; then
  if [ "$plugin" = 1 ]; then
    if "$herdr" plugin list 2>/dev/null | grep -q '^- peek '; then
      # GitHub installs are uninstalled; a linked checkout is unlinked (its files stay)
      "$herdr" plugin uninstall peek >/dev/null 2>&1 || "$herdr" plugin unlink peek >/dev/null
      say "Removed the peek plugin"
    else
      say "The peek plugin isn't installed"
    fi
    state="${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/peek"
    if [ -d "$state" ]; then
      rm -rf "$state"
      say "Removed peek's cache"
    fi
    settings="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/plugins/config/peek"
    if [ -d "$settings" ]; then
      if rmdir "$settings" 2>/dev/null; then
        say "Removed peek's empty settings folder"
      else
        say "Kept your peek settings in $settings (delete it if you don't need it)"
      fi
    fi
  fi
  if [ "$keys" = 1 ]; then
    if [ -f "$config" ] && grep -q 'command *= *"peek\.' "$config"; then
      backup_config
      block="$(mktemp)"
      peek_block > "$block"
      n=$(wc -c < "$block")
      size=$(wc -c < "$backup")
      if [ "$size" -ge "$n" ] && tail -c "$n" "$backup" | cmp -s - "$block" \
        && ! head -c $((size - n)) "$backup" | grep -q 'command *= *"peek\.'; then
        # the installer's block is still the end of the file: cut exactly it
        head -c $((size - n)) "$backup" > "$config"
      else
      # otherwise drop every [[keys.command]] table that runs a peek action
      awk '
        function flush() {
          if (buf != "" && buf !~ /command[ \t]*=[ \t]*"peek\./) printf "%s", buf
          buf = ""; inblk = 0
        }
        /^[ \t]*# herdr-peek[ \t]*$/ { flush(); next }
        /^[ \t]*\[/ {
          flush()
          if ($0 ~ /^[ \t]*\[\[keys\.command\]\]/) { inblk = 1; buf = $0 "\n"; next }
        }
        { if (inblk) buf = buf $0 "\n"; else print }
        END { flush() }
      ' "$backup" > "$config"
      fi
      rm -f "$block"
      say "Removed peek's key bindings from $config (backup: $backup)"
    else
      say "No peek key bindings in $config"
    fi
    reload
  fi
  say "Uninstalled."
  exit 0
fi

# --- install / update --------------------------------------------------------

if [ "$plugin" = 1 ]; then
  command -v python3 >/dev/null 2>&1 || die "peek needs python3"
  if [ -n "$link" ]; then
    say "Linking $link"
    "$herdr" plugin link "$(cd "$link" && pwd)"
    main="$link/peek/main.py"
  else
    say "Installing the peek plugin from GitHub"
    "$herdr" plugin install "$REPO" --yes
    main="$(ls -dt "${XDG_CONFIG_HOME:-$HOME/.config}"/herdr/plugins/github/peek-*/peek/main.py 2>/dev/null | head -n 1 || true)"
  fi
  if [ -n "$main" ] && [ -f "$main" ]; then
    python3 "$main" doctor || true
  fi
fi

if [ "$keys" = 1 ]; then
  mkdir -p "$(dirname "$config")"
  touch "$config"
  if grep -q 'command *= *"peek\.pick"' "$config"; then
    say "Key bindings already in $config"
  else
    backup_config
    peek_block >> "$config"
    say "Added prefix+f and prefix+shift+f to $config (backup: $backup)"
  fi
  check="$("$herdr" config check 2>&1 || true)"
  case "$check" in
    *keys.command*disabled*|*disabled*keys.command*)
      printf '%s\n' "$check"
      say "Another binding already uses one of peek's keys. Change the key lines under '# herdr-peek' in $config."
      ;;
  esac
  reload
fi

say "Done. In herdr press prefix+f, then the letter next to a file."
