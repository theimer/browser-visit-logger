#!/usr/bin/env bash
# check_install.sh — Report which pieces of the browser-visit-logger
# install are present on this laptop, so you can tell what partial state
# an install is in.  Read-only: it never changes anything.
#
# It checks everything the installers set up:
#   - browser-visit-logger/install.sh   (single-laptop core)
#   - browser-visit-tools/install_laptop.sh (multi-laptop extras)
#   - the optional MCP server and tools venvs
#   - whether Chrome has the extension loaded, and the runtime files
#     the host creates on first use
#
# Each line is one item with a status:
#   OK       installed and looks right
#   MISSING  should be installed for this mode but isn't
#   WARN     present but wrong (stale, broken, or a leftover)
#   N/A      not expected in this mode
#   INFO     optional, or created on first use — not counted as a failure
#
# The mode is detected from ~/.browser-visit-logger/config.json (present
# means multi-laptop).  Override with --mode.
#
# Usage:
#   bash browser-visit-tools/check_install.sh
#   bash browser-visit-tools/check_install.sh --mode single|multi
#
# Exit status: 0 if nothing is MISSING or WARN, 1 otherwise, 2 on bad args.
# The VM side of the multi-laptop setup is not checked (it's remote; use
# `manage_sync_server.py health`).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGGER_DIR="$REPO_ROOT/browser-visit-logger"

MODE="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$MODE" in auto|single|multi) ;; *) echo "--mode must be auto, single or multi" >&2; exit 2 ;; esac

# ---------------------------------------------------------------------------
# Paths (mirroring install.sh / install_laptop.sh / Paths.swift)
# ---------------------------------------------------------------------------
KEY_PEM="$LOGGER_DIR/native-host/generated_key.pem"
EXT_DIR="$LOGGER_DIR/extension"
EXT_MANIFEST="$EXT_DIR/manifest.json"
SWIFT_BUILD="$LOGGER_DIR/swift/.build/release"
APP_PARENT="$HOME/Library/Application Support/browser-visit-logger"
HOST_APP="$APP_PARENT/BrowserVisitLoggerHost.app"
HOST_EXEC="$HOST_APP/Contents/MacOS/BrowserVisitLoggerHost"
VERIFIER_APP="$APP_PARENT/BrowserVisitLoggerVerifier.app"
VERIFIER_EXEC="$VERIFIER_APP/Contents/MacOS/BrowserVisitLoggerVerifier"
HOST_NAME="com.browser.visit.logger"
VERIFIER_LABEL="com.browser.visit.logger.snapshot_verifier"
MOVER_LABEL="com.browser.visit.logger.snapshot_mover"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
USER_DOMAIN="gui/$(id -u)"
CONFIG_DIR="${BVL_CONFIG_DIR:-$HOME/.browser-visit-logger}"
CONFIG_FILE="$CONFIG_DIR/config.json"
TLS_DIR="$CONFIG_DIR/tls"
SYNC_VENV="$CONFIG_DIR/sync-venv"
STUBS_DIR="$REPO_ROOT/browser-visit-sync-server/gen/syncpb_py"
MCP_VENV="$REPO_ROOT/browser-visit-mcp/.venv"
TOOLS_VENV="$SCRIPT_DIR/.venv"
CHROME_ROOTS=(
  "$HOME/Library/Application Support/Google/Chrome"
  "$HOME/Library/Application Support/Google/Chrome Canary"
  "$HOME/Library/Application Support/Chromium"
)

if [[ "$MODE" == "auto" ]]; then
  if [[ -f "$CONFIG_FILE" ]]; then MODE="multi"; else MODE="single"; fi
  MODE_SOURCE="detected"
else
  MODE_SOURCE="from --mode"
fi

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_OK=$'\e[32m'; C_BAD=$'\e[31m'; C_WARN=$'\e[33m'; C_DIM=$'\e[2m'; C_B=$'\e[1m'; C_0=$'\e[0m'
else
  C_OK=""; C_BAD=""; C_WARN=""; C_DIM=""; C_B=""; C_0=""
