#!/usr/bin/env bash
# install_laptop.sh — Set up (or upgrade) a laptop for the
# multi-laptop browser-visit-logger architecture:
#
#   1. Run uninstall_laptop_legacy.sh first to remove the
#      snapshot-verifier LaunchAgent (which now lives on the EC2 VM).
#   2. Delegate to the existing browser-visit-logger/install.sh for
#      the BVLHost + extension manifest, with BVL_SKIP_VERIFIER=1 so
#      install.sh does not re-install the verifier LaunchAgent.
#   3. Materialize ~/.browser-visit-logger/config.json with the
#      machine_id (sanitized LocalHostName), and ensure mTLS material
#      is in place under ~/.browser-visit-logger/tls/.
#   4. Provision the sync runtime: a grpcio venv at
#      ~/.browser-visit-logger/sync-venv (which BVLHost prefers when
#      spawning sync_client.py) and the generated gRPC Python stubs.
#
# Usage:
#   bash browser-visit-tools/install_laptop.sh
#   bash browser-visit-tools/install_laptop.sh --server bvl-vm.example.com:50443

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$HOME/.browser-visit-logger"
CONFIG_FILE="$CONFIG_DIR/config.json"
TLS_DIR="$CONFIG_DIR/tls"

SERVER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    -h|--help) sed -n '1,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

info() { echo "[install-laptop] $*"; }

info "Step 1/3: removing legacy verifier LaunchAgent (if present)"
bash "$SCRIPT_DIR/uninstall_laptop_legacy.sh"

info "Step 2/4: provisioning the sync runtime (grpcio venv + gRPC stubs)"
# sync_client.py needs the grpcio runtime plus generated Python stubs,
# neither of which the ambient python3 provides.  Build a dedicated venv
# the host prefers (see BVLHost.resolveSyncPython) and generate the stubs
# the client imports.  Done BEFORE install.sh so install.sh can bundle the
# stubs into the host .app.
mkdir -p "$CONFIG_DIR"
SYNC_VENV="$CONFIG_DIR/sync-venv"
STUBS_DIR="$REPO_ROOT/browser-visit-sync-server/gen/syncpb_py"

# Pick an interpreter with prebuilt grpcio wheels.  A too-new python3
# (e.g. 3.14) has no wheels yet and would trigger a failing source build,
# so prefer a known-good minor.  Override with BVL_SYNC_PYTHON.
pick_sync_python() {
  if [[ -n "${BVL_SYNC_PYTHON:-}" ]]; then echo "$BVL_SYNC_PYTHON"; return; fi
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo python3
}
SYNC_PY_BOOT="$(pick_sync_python)"

if [[ ! -x "$SYNC_VENV/bin/python" ]]; then
  info "creating sync venv at $SYNC_VENV (using $SYNC_PY_BOOT)"
  "$SYNC_PY_BOOT" -m venv "$SYNC_VENV"
fi
"$SYNC_VENV/bin/pip" install --quiet --upgrade pip
"$SYNC_VENV/bin/pip" install --quiet grpcio grpcio-tools

info "generating gRPC Python stubs into $STUBS_DIR"
mkdir -p "$STUBS_DIR"
"$SYNC_VENV/bin/python" -m grpc_tools.protoc \
  -I "$REPO_ROOT/browser-visit-sync-server/proto" \
  --python_out="$STUBS_DIR" --grpc_python_out="$STUBS_DIR" \
  "$REPO_ROOT/browser-visit-sync-server/proto/sync.proto"
info "sync runtime ready (interpreter: $SYNC_VENV/bin/python)"

info "Step 3/4: running the upstream install.sh (BVLHost + extension)"
# Multi-laptop mode runs sealing/verification on the EC2 VM, so the laptop
# must not install its own verifier LaunchAgent.  install.sh also bundles
# sync_client.py + the stubs generated above into the host .app.
BVL_SKIP_VERIFIER=1 bash "$REPO_ROOT/browser-visit-logger/install.sh"

info "Step 4/4: writing machine config"
mkdir -p "$CONFIG_DIR" "$TLS_DIR"
chmod 700 "$TLS_DIR"

# Sanitize hostname into a machine_id.  Mirrors Paths.swift's
# sanitiseMachineId: keep [A-Za-z0-9_-], collapse runs of '-'.
RAW_HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
MACHINE_ID="$(echo "$RAW_HOST" | sed 's/[^A-Za-z0-9_-]/-/g' | sed 's/--*/-/g' | sed 's/^-//; s/-$//')"
if [[ -z "$MACHINE_ID" ]]; then
  MACHINE_ID="unknown-host"
fi

if [[ -f "$CONFIG_FILE" ]]; then
  info "config already exists at $CONFIG_FILE — leaving machine_id unchanged"
else
  python3 - <<PY
import json, datetime, pathlib
cfg = {
    "machine_id": "$MACHINE_ID",
    "assigned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
if "$SERVER":
    cfg["sync_server"] = "$SERVER"
pathlib.Path("$CONFIG_FILE").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
PY
  info "wrote $CONFIG_FILE with machine_id=$MACHINE_ID"
fi

cat <<EOF

[install-laptop] Done.

Machine ID:  $MACHINE_ID
Config file: $CONFIG_FILE
TLS dir:     $TLS_DIR

Next steps:
  1. Generate a client cert for this laptop (out of band) and drop
     client.crt / client.key into $TLS_DIR.  Also put the server CA
     into $TLS_DIR/server-ca.crt.
  2. On the VM, enroll this laptop:
       enroll_machine.py --db enrolled_machines.db \\
         --machine-id $MACHINE_ID --cert client.crt
  3. (Optional) Migrate the iCloud archive to Google Drive:
       python3 browser-visit-tools/migrate_icloud_to_gdrive.py --src ... --dst ... --db ~/browser-visits.db
  4. Trigger a Chrome → extension interaction; BVLSync should fire
     within ~60s.
EOF
