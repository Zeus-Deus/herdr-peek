# Client-native temporary preview protocol (v1)

This opt-in path never calls server `open_items`, `display.open_external`,
`xdg-open`, or a server-native picker/viewer. Existing plugin actions remain
unchanged. It is Linux/XDG laptop functionality, not terminal auto-detection.

## Core → local helper

Use local `[[keys.client_command]]` bindings with an argv array:

```toml
[[keys.client_command]]
key = "prefix+f"
command = ["python3", "/absolute/installed/peek/peek/main.py", "client-pick"]
```

`client-last` is the second action. The compatible core owns endpoint choice
and sets **HERDR_CLIENT_CONTEXT_JSON** on that local child:

```json
{
  "version": 1,
  "endpoint_id": "opaque-actual-committed-endpoint",
  "machine_id": "optional-saved-machine-id",
  "ssh_target": "optional-user@host-or-ssh-config-alias",
  "session": "optional-explicit-session",
  "server_socket": "/optional/exact/local/server.sock",
  "workspace_id": "optional-focused-workspace",
  "tab_id": "optional-focused-tab",
  "pane_id": "focused-pane-on-the-chosen-endpoint",
  "selection": {
    "pane_id": "focused-pane-on-the-chosen-endpoint",
    "anchor": {"row": 4, "col": 2},
    "cursor": {"row": 4, "col": 20},
    "content_revision": 42
  }
}
```

Omit optional fields (or use null); do not provide empty strings. `endpoint_id`
is required and retained as data. A saved `machine_id` without `ssh_target`
is rejected rather than guessed to be local. No machine/home names are built
in. `server_socket` is a **local** endpoint override and is removed before
remote transfer. Focus IDs are snapshot IDs, not looked up on the laptop.
`client-last` drops `selection`; `client-pick` preserves its exact parameters.

## Laptop → server helper

Local panes execute the same installed module as an argv child:

```text
[sys.executable, /absolute/local/peek/peek/main.py, native-server]
```

For SSH panes the exact argv shape is:

```text
ssh -T
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UpdateHostKeys=no
  -o ConnectTimeout=10
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=2
  -- <ssh_target> <one-fixed-remote-command-argument>
```

The last argument is `python3 -c ` followed by **one** `shlex.quote(BOOTSTRAP)`;
`peek/native_client.py:BOOTSTRAP` is the authoritative fixed code. No target,
filename, session, pane ID or request text appears inside that shell command.
The target is a separate SSH argv argument; leading options, control/whitespace,
shell metacharacters and URI/path forms are rejected. Ordinary host/user@host,
IPv6 and simple SSH-config aliases are supported. Ports/identities/proxies
belong in trusted existing SSH config.

Each request is JSON on stdin, with EOF after the request. List:

```json
{
  "version": 1,
  "op": "list",
  "images_only": false,
  "context": {"version": 1, "endpoint_id": "opaque", "pane_id": "pane-id"}
}
```

`images_only=true` is used by `client-last`, filtering on the server before
bounding the candidate list. The bootstrap reads the optional session from
JSON and constructs:

```text
[herdr, --session, <literal-session>, plugin, list, --json, --plugin, peek]
```

Without a session, omit those two session arguments. It requires exactly one
enabled `plugin_id=peek` entry with an absolute `plugin_root`, imports the
native exporter under that installed root, and forwards the original request.
Old Peek installations without `native_server.py` fail with an update message.
The real Herdr 0.9.1 `result.plugins[].plugin_root/enabled/plugin_id` response
has been exercised in the private SSH integration test.

The exporter gets the requested pane and visible text with Herdr CLI argv
(`--session` is separate argv when supplied; an exact local socket takes
precedence). It resolves paths relative to the owning pane's foreground cwd,
then initial cwd and owning user's home, with existing `peek.paths` parsing.
At most 256 distinct most-recent regular-file candidates are returned, sorted
by bottom-most occurrence. Symlinks, FIFOs, directories and over-limit files
are excluded; no existence checks happen on the laptop.

Successful list response:

