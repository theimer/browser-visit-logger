# Browser Visit projects

Four related projects that share a SQLite database of browser visit
history, optionally synchronised across multiple laptops via an
EC2-hosted gRPC service.

The setup can run in either of two modes:

- **Single-laptop** — extension → local DB + per-day TSV logs +
  a snapshot archive under `~/Documents` (iCloud-synced).  No network.
  Original design.
- **Multi-laptop** — every laptop still writes locally as above, plus
  pushes its log records to (and pulls peer records from) a gRPC
  service running on a Linux VM.  The VM holds the canonical merged
  DB, runs the verifier/snapshotter, and stores the unified snapshot
  archive on Google Drive (accessible to laptops and the VM alike).
  Each laptop's local DB converges to the union of all laptops'
  activity within ~1 minute of any interaction.

**Setting it up from scratch?** Single-laptop mode is just
`browser-visit-logger/install.sh`.  For the full multi-laptop stack —
create the EC2 VM, run the sync-server on it, and enrol/install one or
more laptops — follow the end-to-end runbook in
[`docs/MULTI_LAPTOP_SETUP.md`](docs/MULTI_LAPTOP_SETUP.md).

**Converting an existing single-laptop install?** The two modes use
different snapshot stores (iCloud Documents vs. Google Drive), so a
laptop that already ran single-laptop mode has historical snapshots in
iCloud that won't be visible to the shared Google Drive archive.  Run
the one-time migration once per such laptop —
[`browser-visit-tools/migrate_icloud_to_gdrive.py`](browser-visit-tools/migrate_icloud_to_gdrive.py)
copies the archive to Google Drive (SHA-256 verified) and rewrites the
DB paths; it's idempotent and leaves the iCloud copy in place.

## [`browser-visit-logger/`](browser-visit-logger/)

The Chrome extension and macOS native-messaging host that records
every page you visit to `~/browser-visits.db` and to a per-day TSV
log file, lets you tag pages of interest from a popup, and archives
full-page snapshots (MHTML or PDF).  The address-bar icon turns
gray / orange / yellow / green based on the current tab's tag state
(untagged / of-interest / skimmed / read).

In multi-laptop mode, the native host (`BVLHost`) also fork-detaches
a Python `sync_client.py` after responding to Chrome — that's what
actually talks to the gRPC service.  Sync is debounced (default 60
seconds since last attempt) and gated on
`~/.browser-visit-logger/config.json` existing, so single-laptop
installs are entirely unaffected.

## [`browser-visit-sync-server/`](browser-visit-sync-server/)

The Go gRPC service that runs on the EC2 Linux VM.  Three RPCs:

- **`PushLogs`** — receives log lines from a laptop, mirrors them
  into `<log-root>/<machine_id>/browser-visits-YYYY-MM-DD.log`, and
  replays each line into the canonical DB.
- **`PullLogs`** — streams every peer machine's log lines past each
  per-peer cursor the caller supplies.  The caller's own records are
  never echoed back.
- **`ExportDbSnapshot`** — runs `VACUUM INTO` and streams a
  transactionally-consistent SQLite snapshot back to the caller.
  Feeds `browser-visit-tools/db_diff.py`.

Authentication is mTLS.  Each laptop has its own client cert.  The
server's allowlist is `enrolled_machines.db`, a SQLite file on the VM
that maps each laptop's `machine_id` to its client-cert SHA-256.  You
populate it with the admin tool `browser-visit-tools/enroll_machine.py`
(enroll / revoke / list); the server only reads it, at handshake time,
rejecting any cert whose fingerprint isn't enrolled.  Identity spoofing
— a laptop presenting a valid enrolled cert but claiming a different
`machine_id` — is caught by a `CrossCheck(ctx, claimedID)` call on every
RPC.

Includes a full Docker Compose integration suite under
[`browser-visit-sync-server/test/`](browser-visit-sync-server/test/);
see that directory's README for the venv + `make test-integration`
workflow.

The EC2 VM that hosts this service is created and managed by the
`manage_vm.py` / `manage_sync_server.py` tools (below): the binary is
cross-compiled (`make build-linux`), deployed as a systemd service, and
the VM is reached entirely over AWS SSM — no SSH, no inbound port 22,
only the gRPC port `50443` is open to enrolled laptops.

## [`browser-visit-tools/`](browser-visit-tools/)

Standalone scripts that consume the database or operate on the
multi-laptop setup itself:

- **`reading_list.py`** — generates a reading list of every URL
  tagged of_interest but not yet read.  HTML by default, Markdown
  via `--format markdown`.
- **`db_diff.py`** — compares two `browser-visits.db` files (e.g.
  laptop vs. VM snapshot) and reports per-table presence and value
  differences.  Counter columns can be ignored via
  `--ignore-counters`.
- **`fetch_vm_snapshot.py`** — gRPC client that calls
  `ExportDbSnapshot` and writes the bytes to a local file.  Composes
  with `db_diff.py`.
