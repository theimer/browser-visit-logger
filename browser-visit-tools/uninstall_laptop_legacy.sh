#!/usr/bin/env bash
# uninstall_laptop_legacy.sh — Remove the legacy snapshot verifier
# LaunchAgent + binary from this laptop.  Used when migrating to the
# multi-laptop architecture (where verification runs on the EC2 VM).
#
# Idempotent: if a piece is already gone, it's skipped.  Never touches
# user data (~/browser-visits-*.log, ~/browser-visits.db, snapshot
# directories, the new .browser-visit-logger config dir).
#
# Usage:
#   bash browser-visit-tools/uninstall_laptop_legacy.sh

set -euo pipefail

VERIFIER_LABEL="com.browser.visit.logger.snapshot_verifier"
MOVER_LABEL="com.browser.visit.logger.snapshot_mover"  # older generation
LAUNCH_DIR="$HOME/Library/LaunchAgents"
APP_PARENT="$HOME/Library/Application Support/browser-visit-logger"
VERIFIER_APP="$APP_PARENT/BrowserVisitLoggerVerifier.app"

info() { echo "[uninstall] $*"; }
warn() { echo "[uninstall] WARN: $*" >&2; }

unload_agent() {
  local label="$1"
  local plist="$LAUNCH_DIR/${label}.plist"
  if [[ ! -f "$plist" ]]; then
    info "no LaunchAgent plist for $label (already gone)"
    return 0
  fi
  if launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null; then
    info "booted out $label"
  else
    # Fall back to the older `unload` API; ignore failure (may not be loaded).
    launchctl unload "$plist" 2>/dev/null || true
    info "unloaded $label (legacy API)"
  fi
}

remove_plist() {
  local label="$1"
  local plist="$LAUNCH_DIR/${label}.plist"
  if [[ -f "$plist" ]]; then
    rm -f "$plist"
    info "removed $plist"
  fi
}

remove_app() {
  local path="$1"
  if [[ -d "$path" ]]; then
    rm -rf "$path"
    info "removed $path"
  else
    info "no app at $path (already gone)"
  fi
}

info "Removing legacy snapshot-verifier LaunchAgent (now runs on the VM)"
unload_agent "$VERIFIER_LABEL"
remove_plist "$VERIFIER_LABEL"

# Also clean up the even-older snapshot_mover LaunchAgent if any
# install from before the verifier consolidation is still lingering.
unload_agent "$MOVER_LABEL"
remove_plist "$MOVER_LABEL"

remove_app "$VERIFIER_APP"

info ""
info "Done. The following was preserved (user data + new sync infra):"
info "  - ~/browser-visits-*.log"
info "  - ~/browser-visits.db"
info "  - ~/Documents/browser-visit-logger/  (legacy iCloud archive)"
info "  - ~/.browser-visit-logger/           (machine config + mTLS)"
info "  - BrowserVisitLoggerHost.app         (still serves Chrome native msgs)"
info ""
info "Next:"
info "  1. Run browser-visit-tools/migrate_icloud_to_gdrive.py to copy the"
info "     iCloud archive into Google Drive and rewrite DB paths."
info "  2. Verify launchctl no longer lists the verifier:"
info "       launchctl list | grep $VERIFIER_LABEL  # expected: empty"