```json
{
  "version": 1,
  "ok": true,
  "candidates": [{
    "path": "/absolute/server/file.png",
    "line": null,
    "row": 17,
    "col": 4,
    "kind": "image",
    "signature": {"dev": 1, "ino": 2, "size": 1234, "mtime_ns": 3, "ctime_ns": 4}
  }]
}
```

The numbers above illustrate the schema, not a test receipt. Export sends the
**exact chosen candidate** (including its signature):

```json
{"version": 1, "op": "export", "context": {}, "candidate": {"path": "/absolute/server/file.png", "line": null, "row": 17, "col": 4, "kind": "image", "signature": {"dev": 1, "ino": 2, "size": 1234, "mtime_ns": 3, "ctime_ns": 4}}}
```

The candidate must actually be a JSON object. Success is
`{"version":1,"ok":true,"png_base64":"...actual PNG..."}`. Failure is
`{"version":1,"ok":false,"error":"actionable explanation"}`; operational
failures still emit valid JSON on stdout. Stderr/exit failures in the transport
also become local visible errors. Cancellation of the picker returns exit 0;
other failures return 1; handled client interruption returns 130.

### Selection compatibility

When `selection` is present, `pane.selection.read` is sent to the exact server
API socket with the original anchor/cursor/content revision. Its `result.pane_id`
and response ID must match. This RPC has no 0.9.1 CLI helper; source qualification
is `src/api/schema/panes.rs:PaneSelectionReadParams` and
`src/app/api/panes.rs:handle_pane_selection_read` in compatible Herdr source.
Stock 0.9.1 rejects the method, and that rejection has been tested to produce a
visible error rather than opening an unrelated screen image. Remote defaults
use the release `.config/herdr[/sessions/<session>]/herdr.sock` layout; custom
remote sockets require the server-side environment override. Debug/custom
local endpoints should always supply `server_socket`. An explicit
session ignores the ambient default socket override, matching Herdr CLI
routing; an exact `server_socket` still takes precedence. Stale/unsupported
selection errors ask the user to clear the selection and retry visible paths.

## Bounds and cleanup

- JSON request: 64 KiB; full response: 12 MiB; stderr: 64 KiB.
- Pane read/plugin discovery/selection read: 1 MiB each.
- Source: regular nonsymlink file, at most 32 MiB.
- Preview: resized PNG, at most 1600 × 1200 and 8 MiB. Laptop validates chunk
  lengths/CRCs, IHDR dimensions, IDAT presence, IEND and no trailing bytes.
- Transport: 90 seconds; plugin discovery: 15 seconds; CLI/selection: 15/10
  seconds; native helper: 60-second alarm. Helper TERM/HUP/ALRM interrupts
  conversion and unwinds temporary snapshot/cache cleanup. Local transport
  gives a terminating helper 3 seconds to clean up before forced kill.
- The exporter opens with O_NOFOLLOW/O_NONBLOCK, verifies stat against the
  listed signature, copies bounded source bytes into a private 0700 directory,
  and rechecks the open source descriptor after copying. Native conversion uses
  only `native_raster`, never general `convert.image_png` or its fallback tools.
  This qualified adapter is Linux/ImageMagick 7 (`magick`), **PNG sources only**.
  SVG, JPEG, GIF, WebP and other formats fail visibly until their confinement is
  separately qualified. A valid PNG IHDR/CRC and a hard 8192-per-dimension,
  8-million-pixel limit are checked before starting any decoder.
- Every export has private policy/HOME/scratch directories. The policy denies
  all delegates, filters, modules, coders and paths, then admits only the PNG
  module/coder and the exact snapshot read/output write. Indirect `@`/pipe/stdin
  resources stay denied. ImageMagick limits: one thread, 20-second time, 8192
  width/height, 8MP area, four list entries, 256MiB memory/max-memory-request,
  zero map/disk pixel-cache spill, 16 file resources. The installed system
  policies are not edited. Each export requires the private policy in actual
  `-list policy` output AND a security-policy denial for an inert secondary PNG.