- **`enroll_machine.py`** — admin tool, run on the VM.  Inserts a
  `(machine_id, cert_sha256)` row into `enrolled_machines.db`.
- **`migrate_icloud_to_gdrive.py`** — one-time migration of the
  historical iCloud snapshot archive to a Google Drive folder, with
  SHA-256 verification and DB-path rewrites.  Idempotent.
- **`install_laptop.sh`** — orchestrates a laptop install for the
  multi-laptop setup (runs the legacy uninstaller, then upstream
  `install.sh`, then writes the machine config).
- **`uninstall_laptop_legacy.sh`** — removes the legacy
  snapshot-verifier LaunchAgent (the verifier moves to the VM in
  multi-laptop mode).  Idempotent; never touches user data.
- **`manage_vm.py`** — idempotently creates and operates the EC2 VM
  (instance + security group + IAM instance profile) via `boto3`.
- **`manage_sync_server.py`** — provisions, deploys, and controls the
  sync-server on the VM over AWS SSM (no SSH).

The read-only tools depend only on the DB schema; the sync-related
tools depend on the gRPC stubs the sync-server emits; the EC2 tools
depend on `boto3` + AWS credentials.

## [`browser-visit-mcp/`](browser-visit-mcp/)

A local [Model Context Protocol](https://modelcontextprotocol.io/)
server that exposes the visits database to MCP clients (Claude Code,
Claude Desktop, MCP Inspector) over stdio.  Provides two tools:
`query` (run an arbitrary read-only SQL statement and get
columns + rows back) and `schema` (return the DDL so the model can
write well-formed queries).  Read-only is enforced both by opening
SQLite with `mode=ro` and by validating each statement before
execution.

A `.mcp.json` at the repo root registers the server project-locally
so Claude Code picks it up automatically.

## End-to-end data flow (multi-laptop mode)

```
Chrome (laptop A)               Chrome (laptop B)
   │ navigation/tag                │ navigation/tag
   ▼                               ▼
BVLHost                          BVLHost
   │ append local log line          │
   │ INSERT/UPDATE local DB         │
   │ archive snapshot → Google Drive│
   │ fork sync_client.py            │
   ▼                               ▼
sync_client.py ──── gRPC ────► browser-visit-sync-server (EC2 VM)
   PushLogs (new local lines)         │
   PullLogs (cursors for every peer)  │
   ▲                                  │
   │  replay into local DB    ◄───────┘  apply lines to canonical DB
   │  mirror to ~/browser-visits-peers/<peer>/
   ▼
Chrome stays unaware; address-bar icon refresh
sees the updated counters on next query.

Snapshots ride independently via Google Drive's own sync (Drive
mounted on both laptops and the VM).  The VM-side verifier
periodically seals completed days and writes MANIFEST.tsv files.
```

Everything that runs on the laptop side keeps working in
single-laptop mode if `~/.browser-visit-logger/config.json` doesn't
exist; the sync hook in `BVLHost` short-circuits, and the local DB +
local logs + local snapshot archive remain authoritative.

## Testing & development

Each project owns its own test suite and a `Makefile` that wraps it.
The Python suites build a throwaway, gitignored virtualenv
(`.venv-test/` or `test/.venv/`) on first run, so your system Python is
never modified.

| Project | Command | What it needs | Coverage |
|---|---|---|---|
| `browser-visit-logger` (Python) | `make test-py` | python3 | 225 tests, 100% (gated) |
| `browser-visit-logger` (JS) | `make test-js` | node/npm | 131 tests, 100% |
| `browser-visit-tools` | `make test` | python3 | 168 tests, 100% (gated) |
| `browser-visit-sync-server` (Go unit) | `make cover` | go 1.22+, protoc | 94–100% per package¹ |
| `browser-visit-sync-server` (integration) | `make test-integration` | Docker | 7 end-to-end tests |
| `browser-visit-mcp` | `pytest` | python3 | see its README |

¹ Go residuals are `main()` (a process entrypoint) plus unreachable
defensive error branches; generated protobuf code is excluded by
convention.

**Key assumptions baked into the tests and tooling:**

- **Tests never touch production data.** Every suite routes I/O to a
  temp dir, and the Python `conftest.py` files additionally force all
  `BVL_*` paths to a throwaway sandbox before import as a safety net.
  `setdefault` is used, so an intentionally exported `BVL_*` value
  still wins.
- **`go` and `protoc` are only needed to build/test the sync-server
  outside Docker.** The Docker image (and the integration suite that
  builds it) install them internally; `grpcio` is faked in unit tests.
- **The macOS system Python redirects bytecode to
  `~/Library/Caches/com.apple.python/`** (`sys.pycache_prefix`), so a
  hand-edit that seems ignored may be a stale `.pyc` there, not beside
  the source.
- **Single-laptop mode is the default**; multi-laptop behavior only
  activates once `~/.browser-visit-logger/config.json` exists.