fi
N_OK=0; N_MISSING=0; N_WARN=0
# N_PRESENT counts project components found on this machine (OK or WARN),
# to tell "partially installed" from "nothing installed".  Sections that
# aren't project components (prerequisites, user data) turn COUNTS off.
N_PRESENT=0; COUNTS=1

section() { COUNTS=1; printf '\n%s%s%s\n' "$C_B" "$1" "$C_0"; }

# report STATUS "item" ["detail"]
report() {
  local status="$1" item="$2" detail="${3:-}" color=""
  case "$status" in
    OK)      color="$C_OK";   N_OK=$((N_OK+1)) ;;
    MISSING) color="$C_BAD";  N_MISSING=$((N_MISSING+1)) ;;
    WARN)    color="$C_WARN"; N_WARN=$((N_WARN+1)) ;;
    *)       color="$C_DIM" ;;
  esac
  if [[ $COUNTS -eq 1 && ( "$status" == OK || "$status" == WARN ) ]]; then
    N_PRESENT=$((N_PRESENT+1))
  fi
  printf '  %s%-7s%s  %s' "$color" "$status" "$C_0" "$item"
  # Fix-it hints on N/A items are just noise.
  [[ -n "$detail" && "$status" != "N/A" ]] && printf '  %s— %s%s' "$C_DIM" "$detail" "$C_0"
  printf '\n'
}

# expect_for MODE: status to use when an item expected only in MODE is absent.
absent_status() { [[ "$1" == "$MODE" || "$1" == "both" ]] && echo MISSING || echo N/A; }

have() { command -v "$1" >/dev/null 2>&1; }
HAVE_PY=0; have python3 && HAVE_PY=1

# json_get FILE KEY.PATH — print a value from a JSON file (empty if absent).
json_get() {
  [[ $HAVE_PY -eq 1 ]] || return 0
  python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, sys
try:
    v = json.load(open(sys.argv[1]))
    for k in sys.argv[2].split('.'):
        v = v[k]
    print(json.dumps(v) if isinstance(v, (list, dict)) else v)
except Exception:
    pass
PY
}