- Before exec/decoding, converter OS limits are set: 1GiB address space,
  20 CPU seconds, 8MiB maximum output/scratch file, no core dumps, 32 descriptors,
  077 umask. Both converter streams are capped at 64KiB during reading, with
  a 20-second wall deadline; output reads stop at 8MiB + 1 byte and the PNG is
  structurally validated. A dedicated Linux subreaper owns a separate converter
  process group, kills it on cancellation/error/leader exit, and reaps the leader
  plus adopted descendants before the worker returns and snapshot cleanup runs.
  This is feature/resource confinement, not a sandbox against native decoder
  vulnerabilities or descendants deliberately escaping the owned process group.
- Laptop file: private temporary directory 0700, `preview.png` 0600. Resolve
  default image/png desktop association locally, then run the supported imv
  argv directly. Its process is waited/reaped before deleting the directory.
  On handled cancellation the owned viewer process group is terminated/reaped
  first. Neither an `xdg-open` launcher nor an unsupported single-instance
  application is accepted as a foreground lifecycle adapter.
- Errors go to stderr and a local Zenity `--error --no-markup` dialog when
  available. The source is never modified. SIGKILL/power failure can defeat
  finalizers; no universal cleanup guarantee is claimed for forced death.

## Settings-only manual adoption contract

`--client-native` prepares settings; exit 0 does **not** mean an existing config
was adopted. The helper opens a regular nonsymlink config by descriptor, bounds
and checks the read, and captures its identity/bytes. If changes are needed,
it creates a unique 0700 `.peek-proposal-*` directory alongside the config with
0600 `config.toml` (merged proposal), `baseline.toml` (captured input), and
`adoption.json` (target, baseline identity/hash, manual-adoption flag). The
original path is never written, renamed, replaced, or recreated, even if an
editor changes/deletes/replaces it after the snapshot. A matching config is
untouched. An initially absent config may be created with O_EXCL; any intervening
create, including an empty file or symlink, is refused atomically.

Arbitrary editors share no enforceable compare-and-replace protocol. A final
re-read would still leave a rename TOCTOU, so the helper intentionally offers
**no automatic adoption operation**. Review proposal against its baseline and
the current config, reconcile every newer edit, and explicitly adopt only under
your own editor/exclusive-access procedure. Baseline identity/hash is provenance,
not a promise that the live path is still unchanged. Reload/reconnect the client
only after adoption; the settings-only shell path never reloads a server.

Installation proposals can migrate legacy Peek pick/last server bindings as
before. Native uninstall proposals remove only exact `client_command` argv
entries owned by this installed entrypoint; they preserve all `keys.command`
server actions and other installations' bindings. Non-BMP local paths are emitted
as valid Unicode TOML rather than JSON surrogate pairs. Proposals contain private
configuration; remove them yourself when their review/adoption is complete.

## Headless verification (no real windows)

Run these only with private HOME/XDG/runtime/TMP roots and inherited
`HERMES_*`/`HERDR_*` overrides removed from the test child environment (do not
export a fixture HOME into the parent shell). The warning-strict full suite
includes the real private policy/resource and synthetic-descendant tests.

```bash
python3 -W error -m unittest discover -s tests -v
python3 -W error -m unittest discover -s tests -p test_native.py -v
python3 -W error -m unittest discover -s tests -p test_native_raster.py -v
python3 -W error -m unittest discover -s tests -p test_native_integration.py -v
python3 -W error -m unittest discover -s tests -p test_client_install.py -v
```

Native tests need `herdr`, ImageMagick (`magick`), OpenSSH client/keygen/sshd,
and Linux Unix sockets. They use private HOME/XDG/runtime/socket/settings,
private SSH host/client keys, a prepopulated private known_hosts file and a
user-level loopback sshd. A synthetic foreground `imv` process records the
actual preview path/modes and remains alive until explicitly released; a
synthetic Zenity process records actual argv. No GUI window is launched and
no live installer, user SSH configuration, app settings or deployment is
changed. Legacy terminal-plugin E2E scripts are separate existing lanes.

The installer lane exercises settings-only entrypoints with a compatible
synthetic core and proves refusal against the real installed old core. It is
**not** proof that a newly built compatible Herdr client dispatches real keys;
that and the actual desktop viewer acceptance belong to the parent live E2E
lane. The positive selection-RPC test is a real private Unix socket peer, not
proof of selection support in the old 0.9.1 endpoint.
