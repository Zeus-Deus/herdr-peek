#!/usr/bin/env bash
# Install herdr-peek on this machine and report which optional viewers it has.
#
#   ./scripts/install.sh            # link this checkout (for development)
#   ./scripts/install.sh --github   # install the published plugin from GitHub
#
# Run it on every machine whose files you want to view. Key bindings live in
# the herdr client you type into, so add them on your laptop only (printed below).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
herdr="${HERDR_BIN_PATH:-herdr}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "herdr-peek needs python3 (standard library only)." >&2
  exit 1
fi

if [[ "${1:-}" == "--github" ]]; then
  "$herdr" plugin install Zeus-Deus/herdr-peek --yes
else
  "$herdr" plugin link "$here"
fi

python3 "$here/peek/main.py" doctor

cat <<'EOF'

Add these to ~/.config/herdr/config.toml on the machine you type on, then run
`herdr server reload-config`:

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