launchd_loaded() { launchctl print "$USER_DOMAIN/$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
printf '%sBrowser Visit Logger — install check%s\n' "$C_B" "$C_0"
printf 'Repo: %s\n' "$REPO_ROOT"
printf 'Mode: %s-laptop (%s)\n' "$MODE" "$MODE_SOURCE"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo
  echo "This checker targets macOS laptops (the native host is macOS-only)."
  exit 1
fi

# ---------------------------------------------------------------------------
section "Prerequisites"
COUNTS=0  # system tools, not part of the install
# ---------------------------------------------------------------------------
for cmd in python3 openssl codesign swift; do
  if have "$cmd"; then report OK "$cmd" "$(command -v "$cmd")"
  else report MISSING "$cmd" "needed by install.sh"; fi
done

# ---------------------------------------------------------------------------
section "Core install (browser-visit-logger/install.sh)"
# ---------------------------------------------------------------------------

# Extension signing key + the extension ID derived from it.
EXT_ID=""
if [[ -f "$KEY_PEM" ]]; then
  if have openssl && [[ $HAVE_PY -eq 1 ]]; then
    EXT_ID="$(openssl rsa -in "$KEY_PEM" -pubout -outform DER 2>/dev/null | python3 -c '
import sys, hashlib
d = hashlib.sha256(sys.stdin.buffer.read()).digest()
print("".join(chr(97 + n) for b in d[:16] for n in (b >> 4, b & 15)))')"
  fi
  report OK "Extension signing key" "$KEY_PEM"
else
  report MISSING "Extension signing key" "$KEY_PEM"
fi

# Generated extension manifest with the public key embedded.
if [[ -f "$EXT_MANIFEST" ]]; then
  MANIFEST_KEY="$(json_get "$EXT_MANIFEST" key)"
  EXPECTED_KEY=""
  if [[ -f "$KEY_PEM" ]] && have openssl; then
    EXPECTED_KEY="$(openssl rsa -in "$KEY_PEM" -pubout -outform DER 2>/dev/null | base64 | tr -d '\n')"
  fi
  if [[ -z "$MANIFEST_KEY" ]]; then
    report WARN "Extension manifest.json" "no \"key\" field; re-run install.sh"
  elif [[ -n "$EXPECTED_KEY" && "$MANIFEST_KEY" != "$EXPECTED_KEY" ]]; then
    report WARN "Extension manifest.json" "key doesn't match generated_key.pem; re-run install.sh"
  else
    report OK "Extension manifest.json" "${EXT_ID:+extension ID $EXT_ID}"
  fi
else
  report MISSING "Extension manifest.json" "$EXT_MANIFEST"
fi

# Swift build output (build artifacts copied into the app bundles).
COUNTS=0  # build output in the repo, not installed onto the host
for bin in BVLHost BVLVerifier; do
  if [[ -x "$SWIFT_BUILD/$bin" ]]; then report OK "Swift binary $bin" "$SWIFT_BUILD/$bin"
  else report INFO "Swift binary $bin" "not built (only needed while running install.sh)"; fi
done
COUNTS=1

# check_bundle LABEL APP EXEC EXPECTED_MODE
check_bundle() {
  local label="$1" app="$2" exe="$3" want="$4"
  if [[ ! -d "$app" ]]; then
    report "$(absent_status "$want")" "$label" "$app"
    return
  fi
  if [[ "$want" != "both" && "$want" != "$MODE" ]]; then
    report WARN "$label" "present but not used in $MODE-laptop mode; run browser-visit-tools/uninstall_laptop_legacy.sh"
    return
  fi
  if [[ ! -x "$exe" ]]; then
    report WARN "$label" "bundle exists but executable is missing: $exe"
  elif ! codesign --verify "$app" >/dev/null 2>&1; then
    report WARN "$label" "code signature invalid; re-run install.sh"
  else
    report OK "$label" "$app"
  fi
}
check_bundle "Host app bundle (signed)" "$HOST_APP" "$HOST_EXEC" both

if [[ -f "$HOST_APP/Contents/Resources/native-host/sync_client.py" ]]; then
  report OK "sync_client.py bundled in host app"
elif [[ -d "$HOST_APP" ]]; then
  # install.sh always bundles it, so a missing copy means a stale host app.
  report MISSING "sync_client.py bundled in host app" "host app predates current install.sh; re-run it"
else
  report MISSING "sync_client.py bundled in host app" "host app not installed"
fi

# Native messaging manifest: any Chrome-family browser counts.
NM_FOUND=0
for root in "${CHROME_ROOTS[@]}"; do
  nm="$root/NativeMessagingHosts/$HOST_NAME.json"
  [[ -f "$nm" ]] || continue
  NM_FOUND=1
  browser="$(basename "$root")"
  nm_path="$(json_get "$nm" path)"
  nm_origins="$(json_get "$nm" allowed_origins)"
  if [[ "$nm_path" != "$HOST_EXEC" ]]; then
    report WARN "Native messaging manifest ($browser)" "path is '$nm_path', expected the host app executable"
  elif [[ ! -x "$nm_path" ]]; then
    report WARN "Native messaging manifest ($browser)" "points at $nm_path, which doesn't exist"
  elif [[ -n "$EXT_ID" && "$nm_origins" != *"chrome-extension://$EXT_ID/"* ]]; then
    report WARN "Native messaging manifest ($browser)" "allowed_origins doesn't include extension $EXT_ID; re-run install.sh"
  else
    report OK "Native messaging manifest ($browser)" "$nm"
  fi
done
[[ $NM_FOUND -eq 1 ]] || report MISSING "Native messaging manifest" "none in any Chrome/Chromium NativeMessagingHosts dir"

# Verifier: single-laptop only (runs on the VM in multi-laptop mode).
check_bundle "Verifier app bundle (signed)" "$VERIFIER_APP" "$VERIFIER_EXEC" single

VERIFIER_PLIST="$LAUNCH_DIR/$VERIFIER_LABEL.plist"
if [[ -f "$VERIFIER_PLIST" ]]; then
  if [[ "$MODE" == "multi" ]]; then
    report WARN "Verifier LaunchAgent" "present but verification runs on the VM; run browser-visit-tools/uninstall_laptop_legacy.sh"
  elif launchd_loaded "$VERIFIER_LABEL"; then
    report OK "Verifier LaunchAgent (plist + loaded)" "$VERIFIER_PLIST"
  else
    report WARN "Verifier LaunchAgent" "plist exists but not loaded: launchctl bootstrap $USER_DOMAIN \"$VERIFIER_PLIST\""
  fi
else
  report "$(absent_status single)" "Verifier LaunchAgent" "$VERIFIER_PLIST"
fi

if [[ -f "$LAUNCH_DIR/$MOVER_LABEL.plist" ]] || launchd_loaded "$MOVER_LABEL"; then
  report WARN "Obsolete snapshot_mover LaunchAgent" "still present; re-run install.sh to remove it"
else
  COUNTS=0  # reports an absence, so it isn't evidence of an install
  report OK "Obsolete snapshot_mover LaunchAgent removed"
  COUNTS=1
fi

# ---------------------------------------------------------------------------
section "Chrome extension"
# ---------------------------------------------------------------------------
if [[ -z "$EXT_ID" ]]; then
  report INFO "Extension loaded in Chrome" "can't check without the signing key"
elif [[ $HAVE_PY -eq 0 ]]; then
  report INFO "Extension loaded in Chrome" "can't check without python3"
else
  PROFILES="$(python3 - "$EXT_ID" "${CHROME_ROOTS[@]}" <<'PY' 2>/dev/null
import json, os, sys, glob
ext_id, roots = sys.argv[1], sys.argv[2:]
hits = []
for root in roots:
    for prof in sorted(glob.glob(os.path.join(root, "*"))):
        for fname in ("Secure Preferences", "Preferences"):
            p = os.path.join(prof, fname)
            try:
                s = json.load(open(p)).get("extensions", {}).get("settings", {})
            except Exception:
                continue
            if ext_id in s:
                hits.append(f"{os.path.basename(root)}/{os.path.basename(prof)}")
                break
print(", ".join(hits))
PY
)"
  if [[ -n "$PROFILES" ]]; then
    report OK "Extension loaded in Chrome" "$EXT_ID in $PROFILES"
  else
    report MISSING "Extension loaded in Chrome" "load unpacked from $EXT_DIR at chrome://extensions"
  fi
