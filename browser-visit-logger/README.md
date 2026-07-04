# Browser Visit Logger

A Chrome extension + native-messaging host that records every page you
visit to a local SQLite database (and a TSV log file), lets you tag
pages of interest from a popup, and archives full-page snapshots (MHTML
or PDF) into iCloud-synced daily folders sealed by a tab-delimited
manifest once each day has passed.

Everything runs locally by default.  An **optional multi-laptop sync
mode** ([covered below](#multi-laptop-sync-mode)) layers a gRPC
client on top so several laptops can share state via a central EC2
service; with multi-laptop mode disabled, nothing is sent anywhere.

---

## What gets recorded

Two things, on every visit:

1. **An auto-log entry** — URL, page title, and an ISO timestamp, the
   moment the navigation completes. One row per URL (re-visits don't
   duplicate the row, but the original timestamp is preserved).
2. **Optional tags** — when you click the toolbar icon, a popup lets you
   mark the current page as one of:
   - **★ Of Interest** — set a flag on the visit row.
   - **✓ Read** — record a "read" event with its own timestamp and save
     a full snapshot of the page.
   - **~ Skimmed** — record a "skimmed" event with its own timestamp and
     save a full snapshot.

Snapshots are MHTML for normal pages, PDF for `.pdf` URLs (downloaded
directly to avoid Chrome's mangled PDF viewer output). Each file is named
`<YYYY-MM-DDTHH-MM-SSZ>-<sha256>.<ext>`, where the prefix is derived
from the click timestamp so the file's name is permanent and globally
sortable.

Snapshots land first in `~/Downloads/browser-visit-snapshots/` (Chrome's
downloads API forces files there).  The native messaging host then
archives them *synchronously* — same invocation as the tag write — into
`~/Documents/browser-visit-logger/snapshots/<YYYY-MM-DD>/` and chmods
them read-only.  A daily `snapshot_verifier` LaunchAgent later writes a
`MANIFEST.tsv` summarising each completed day, moves that day's per-day
log into the sealed directory, and re-checks already-sealed days for
drift.

Both processes run inside ad-hoc-signed `.app` bundles so they have
stable TCC identities — macOS' Privacy & Security framework grants
~/Downloads and ~/Documents access to apps, not to bare `python3`
invocations.  The first time you tag a page (and the first time the
verifier ticks) macOS asks for the relevant Files & Folders permission;
clicking Allow grants it for the lifetime of the app's signature.

---

## Storage layout

| Path | What it is |
|------|------------|
| `~/browser-visits-<YYYY-MM-DD>.log` | Per-day TSV append-only log of every visit and tag action.  One file per UTC day; the sealer collects each completed day's log into the matching iCloud sealed dir |
| `~/browser-visits.db` | SQLite database — `visits`, `read_events`, `skimmed_events`, `snapshots`, `mover_errors` |
| `~/browser-visits-host.log` | Native host process log (rotated, 1 MiB × 3) |
| `~/browser-visits-verifier.log` | Verifier LaunchAgent process log |
| `~/Library/Application Support/browser-visit-logger/BrowserVisitLoggerHost.app/` | Code-signed app bundle wrapping the Swift `BVLHost` Mach-O binary (Chrome's native-messaging host) |
| `~/Library/Application Support/browser-visit-logger/BrowserVisitLoggerVerifier.app/` | Code-signed app bundle wrapping the Swift `BVLVerifier` Mach-O binary (daily background agent) |
| `~/Downloads/browser-visit-snapshots/` | Chrome's drop point — `BVLHost` archives files away on every tag, the verifier sweeps stragglers daily |
| `~/Documents/browser-visit-logger/snapshots/<YYYY-MM-DD>/` | Sealed daily archive: read-only snapshot files + read-only `MANIFEST.tsv` + read-only `browser-visits-<YYYY-MM-DD>.log` (single-laptop default; multi-laptop mode points the archive at Google Drive instead) |
| `~/.browser-visit-logger/config.json` | Machine config (multi-laptop mode only).  Records `machine_id` + optional `sync_server` endpoint.  Presence of this file is what gates the sync hook in `BVLHost` |
| `~/.browser-visit-logger/tls/` | mTLS material (multi-laptop mode only): `client.crt`, `client.key`, `server-ca.crt` |
| `~/.browser-visit-logger/sync.lock` | flock acquired by `sync_client.py` so back-to-back Chrome events don't spawn overlapping syncs |
| `~/browser-visits-peers/<machine_id>/browser-visits-<YYYY-MM-DD>.log` | Local mirror of peer machines' logs, written by `sync_client.py` on pull |

All paths can be overridden via `BVL_*` environment variables — see
[Configuration](#configuration).

---

## Database schema

```sql
visits (
    url         TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,                 -- first-visit timestamp
    title       TEXT NOT NULL DEFAULT '',
    of_interest TEXT,                          -- non-NULL once tagged
    read        INTEGER NOT NULL DEFAULT 0,    -- count of read clicks
    skimmed     INTEGER NOT NULL DEFAULT 0     -- count of skimmed clicks
)

read_events / skimmed_events (
    url       TEXT NOT NULL,
    timestamp TEXT NOT NULL,                   -- when the user tagged
    filename  TEXT NOT NULL DEFAULT '',        -- snapshot basename
    directory TEXT NOT NULL DEFAULT '<Downloads dir>',
    PRIMARY KEY (url, timestamp)
)

snapshots (
    date   TEXT PRIMARY KEY,                   -- 'YYYY-MM-DD' (UTC)
    sealed INTEGER NOT NULL DEFAULT 0          -- 0 = unsealed, 1 = sealed
)

mover_errors (
    key        TEXT PRIMARY KEY,               -- '<op>:<target>'
    operation  TEXT NOT NULL,                  -- 'move'|'seal'|'rewrite_manifest'|'top_level'|'sync_push'|'sync_pull'|'sync_handshake'
    target     TEXT NOT NULL,                  -- file path / date dir / VM endpoint
    message    TEXT NOT NULL,                  -- last exception message
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 1,
    notified   INTEGER NOT NULL DEFAULT 0,
    immediate  INTEGER NOT NULL DEFAULT 0
)

sync_state (                                   -- multi-laptop mode only
    machine_id            TEXT PRIMARY KEY,    -- this laptop or any peer
    cursor_log_date       TEXT NOT NULL DEFAULT '',
    cursor_line_offset    INTEGER NOT NULL DEFAULT 0,
    last_sync_attempt_at  TEXT NOT NULL DEFAULT '',
    last_sync_success_at  TEXT NOT NULL DEFAULT '',
    last_error            TEXT NOT NULL DEFAULT ''
)
```

`sync_state` holds one row per machine the VM has told us about
(including this laptop).  The cursor is the high-water mark of log
records produced by `machine_id` that this laptop has confirmed are
at the VM — for the self row, "successfully pushed and acked"; for
peer rows, "successfully pulled and applied locally".  See
[Multi-laptop sync mode](#multi-laptop-sync-mode).

The `visits` / `*_events` tables are owned by `BVLHost`; the `snapshots`
and `mover_errors` tables are co-owned by `BVLHost` and `BVLVerifier`.
`BVLHost` INSERTs a `snapshots` row (`sealed=0`) on every write
invocation (covers activity-only days), and the synchronous archive in
`Archive.forTag` INSERTs one when a file lands in a new daily dir.
The verifier's seal pass flips `sealed=1` once the day has fully
passed.  The seal pass queries this table to find work — no
filesystem rescan needed.  The `mover_errors` table tracks unresolved
failures from any of the passes (move / seal / rewrite_manifest /
manifest_invalid / orphan_file / invalid_filename / top_level) so they
can be surfaced to the user — see
[When something goes wrong](#when-something-goes-wrong).

---

## Installation

Tested on macOS with Chrome / Chrome Canary / Chromium.  Linux works
for the extension and host but not the LaunchAgent-driven verifier.

**Requirements:** `python3` ≥ 3.9, `openssl`.  On macOS, `codesign`
(always available with the Xcode Command Line Tools).

```bash
git clone <repo>
cd browser-visit-logger
bash install.sh
```

`install.sh` is idempotent.  It:

1. Generates a stable RSA key pair (once) and pins the extension ID
   via the `key` field in `manifest.json`.
2. On macOS, builds the Swift binaries (`swift build -c release`) and
   materializes two ad-hoc-signed `.app` bundles under
   `~/Library/Application Support/browser-visit-logger/`:
   - `BrowserVisitLoggerHost.app` wraps the `BVLHost` Mach-O binary
     (Chrome's native-messaging host).  The Chrome native-messaging
     manifest's `path` points at this bundle's executable, so the
     spawned process IS the bundle's signed code — TCC attributes file
     accesses to the bundle's identity, which the user can grant
     Files & Folders / Full Disk Access in System Settings.
   - `BrowserVisitLoggerVerifier.app` wraps the `BVLVerifier` Mach-O
     binary (daily background agent) with the same TCC reasoning.
3. Installs the Chrome native-messaging host manifest under each
   detected browser's `NativeMessagingHosts` directory, with `path`
   pointing at the host bundle.
4. Cleans up any previous-generation `snapshot_mover` LaunchAgent
   (it's been folded into the host and the verifier).
5. Installs the verifier LaunchAgent
   (`com.browser.visit.logger.snapshot_verifier`,
   `StartInterval = 86400`s = daily) pointing at the verifier
   bundle's executable.
6. Kicks the verifier once interactively so its first `~/Downloads`
   read triggers the macOS Files & Folders prompt while you're at
   the keyboard.
7. Prints the extension ID and where to load the unpacked extension.

After it finishes:

1. **Fully quit Chrome (⌘Q, not just close the window)** if it's
   running.  Chrome reads the native-messaging manifest once at
   startup, so a re-install's new `path` value only takes effect on
   the next launch.
2. Open `chrome://extensions` and enable **Developer mode**.
3. **Load unpacked** → select the `extension/` directory.
4. The extension ID Chrome displays should match the one printed by
   the installer.
5. **Tag any page once** (★ / ✓ / ~) — macOS will prompt for
   "BrowserVisitLoggerHost would like to access files in your
   Downloads folder.  Allow / Don't Allow."  Click Allow.  The same
   prompt may already have appeared for `BrowserVisitLoggerVerifier`
   from step 6 of the install.
6. Confirm the pipeline is healthy:
   ```bash
   ./verify_snapshot_directory --show-errors        # → "No pending mover errors."
   tail ~/browser-visits-host.log                   # → "Moved … (read-only)"
   sqlite3 ~/browser-visits.db "SELECT * FROM visits ORDER BY timestamp DESC LIMIT 10;"
   ```

### Re-running `install.sh`

Every `install.sh` run rebuilds the Swift binaries and re-codesigns
the `.app` bundles ad-hoc.  Even when the binary contents are
byte-identical, re-signing rewrites `Contents/_CodeSignature/` and
the resulting cdhash often differs — which TCC treats as a different
app, **invalidating any prior Files & Folders / Full Disk Access
grants**.

Symptom of this: tagging a page logs `ERROR Failed to move … [Errno 1]
Operation not permitted` to `~/browser-visits-host.log`, or
`./verify_snapshot_directory --show-errors` reports a fresh
`top_level: Operation not permitted` row.

Recovery, after every re-install:

1. Open **System Settings → Privacy & Security → Full Disk Access**
   (or **Files and Folders → Downloads Folder + Documents Folder**).
2. Find `BrowserVisitLoggerHost` and `BrowserVisitLoggerVerifier`.
   If they're listed, remove them (click, then `−`).
3. Re-add them: click `+` and navigate to (or drag from Finder)
   ```
   ~/Library/Application Support/browser-visit-logger/BrowserVisitLoggerHost.app
   ~/Library/Application Support/browser-visit-logger/BrowserVisitLoggerVerifier.app
   ```
   Toggle each ON.
4. Quit Chrome (⌘Q) and reopen.
5. Verify with `./verify_snapshot_directory --show-errors`.

### Changing the verifier cadence (macOS)

Edit `StartInterval` in `~/Library/LaunchAgents/com.browser.visit.logger.snapshot_verifier.plist`,
then reload:

```bash
launchctl bootout   gui/$(id -u)/com.browser.visit.logger.snapshot_verifier
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.browser.visit.logger.snapshot_verifier.plist
```

Common values: `3600` (hourly), `86400` (daily, the default),
`604800` (weekly).

---

## Using the extension

**Auto-logging is automatic.** Every page you finish navigating to is
logged. There's nothing to click.

**Tagging** — click the toolbar icon to open the popup. It shows the
current visit's history (first visited / of-interest / read / skimmed
timestamps) and three buttons:

- **★ Of Interest** — quick boolean flag.
- **✓ Read** — captures a snapshot, records a read event.
- **~ Skimmed** — captures a snapshot, records a skimmed event.

Read and Skimmed can each be clicked multiple times on the same URL —
each click creates a separate event row (its own timestamp + snapshot).

The popup stays open while the snapshot saves; on success it closes
itself, on failure it shows the error and re-enables the buttons.

**Address-bar icon color** reflects the current tab's tag state at a
glance — a white "B" on a colored disk:

| State | Color |
|-------|-------|
| Untagged (or new tab) | Gray |
| ★ Of Interest only | Orange |
| ~ Skimmed (no Read) | Yellow |
| ✓ Read (overrides Skimmed and Of Interest) | Green |

The color refreshes on tab switch, on navigation completion, and
immediately after a successful tag click.  The background queries
`BVLHost` for the current URL's record — if the host is unavailable
or the URL isn't `http(s)`, the icon falls back to gray.

---

## CLI scripts

All scripts live at the repo root or under `native-host/`.  They share
the same `BVL_*` env-var conventions and accept overriding flags so
they're safe to point at test data.

Three of the wrappers delegate to Python scripts; one (`verify_snapshot_directory`)
delegates to the Swift `BVLVerifier` binary built under `swift/.build/release/`.
Each wrapper forwards all arguments verbatim and intercepts `--help` / `-h`
to print a one-line wrapper note before delegating.

| Wrapper | Underlying tool |
|---------|------------------|
| `./seal_snapshot_directory`   | `native-host/snapshot_sealer.py` (Python) |
| `./verify_snapshot_directory` | `swift/.build/release/BVLVerifier` (Swift Mach-O) |
| `./reset_visits_data`         | `reset.py` (Python) |
| `./rebuild_visits_data`       | `native-host/visits_rebuilder.py` (Python) |

### `install.sh`

One-shot installer; see [Installation](#installation). Re-run it any
time the extension or host changes location, or to refresh the
LaunchAgent.

```bash
bash install.sh
```

### `swift/Sources/BVLHost/` (Mach-O binary)

The native messaging host invoked by Chrome.  **Don't run this directly**
— it speaks Chrome's framed-stdio protocol (4-byte length prefix +
JSON), not a normal CLI.  Chrome launches the bundled binary
once per `sendNativeMessage` call.  Reads the message, writes a per-day
log line + DB row, archives the just-tagged snapshot synchronously
from `~/Downloads/browser-visit-snapshots/` to its iCloud date subdir,
chmods the destination 0o444, writes a result line, and exits.

Built from `swift/Sources/BVLHost/main.swift` plus the shared
`swift/Sources/BVLCore/` library (schema, DB, archive helpers, etc.).
The compiled Mach-O lives at `swift/.build/release/BVLHost` after
`swift build -c release` and is copied into
`BrowserVisitLoggerHost.app/Contents/MacOS/BrowserVisitLoggerHost`
during install.

### `snapshot_mover.py` (in `../browser-visit-snapshot-lib/`)

**Shared library only.**  The production move / seal / verify pipeline
runs in Swift now (see `BVLHost` above and `BVLVerifier` below); this
file's `_seal_directory`, `_ensure_snapshots_table`, manifest constants,
and other utilities are imported by the surviving Python CLI tools
(`snapshot_sealer.py`, `visits_rebuilder.py`) *and* the VM-side sealer
([`browser-visit-sync-server/sealer/`](../browser-visit-sync-server/sealer/)).
Because it is shared across the laptop and VM sides it lives in its own
top-level component, [`browser-visit-snapshot-lib/`](../browser-visit-snapshot-lib/),
rather than in this laptop directory.

### `native-host/snapshot_sealer.py`

Manual complement to the mover's seal pass. Forces sealing of one
specified directory immediately, regardless of whether its date has
passed. Useful for testing or for closing a directory early.

```bash
# Seal by date (resolved under ICLOUD_SNAPSHOTS_DIR)
python3 native-host/snapshot_sealer.py 2026-04-30

# Seal by absolute or relative path
python3 native-host/snapshot_sealer.py /Users/me/.../snapshots/2026-04-30

# Show what would happen without writing
python3 native-host/snapshot_sealer.py --dry-run 2026-04-30

# Override paths
python3 native-host/snapshot_sealer.py \
    --db /tmp/test.db --dest /tmp/snapshots 2026-04-30
```

Flags:

| Flag | Effect |
|------|--------|
| `--dry-run` | Log what would be sealed without writing the manifest |
| `-v`, `--verbose` | DEBUG log level |
| `--dest DIR` | Override `BVL_ICLOUD_SNAPSHOTS_DIR` |
| `--db FILE` | Override `BVL_DB_FILE` |

Refuses to overwrite an existing manifest (exit code 1). To re-seal,
delete the manifest first.

If the directory's basename is a valid `YYYY-MM-DD` date, the sealer
also upserts a `(date, sealed=1)` row into the `snapshots` table — so
the auto seal pass won't re-process it.  When a per-day log file
exists in `BVL_LOG_DIR` for that date, it's moved into the sealed dir
(chmod `0o444`) as part of the seal — same flow as the auto sealer.
The orphan-log merge pass also runs after the manual seal, so
ad-hoc seals clean up any race orphans in `BVL_LOG_DIR`.
Non-date-named directories get a manifest but no table row and no log.

**Manifest format** — `MANIFEST.tsv`, one header row + one row per file:

```
filename	tag	timestamp	url	title
2026-04-30T10-00-00Z-abc.mhtml	read	2026-04-30T10:00:00Z	https://example.com	Example Page
```

Files in the directory with no matching DB row appear with empty
metadata fields. Tabs / newlines / CRs in titles are sanitised to
spaces.

### `swift/Sources/BVLVerifier/` (Mach-O binary)

The sole background agent.  A daily LaunchAgent invokes the bundled
binary with `--quiet`; you can also run it by hand via
`./verify_snapshot_directory`.  Each default-mode tick (no operation
flag) does, in order:

1. **Sweep** — scan `~/Downloads/browser-visit-snapshots/` for
   stragglers (files left behind by a `BVLHost` crash between Chrome's
   download and the synchronous archive).  Files at least
   `MIN_AGE_SECONDS` old are archived to `<dest>/<YYYY-MM-DD>/`
   (chmod `0o444`, events row updated).  Idempotent — interrupted runs
   recover on the next tick.
2. **Seal** — DB-driven (no filesystem rescan).  For every snapshots
   row where `sealed=0 AND date<today_utc`: write `MANIFEST.tsv`,
   move that day's per-day log into the dir, flip `sealed=1`.  If
   anything fails, the row stays at `sealed=0` and retries next tick.
3. **Orphan-log merge** — reconcile per-day logs left in `BVL_LOG_DIR`
   with their iCloud counterparts (race orphans, lost-snapshots-row
   recovery).
4. **Verify** — for every `sealed=1` directory, check the manifest
   against disk + DB (10 invariants, listed below).  Failures record a
   `manifest_invalid` row in `mover_errors`; subsequent successes
   clear it.
5. **Escalate** — drain pending `mover_errors` rows that have crossed
   the threshold or are immediate-class; pop a Notification Center
   banner.

```bash
# Default — full tick (the canonical background-agent invocation)
./verify_snapshot_directory
./verify_snapshot_directory --quiet           # silence OK lines
./verify_snapshot_directory --dry-run         # plan, don't write

# Verify one directory by date or path (no sweep / seal / escalate)
./verify_snapshot_directory --verify 2026-04-30
./verify_snapshot_directory --verify /Users/.../snapshots/2026-04-30

# Verify every sealed directory in the snapshots table
./verify_snapshot_directory --verify-all

# Inspect / clear pending mover_errors rows
./verify_snapshot_directory --show-errors
./verify_snapshot_directory --clear-errors
./verify_snapshot_directory --clear-error 1
```

Flags:

| Flag | Effect |
|------|--------|
| `--verify DIR_OR_DATE` | Verify one directory; skip sweep/seal/escalate.  Mutually exclusive with the others. |
| `--verify-all` | Verify every `sealed=1` row in the `snapshots` table; skip sweep/seal/escalate. |
| `--show-errors` | Print pending `mover_errors` and exit. |
| `--clear-errors` | Delete every `mover_errors` row and exit. |
| `--clear-error N` | Delete the Nth row (1-indexed, `--show-errors` order) and exit. |
| `--dry-run` | Plan a tick without writing files or the DB. |
| `--quiet` | Print only failure summaries — for background invocation. |
| `--record` | (For `--verify` / `--verify-all`) UPSERT failures into `mover_errors`.  Default-mode ticks always record. |
| `-v`, `--verbose` | DEBUG log level. |
| `--source DIR` | Override `BVL_DOWNLOADS_SNAPSHOTS_DIR`. |
| `--dest DIR` | Override `BVL_ICLOUD_SNAPSHOTS_DIR`. |
| `--db FILE` | Override `BVL_DB_FILE`. |
| `--min-age-seconds N` | Override the sweep age threshold (default 60). |

Verification checks (per directory):

1. `MANIFEST.tsv` exists.
2. Manifest is read-only (mode `0o444`).
3. First line is the canonical header.
4. Every data row has exactly 5 tab-delimited columns; no duplicate filenames.
5. **Every file in the directory** (other than `MANIFEST.tsv` and the
   per-day log) has a conforming snapshot filename — non-conforming
   files are flagged whether or not they appear in the manifest.
6. The set of conforming snapshot files in the directory equals the
   set of filenames listed in the manifest.
7. **Every manifest row has a corresponding events row in the DB**, and
   its `(tag, timestamp, url, title)` matches.  Orphan rows are
   always invalid.
8. **Every conforming file in the directory has an events row in the
   DB.**  Orphan files are always invalid, even when correctly
   excluded from the manifest.
9. The per-day log file is present, a regular file, mode `0o444`.

Exit codes: `0` if every sealed directory passes; `1` if any
directory fails verification, if a target doesn't exist, or on
argument errors.

**LaunchAgent (macOS, installed by `install.sh`)**

`install.sh` registers `com.browser.visit.logger.snapshot_verifier`,
which invokes the verifier bundle's executable with `--quiet` on a
configurable `StartInterval` (default `86400` seconds = **1 day**).
Logs land in `~/browser-visits-verifier.log`.

### `reset.py`

Wipes local data the extension produced. Asks for confirmation by default.

```bash
# Reset everything (per-day visit logs, host log, verifier log, DB, snapshot dirs)
python3 reset.py

# Skip confirmation
python3 reset.py -f

# Reset one thing only
python3 reset.py --log         # all ~/browser-visits-<date>.log files in BVL_LOG_DIR
python3 reset.py --host-log    # host log + verifier log
python3 reset.py --db          # ~/browser-visits.db
python3 reset.py --snapshots   # ~/Downloads/browser-visit-snapshots/
python3 reset.py --icloud      # ~/Documents/browser-visit-logger/  (also wipes per-day logs sealed in iCloud)
```

`--log` only deletes per-day logs that still live in `BVL_LOG_DIR`.
Per-day logs that have already been moved into iCloud sealed dirs are
wiped by `--icloud` — same separation between log and snapshot data.

Respects the same `BVL_*` env vars as the host, so custom paths Just
Work. Safe to run with no targets present — missing files/dirs are
reported and skipped.

### `native-host/visits_rebuilder.py`

Reconstructs `~/browser-visits.db` from two side-channels that survive
DB loss: the per-day on-disk logs (`~/browser-visits-<date>.log` plus
each sealed iCloud subdir's bundled log) and the iCloud snapshot
archive.  Two phases run by default:

1. **Log replay** — every host invocation writes a UUID-prefixed
   action line followed by a result line, pinned to the day the
   invocation started.  The rebuilder enumerates per-day logs from
   `BVL_LOG_DIR` *and* every sealed iCloud subdir, sorts them
   chronologically, and pairs action+result UUIDs across files (so
   the rare cross-day seal-race orphan is handled correctly).
   Successful pairs are re-applied via the same `host.py` helpers
   used at write-time; error pairs are skipped (the DB write didn't
   happen); orphan / malformed lines are counted and trip a non-zero
   exit.  Each replayed log file's date also produces a `snapshots`
   row (mirrors the live INSERT host.py does).
2. **Filesystem rehydration** — iterates every `YYYY-MM-DD`
   subdirectory under the iCloud root, upserts a `snapshots` row
   (`sealed = 1` if `MANIFEST.tsv` exists), and updates each event
   row's `directory` column from Downloads to the date subdir for
   files that have already been moved.

```bash
# Rebuild against the configured paths (DROPs and recreates
# visits / read_events / skimmed_events / snapshots first)
./rebuild_visits_data

# Skip the wipe; rely on idempotency
./rebuild_visits_data --no-truncate

# Phase 1 only (e.g. logs present, iCloud unreachable)
./rebuild_visits_data --log-only

# Phase 2 only (logs lost, iCloud archive intact)
./rebuild_visits_data --rehydrate-only

# Operate on isolated paths (useful in tests / experiments)
./rebuild_visits_data \
    --log-dir /tmp/logs --db /tmp/test.db \
    --source /tmp/dl --dest /tmp/icloud
```

Flags:

| Flag | Effect |
|------|--------|
| `--truncate` (default) / `--no-truncate` | DROP and recreate the four rebuildable tables before phase 1.  `mover_errors` is left alone in either case. |
| `--log-only` / `--rehydrate-only` | Skip phase 2 / phase 1 (mutually exclusive). |
| `--log-dir DIR` | Override `BVL_LOG_DIR` (the per-day logs root) |
| `--db FILE` | Override `BVL_DB_FILE` |
| `--source DIR` | Override `BVL_DOWNLOADS_SNAPSHOTS_DIR` |
| `--dest DIR` | Override `BVL_ICLOUD_SNAPSHOTS_DIR` |
| `-v`, `--verbose` | DEBUG log level |

`mover_errors` is intentionally not log-recoverable; the rebuild
leaves it alone.  Filesystem-derived rows (`orphan_file`,
`invalid_filename`, `missing_directory`, `manifest_invalid`)
repopulate naturally on the next mover/sealer/verifier pass.

Exit codes: 0 on success, 1 on input errors (missing log/DB path,
orphan or malformed lines skipped during phase 1), 2 on an
unexpected exception.  See [docs/rebuild-visits-from-log.md](docs/rebuild-visits-from-log.md)
for the full design.

---

## Multi-laptop sync mode

Optional layer that lets several laptops share their browsing state
through a central gRPC service ([`browser-visit-sync-server/`](../browser-visit-sync-server/))
running on an EC2 Linux VM.  Each laptop's local DB and local logs
continue to work exactly as in single-laptop mode; the sync layer
just makes the local DB converge to the union of every laptop's
activity within ~1 minute of any interaction.

**How to turn it on:**

1. Set up the sync server on EC2 (see
   [`browser-visit-sync-server/README.md`](../browser-visit-sync-server/README.md)).
2. Mint a client cert for this laptop, signed by the same CA the
   server trusts.  Drop `client.crt`, `client.key`, and
   `server-ca.crt` into `~/.browser-visit-logger/tls/` (mode 0600).
3. Run [`browser-visit-tools/install_laptop.sh`](../browser-visit-tools/install_laptop.sh).
   It removes the legacy verifier LaunchAgent (verification lives on
   the VM now), re-runs the upstream `install.sh`, and writes
   `~/.browser-visit-logger/config.json` with this laptop's
   `machine_id` (derived from `scutil --get LocalHostName`) and the
   sync-server endpoint.
4. On the VM, enroll the laptop:
   ```
   python3 enroll_machine.py --db enrolled_machines.db \
     --machine-id <id-printed-by-install_laptop.sh> --cert client.crt
   ```
5. (Optional) migrate the historical iCloud snapshot archive into
   Google Drive with
   [`browser-visit-tools/migrate_icloud_to_gdrive.py`](../browser-visit-tools/migrate_icloud_to_gdrive.py).
6. Trigger any Chrome → extension interaction.  The Swift host
   responds to Chrome, then fork-detaches `sync_client.py`; the
   first run pushes any backlog and pulls peer history.

**How sync runs:**

`BVLHost` (the Chrome native-messaging host) checks for
`~/.browser-visit-logger/config.json` right after writing its
response to Chrome.  If present, it `/bin/sh -c 'nohup <python>
sync_client.py … &'` — fork+detach so Chrome isn't blocked on the
network round-trip.  Two resolution details make this work without a
source checkout on `PATH`:

- **Script path** — `install.sh` bundles `sync_client.py` (and the
  generated gRPC stubs) into the host `.app` under
  `Contents/Resources/native-host/`, so `BVLHost` finds it via the
  bundle; a dev fallback to the repo checkout is tried only if the
  bundle copy is absent.
- **Interpreter** (`resolveSyncPython`) — `BVL_PYTHON` if set, else the
  grpcio venv at `~/.browser-visit-logger/sync-venv/bin/python` that
  `install_laptop.sh` provisions, else bare `python3`.  The fallback
  rarely has `grpcio`, so the venv is what actually lets sync connect.

`sync_client.py` itself:

1. Acquires `~/.browser-visit-logger/sync.lock` (flock) — drops out
   immediately if another sync is already running.
2. Reads `sync_state.last_sync_attempt_at` for this machine_id;
   skips if within `BVL_SYNC_INTERVAL_SECONDS` (default 60).
3. Bumps `last_sync_attempt_at`.
4. Walks local log files past the self cursor and `PushLogs` them
   to the server in batches of 500 lines.  Advances the self cursor
   on each ack.
5. Sends every `sync_state` row to the server as a `PeerCursor`;
   server streams every peer's lines past those cursors.  For each
   returned chunk: append to `~/browser-visits-peers/<peer>/…log`
   and replay into the local DB via the same code path BVLHost uses
   for local events.  Bump each peer's cursor.
6. Records failures into `mover_errors` keyed by `(sync_push |
   sync_pull | sync_handshake, <endpoint>)` so the existing
   threshold-based escalation (Notification Center via the verifier
   pattern) surfaces persistent breakage to the user.

**What the laptop side no longer does in multi-laptop mode:**

- The `snapshot_verifier` LaunchAgent is not installed (and
  `install_laptop.sh` removes any leftover from a single-laptop
  install).  Sealing, manifest verification, and cross-day
  reconciliation all run on the VM now.
- The synchronous archive in `BVLHost`'s tag path still moves
  files from `~/Downloads/browser-visit-snapshots/` to the archive
  dir — but the archive dir defaults to a Google Drive mount path
  instead of iCloud Documents.  Drive sync ferries the files to
  the VM.

**Falling back to single-laptop mode:**

Delete `~/.browser-visit-logger/config.json`.  `BVLHost` will skip
the sync spawn immediately on its next invocation.  The local DB +
local logs + local snapshot archive are unchanged and remain
authoritative.

---

## When something goes wrong

Both `host.py` (synchronous archive at tag time) and the verifier
(periodic sweep / seal / verify) catch `(OSError, sqlite3.Error)` from
each per-file move, per-directory seal, and per-directory
straggler-rewrite.  Transient failures (one bad tick) clear themselves
on the next successful attempt; the user never sees them.

For everything else, the failing pass writes a row into the
`mover_errors` table keyed by `<operation>:<target>` and the verifier's
escalation pass surfaces it as a macOS notification once the row
qualifies:

- **Persistent failure**: the same `(operation, target)` has failed
  `BVL_MOVER_ERROR_THRESHOLD` times in a row (default 3).  One
  notification per streak.  Used for ops whose natural retry loop
  re-attempts the same target on subsequent ticks:
  - `move` — per-file archive failure (copy/chmod/UPDATE/INSERT/unlink).
    Triggered by either `BVLHost`'s synchronous archive at tag time or
    the verifier's sweep pass.
  - `seal` — `_seal_directory` failure (manifest write/chmod/DB update).
  - `missing_directory` — the `snapshots` table has a row for a date
    whose on-disk directory has been deleted.  The row auto-clears
    if the user re-creates the directory.
- **Immediate failure**: notified on the first occurrence regardless
  of attempts.  Used for ops whose retry loop won't re-encounter the
  same target on subsequent ticks (so threshold-based escalation
  would never fire), and for catastrophic conditions:
  - `top_level` — uncaught exception in `BVLHost` or `BVLVerifier`.
  - `rewrite_manifest` — straggler-rewrite failure (only triggered by
    a fresh straggler arriving in a sealed dir).
  - `manifest_invalid` — the verifier's verify pass found a sealed
    directory whose `MANIFEST.tsv` failed one or more checks.
    Auto-clears on the next successful verification.
  - `invalid_filename` — a file in `~/Downloads/browser-visit-snapshots/`
    or in a daily snapshot directory has a name that doesn't match the
    canonical `<YYYY-MM-DDTHH-MM-SSZ>-<hash>.<ext>` format.  The
    Downloads file is left in place; date-dir files are excluded from
    the manifest.  The row auto-clears when the user removes or
    renames the file.
  - `orphan_file` — a conforming-named snapshot file in a daily
    directory has no matching `read_events` / `skimmed_events` row.
    The file is excluded from the manifest.  The row auto-clears when
    the user removes the file or re-tags its URL via the popup so an
    events row is recorded.
  - `OSError` with errno in `{ENOSPC, EROFS, EDQUOT}` — disk full,
    read-only filesystem, or quota exceeded.
  - `sqlite3.DatabaseError` other than `OperationalError` — typically
    means the DB file is corrupt.

Both notification banner bodies and `--show-errors` rows include a
per-op `Fix:` hint pointing at the user action that resolves the
problem (e.g. "Rename the file to match …", "Run `snapshot_sealer.py
<date>` to rebuild the manifest …").

A row stays in the table until the underlying problem is resolved.
Three paths clear it:

1. **Automatic** — the next successful run of the same operation
   `_clear_error`s the row.  For `invalid_filename` and
   `missing_directory`, a directory-scoped reconcile additionally
   clears rows whose target file/dir no longer exists.  Most users
   never need to touch the table.
2. **Manual, all rows** — `./verify_snapshot_directory --clear-errors`.
   Use after acknowledging a batch of stale rows.
3. **Manual, one row** — `./verify_snapshot_directory --clear-error N`,
   where `N` is the index from `--show-errors`.

To inspect: `./verify_snapshot_directory --show-errors`.  Prints a
numbered list of currently pending errors with `attempts`, `first_seen`,
`last_seen`, message, and whether the user has been notified.

If macOS Notification Center can't be reached (`osascript` missing,
non-Darwin platform), the verifier falls back to creating
`~/browser-visits-mover-needs-attention` so the user can spot it via
shell or Finder.

### TCC denials

If `BVLHost` or `BVLVerifier` hit `[Errno 1] Operation not permitted`
on `~/Downloads/browser-visit-snapshots/` or
`~/Documents/browser-visit-logger/`, macOS' Privacy & Security
framework hasn't granted the relevant `.app` bundle access yet.  Two
common triggers:

- **Fresh install** — the user dismissed (or never saw) the initial
  prompt.  Either tag a page (re-triggers the host's prompt) and
  `launchctl kickstart -k gui/$(id -u)/com.browser.visit.logger.snapshot_verifier`
  (re-triggers the verifier's), or grant manually.
- **`install.sh` was re-run** — codesigning changed the bundle's
  cdhash, invalidating any prior grant.  See the
  [Re-running install.sh](#re-running-installsh) section above.

Manual grant: **System Settings → Privacy & Security → Full Disk
Access** (or **Files and Folders** under "Downloads Folder" /
"Documents Folder"), drag in:

```
~/Library/Application Support/browser-visit-logger/BrowserVisitLoggerHost.app
~/Library/Application Support/browser-visit-logger/BrowserVisitLoggerVerifier.app
```

Then `./verify_snapshot_directory --show-errors` should report `No
pending mover errors.` after the next host invocation or verifier
tick.

---

## Configuration

Every script honours these environment variables. CLI flags override
env vars; env vars override defaults.

| Variable | Default | Used by |
|----------|---------|---------|
| `BVL_LOG_DIR` | `~` | host, verifier (orphan-merge + log move during seal), sealer, rebuilder, reset.  The directory holding per-day `browser-visits-<UTC-date>.log` files. |
| `BVL_HOST_LOG` | `~/browser-visits-host.log` | host, reset |
| `BVL_VERIFIER_LOG` | `~/browser-visits-verifier.log` | reset (verifier writes via LaunchAgent stdout/stderr) |
| `BVL_DB_FILE` | `~/browser-visits.db` | host, verifier, sealer, rebuilder, reset |
| `BVL_DOWNLOADS_SNAPSHOTS_DIR` | `~/Downloads/browser-visit-snapshots` | host (synchronous archive source), verifier (sweep source), reset |
| `BVL_GDRIVE_SNAPSHOTS_DIR` | `~/Library/CloudStorage/GoogleDrive-<account>/My Drive/browser-visit-logger/snapshots` | host (multi-laptop archive destination); preferred over the legacy name below. `<account>` comes from `BVL_GDRIVE_ACCOUNT` or `gdrive_account` in `~/.browser-visit-logger/config.json` |
| `BVL_GDRIVE_ACCOUNT` | (none) | host — the Google account that names the local `GoogleDrive-<account>` mount; recorded by `install_laptop.sh`. Only used to build the default above when `BVL_GDRIVE_SNAPSHOTS_DIR` is unset |
| `BVL_ICLOUD_SNAPSHOTS_DIR` | (same default as above, when `BVL_GDRIVE_SNAPSHOTS_DIR` unset) | host, verifier, sealer, rebuilder — alias retained for backwards compat |
| `BVL_MOVER_MIN_AGE_SECONDS` | `60` | verifier (sweep age threshold) |
| `BVL_MOVER_ERROR_THRESHOLD` | `3` | verifier + sync_client (consecutive failures before persistent-error notification) |
| `BVL_CONFIG_DIR` | `~/.browser-visit-logger` | host, sync_client (machine config + mTLS dir) |
| `BVL_PEER_LOGS_DIR` | `~/browser-visits-peers` | sync_client (local mirror of pulled peer logs) |
| `BVL_SYNC_SERVER` | (from `config.json`) | sync_client (override the configured endpoint) |
| `BVL_SYNC_INTERVAL_SECONDS` | `60` | sync_client (debounce — minimum gap between successive sync attempts) |
| `BVL_MACHINE_ID` | (from `config.json`) | host, sync_client (override the sanitised hostname; mainly for tests + recovery) |
| `BVL_SYNC_CLIENT_SCRIPT` | (bundled path, else dev fallback) | host (path to `sync_client.py`; override for tests) |
| `BVL_PYTHON` | `python3` | host (interpreter used to spawn `sync_client.py`) |

---

## Development

A `Makefile` wraps the test suites.  The Python target builds a
throwaway virtualenv under `.venv-test/` on first run (gitignored) and
installs `requirements-test.txt` into it, so the host's system Python
is never touched:

```bash
# Python tests (host, mover, sealer, sync_client) — venv auto-created,
# gated at 100% line coverage (--cov-fail-under=100)
make test-py

# JS tests (background, popup) with coverage
make test-js

# Both
make test

# Build the Swift release binaries (needed by the shell wrappers and
# the Swift-dependent paths); there is no XCTest target — see below
make swift
```

The full suite is **225 Python + 131 JS tests, 100% line/branch
coverage** on every shipped module (including `sync_client.py`).  Each
Makefile Python target enforces the 100% floor, so a coverage
regression fails the build.

**Test isolation / production-data safety.** Tests never read or write
real user data.  Every test routes its file/DB I/O to a temp dir
(`tmp_path` / `tempfile`), and `tests/conftest.py` additionally forces
all `BVL_*` paths to a throwaway sandbox *before* any module import —
so even a test that forgets to isolate falls back to the sandbox, never
`~`.  (`setdefault` is used, so an intentionally exported `BVL_*` value
still wins.)

> **macOS note:** this Python build redirects bytecode caching via
> `sys.pycache_prefix` to `~/Library/Caches/com.apple.python/`.  If you
> ever hand-edit a `native-host/*.py` and see a stale result, clear that
> cache dir — `__pycache__` won't be next to the source.

There is no Swift unit-test target (XCTest isn't available with the
standalone Command Line Tools).  The Swift code paths are covered
indirectly by the Python suite (same DB schema + on-disk artifacts) and
by `install.sh`'s end-to-end native-messaging smoke test.

### Project layout

```
browser-visit-logger/
├── extension/                              # Chrome MV3 extension
│   ├── background.js                       # Service worker
│   ├── manifest.json.template              # → manifest.json (built by install.sh)
│   ├── popup.html
│   └── popup.js
├── swift/
│   ├── Package.swift                       # SwiftPM manifest, no third-party deps
│   ├── Sources/
│   │   ├── BVLCore/                        # shared library (schema, DB, archive, verify, …)
│   │   ├── BVLHost/                        # Chrome native-messaging host (Mach-O)
│   │   └── BVLVerifier/                    # daily background agent (Mach-O)
│   └── .build/release/                     # built binaries (after `swift build -c release`)
├── native-host/
│   ├── host.py                             # DB helpers used by the rebuilder
│   ├── snapshot_sealer.py                  # Manual sealer CLI (Python)
│   ├── visits_rebuilder.py                 # DB rebuilder (log replay + FS rehydrate)
│   ├── sync_client.py                      # Multi-laptop sync subprocess (gRPC push/pull)
│   ├── com.browser.visit.logger.json       # Chrome native-messaging manifest template
│   └── com.browser.visit.logger.snapshot_verifier.plist.template
├── tests/                                  # Python (pytest) + JS (jest); conftest.py sandboxes BVL_* paths
├── install.sh                              # Builds Swift binaries, materializes & signs .app bundles
├── Makefile                                # test-py / test-js / test / swift / venv targets
├── reset.py
├── seal_snapshot_directory                 # Bash wrapper → snapshot_sealer.py
├── verify_snapshot_directory               # Bash wrapper → swift/.build/release/BVLVerifier
├── reset_visits_data                       # Bash wrapper → reset.py
├── rebuild_visits_data                     # Bash wrapper → visits_rebuilder.py
├── package.json                            # JS test deps + jest config
├── requirements-test.txt                   # Python test deps
└── .venv-test/                             # test virtualenv (gitignored, auto-created by `make test-py`)
```

On macOS, `install.sh` additionally materializes:

```
~/Library/Application Support/browser-visit-logger/
├── BrowserVisitLoggerHost.app/             # Wraps the BVLHost Mach-O (Chrome → host)
└── BrowserVisitLoggerVerifier.app/         # Wraps the BVLVerifier Mach-O (launchd → tick)
```

---

## License

Personal project. No license declared — all rights reserved by the author.
