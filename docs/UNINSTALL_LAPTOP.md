# Uninstalling from a laptop

`browser-visit-tools/uninstall_laptop.py` removes what `install.sh` and
`install_laptop.sh` put on this machine. It is stdlib-only, never touches
AWS, and is independent of `uninstall_aws.py` (see
[UNINSTALL_AWS.md](UNINSTALL_AWS.md)).

```
python3 uninstall_laptop.py --dry-run [--purge-data] [--repo-artifacts]
python3 uninstall_laptop.py [--purge-data] [--repo-artifacts]
```

## Flags

| Flag | Meaning |
|---|---|
| `-n`, `--dry-run` | Print what would be removed; change nothing, spawn no processes. |
| `-y`, `--yes` | Skip the prompt. Otherwise type `yes`. |
| `--purge-data` | Also delete user data and identity (below). |
| `--repo-artifacts` | Also delete generated build outputs in the git checkout. |

Non-interactive stdin without `--yes` exits 2. Declining exits 1. A failed
removal is reported, the remaining items still run, and the exit code is 1.

## What is removed

| Tier | Items |
|---|---|
| core (always) | LaunchAgents `com.browser.visit.logger.snapshot_verifier` and `snapshot_mover` (after `launchctl bootout`); `BrowserVisitLoggerHost.app` and `BrowserVisitLoggerVerifier.app` under `~/Library/Application Support/browser-visit-logger/`; `com.browser.visit.logger.json` in the Chrome, Chrome Canary and Chromium `NativeMessagingHosts` directories (macOS) and `~/.config/google-chrome`, `~/.config/chromium` (Linux); `~/.browser-visit-logger/sync-venv/` and `sync.lock`; the two parent directories if they end up empty |
| `--repo-artifacts` | `browser-visit-logger/extension/manifest.json`; `browser-visit-logger/swift/.build/`; `browser-visit-sync-server/gen/syncpb_py/` |
| `--purge-data` | all of `~/.browser-visit-logger/` (`config.json`, `tls/`, `vm.json`, ...); `browser-visit-logger/native-host/generated_key.pem`; `~/browser-visits.db` plus `-wal`, `-shm`, `-journal`; `~/browser-visits-host.log`, `~/browser-visits-verifier.log`, `~/browser-visits-YYYY-MM-DD.log` |

By default your data is kept. **`--purge-data` is irreversible in effect:**
the signing key fixes the Chrome extension ID, and `tls/client.key` is
enrolled on the server.

Everything is rooted at `$HOME`. `BVL_CONFIG_DIR` and `BVL_DB_FILE` are
ignored on purpose. Symlinks are removed, never followed. The script refuses
to run if `HOME` is `/` or not absolute.

## Never touched (no flag)

- the Google Drive / iCloud snapshot archive (`~/Documents/browser-visit-logger`)
- `~/Downloads/browser-visit-snapshots`
- Chrome profile data
- the loaded extension
- macOS privacy (TCC) grants
- the `browser-visit-mcp` and tools venvs (not created by the installers)

## Manual follow-ups

1. Remove the extension at `chrome://extensions`.
2. Review Files & Folders / Full Disk Access entries for the
   `BrowserVisitLogger*` apps in System Settings > Privacy & Security.
3. Delete the snapshot archive yourself if you no longer want it.
4. Backend cleanup: run `uninstall_aws.py` first, before `--purge-data`
   (which deletes `vm.json`).

## Relationship to `uninstall_laptop_legacy.sh`

The legacy script is narrower and preserves the Host app; `install_laptop.sh`
still calls it. This tool is separate and also removes the Host app, the
manifests and the older mover agent. The legacy script is unchanged.

## Verifying

After uninstalling, run `browser-visit-tools/check_install.sh`; it should
report the components as missing.