fi

# ---------------------------------------------------------------------------
section "Multi-laptop sync (browser-visit-tools/install_laptop.sh)"
# ---------------------------------------------------------------------------
M="$(absent_status multi)"

if [[ -f "$CONFIG_FILE" ]]; then
  report OK "Machine config" "$CONFIG_FILE"
  for key in machine_id sync_server gdrive_account; do
    val="$(json_get "$CONFIG_FILE" "$key")"
    if [[ -n "$val" ]]; then report OK "  config: $key" "$val"
    else report MISSING "  config: $key" "not set in config.json"; fi
  done
  ACCOUNT="$(json_get "$CONFIG_FILE" gdrive_account)"
  if [[ -n "$ACCOUNT" ]]; then
    GD="$HOME/Library/CloudStorage/GoogleDrive-$ACCOUNT/My Drive"
    if [[ -d "$GD" ]]; then report OK "Google Drive mount" "$GD"
    else report MISSING "Google Drive mount" "$GD not found; is Google Drive for desktop signed in?"; fi
  fi
else
  report "$M" "Machine config" "$CONFIG_FILE"
fi

if [[ -x "$SYNC_VENV/bin/python" ]]; then
  if "$SYNC_VENV/bin/python" -c 'import grpc' >/dev/null 2>&1; then
    report OK "Sync venv with grpcio" "$SYNC_VENV"
  else
    report WARN "Sync venv" "exists but grpcio isn't importable; re-run install_laptop.sh"
  fi
else
  report "$M" "Sync venv with grpcio" "$SYNC_VENV"
fi

if [[ -f "$STUBS_DIR/sync_pb2.py" && -f "$STUBS_DIR/sync_pb2_grpc.py" ]]; then
  report OK "gRPC Python stubs generated" "$STUBS_DIR"
else
  report "$M" "gRPC Python stubs generated" "$STUBS_DIR"
fi

if [[ -f "$HOST_APP/Contents/Resources/native-host/syncpb_py/sync_pb2.py" ]]; then
  report OK "gRPC stubs bundled in host app"
else
  report "$M" "gRPC stubs bundled in host app" "re-run install_laptop.sh"
fi

for f in client.crt client.key server-ca.crt; do
  p="$TLS_DIR/$f"
  if [[ ! -f "$p" ]]; then
    report "$M" "TLS $f" "$p"
  elif [[ "$f" == "client.key" ]] && [[ "$(stat -f '%Lp' "$p")" != "600" ]]; then
    report WARN "TLS $f" "permissions are $(stat -f '%Lp' "$p"), expected 600"
  else
    report OK "TLS $f" "$p"
  fi
done

# ---------------------------------------------------------------------------
section "Optional components"
# ---------------------------------------------------------------------------
if [[ -x "$MCP_VENV/bin/python" ]] && "$MCP_VENV/bin/python" -c 'import mcp' >/dev/null 2>&1; then
  report OK "MCP server venv (browser-visit-mcp)" "$MCP_VENV"
elif [[ -x "$MCP_VENV/bin/python" ]]; then
  report WARN "MCP server venv (browser-visit-mcp)" "exists but 'mcp' isn't installed; run .venv/bin/pip install -e ."
else
  report INFO "MCP server venv (browser-visit-mcp)" "not set up (see browser-visit-mcp/README.md)"
fi

if [[ -x "$TOOLS_VENV/bin/python" ]] && "$TOOLS_VENV/bin/python" -c 'import boto3, cryptography' >/dev/null 2>&1; then
  report OK "Tools venv (boto3, cryptography)" "$TOOLS_VENV"
else
  report INFO "Tools venv (boto3, cryptography)" "not set up; only needed for VM admin tools"
fi

# ---------------------------------------------------------------------------
section "Runtime data (created on first use)"
COUNTS=0  # user data, not part of the install
# ---------------------------------------------------------------------------
DB="${BVL_DB_FILE:-$HOME/browser-visits.db}"
if [[ -f "$DB" ]]; then report OK "Visits database" "$DB"
else report INFO "Visits database" "$DB not created yet (appears after the first page visit)"; fi
if [[ -f "$HOME/browser-visits-host.log" ]]; then report OK "Host log" "$HOME/browser-visits-host.log"
else report INFO "Host log" "not created yet (Chrome hasn't launched the host)"; fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
if [[ $N_PRESENT -eq 0 ]]; then
  printf '%sNothing is installed.%s\n' "$C_BAD" "$C_0"
  exit 1
fi
if [[ $N_MISSING -eq 0 && $N_WARN -eq 0 ]]; then
  printf '%sComplete:%s %d items OK for %s-laptop mode.\n' "$C_OK" "$C_0" "$N_OK" "$MODE"
  exit 0
fi
printf '%sIncomplete:%s %d OK, %d missing, %d warnings (%s-laptop mode).\n' \
  "$C_BAD" "$C_0" "$N_OK" "$N_MISSING" "$N_WARN" "$MODE"
exit 1
