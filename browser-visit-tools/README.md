# Browser Visit Tools

Command-line tools that complement the
[`browser-visit-logger/`](../browser-visit-logger/) project.  Split
into two groups:

- **Read-only DB consumers** — `reading_list.py`, `db_diff.py`.
  Depend only on the DB schema (`visits`, `read_events`,
  `skimmed_events`).  No Python imports cross the directory
  boundary, so they can be vendored or copied without the logger.
- **Multi-laptop sync operations** — `fetch_vm_snapshot.py`,
  `enroll_machine.py`, `migrate_icloud_to_gdrive.py`,
  `install_laptop.sh`, `uninstall_laptop_legacy.sh`.  Specific to
  the multi-laptop architecture (see the top-level
  [README](../README.md) and
  [`browser-visit-sync-server/`](../browser-visit-sync-server/)).
- **EC2 VM management** — `manage_vm.py`, `manage_sync_server.py`
  (with the internal `_bvl_aws.py` helper).  Create and operate the
  EC2 VM that hosts the sync-server, and remotely manage the server
  process over AWS SSM (no SSH).  Need `boto3` and AWS credentials.

## CLI scripts

All scripts live at the project root.  They share the same `BVL_*`
env-var conventions as `browser-visit-logger` (notably
`BVL_DB_FILE`) and accept overriding flags so they're safe to point
at test data.

Each shell wrapper delegates to a Python script.  The wrapper forwards
all arguments verbatim and intercepts `--help` / `-h` to print a
one-line wrapper note before delegating.

| Wrapper / script | Underlying tool | Purpose |
|---|---|---|
| `./generate_reading_list` | `reading_list.py` | of_interest-but-unread → HTML or Markdown |
| `db_diff.py` | (Python, no wrapper) | Diff two `browser-visits.db` files |
| `fetch_vm_snapshot.py` | (Python, no wrapper) | Download a consistent VM DB snapshot via gRPC |
| `gen-prod-certs` | `../browser-visit-sync-server/test/gen-certs.sh` | Mint production mTLS material into `~/.browser-visit-logger/ca/` |
| `enroll_machine.py` | (Python, no wrapper) | VM-side admin: enroll a laptop's mTLS cert |
| `migrate_icloud_to_gdrive.py` | (Python, no wrapper) | One-time iCloud → Google Drive snapshot migration |
| `install_laptop.sh` | (Bash) | Laptop install for multi-laptop mode |
| `uninstall_laptop_legacy.sh` | (Bash) | Remove legacy verifier LaunchAgent |
| `manage_vm.py` | (Python, no wrapper) | Create / start / stop / status / terminate the EC2 VM |
| `manage_sync_server.py` | (Python, no wrapper) | Provision / deploy / control / health-check the sync-server over SSM |
| `_bvl_aws.py` | (internal) | Shared boto3 / SSM / S3 helpers — not a CLI entrypoint |

### `generate_reading_list`

Generates a reading list of every URL tagged **★ Of Interest** that
has not yet been **✓ Read**.  Output format defaults to **HTML**
(a self-contained file with inline CSS, openable directly in a
browser); pass `--format markdown` for Markdown.  Default output:

| Format | Default path |
|--------|--------------|
| html (default) | `~/Documents/browser-visit-logger/reading_list.html` |
| markdown       | `~/Documents/browser-visit-logger/reading_list.md` |

The list is split into two clickable tables:

| Table | URLs included |
|-------|---------------|
| Unread URLs that have been skimmed | of_interest = set, read = 0, skimmed > 0 |
| Unread URLs                         | of_interest = set, read = 0, skimmed = 0 |

Both tables are sorted by **first-visited timestamp, most recent
first**.  Date-time values are rendered in the user's **local time
zone** (the database stores UTC; the tool converts at format time).

URLs render as clickable links — the visible label is the page title
(falling back to the URL itself when title is empty).  Special
characters are escaped per format: HTML uses `html.escape` for both
the link label and the `href` value; Markdown escapes `|`, `[`, `]`
and percent-encodes parens / spaces in URLs.  Tabs / newlines in
titles collapse to spaces so rows stay on one line.

```bash
# Default — HTML to ~/Documents/browser-visit-logger/reading_list.html
./generate_reading_list

# Markdown instead (default path becomes reading_list.md)
./generate_reading_list --format markdown

# Override paths (useful for tests / experiments)
./generate_reading_list --db /tmp/test.db --output /tmp/reading_list.html

# Skip the wrapper (equivalent)
python3 reading_list.py
```

Flags:

| Flag | Effect |
|------|--------|
| `--format {html,markdown}` | Output format (default `html`) |
| `--db FILE` | Override `BVL_DB_FILE` (default `~/browser-visits.db`) |
| `--output FILE` | Override the default output path; takes precedence over the format-derived default |
| `-v`, `--verbose` | DEBUG log level |

The default output directory is `~/Documents/browser-visit-logger/`,
overridable via the `BVL_OUTPUT_DIR` env var (an explicit `--output`
still wins over both).

The output file is overwritten on every run.  Parent directory is
created if missing.  Exit codes: `0` on success, `1` if the database
file is missing.

### `db_diff.py`

Compares two `browser-visits.db` SQLite files.  Reports presence
differences (rows in A not in B, and vice versa) per table, plus
value differences for rows present in both.  Counter columns
(`visits.read`, `visits.skimmed`) legitimately diverge between sync
windows; suppress them with `--ignore-counters`.

Exit codes: `0` no differences, `1` differences found, `2` tool
error.  Output is text by default; pass `--format json` to pipe into
other tooling.

```bash
# Diff a laptop's local DB against a freshly-pulled VM snapshot
python3 fetch_vm_snapshot.py --server vm:50443 --machine-id $(hostname) \
    --client-cert ~/.browser-visit-logger/tls/client.crt \
    --client-key  ~/.browser-visit-logger/tls/client.key \
    --ca          ~/.browser-visit-logger/tls/server-ca.crt \
    --out /tmp/vm.db
python3 db_diff.py --db-a ~/browser-visits.db --db-b /tmp/vm.db --ignore-counters

# Diff two arbitrary snapshots
python3 db_diff.py --db-a a.db --db-b b.db --sample 10 --format json
```

Default tables: `visits, read_events, skimmed_events, snapshots`
(intrinsically per-machine tables — `sync_state`, `mover_errors` —
are excluded).  Override with `--tables a,b,c`.

### `fetch_vm_snapshot.py`

gRPC client for the sync-server's `ExportDbSnapshot` RPC.  Streams a
`VACUUM INTO`-produced copy of the canonical DB to a local file.
Pairs with `db_diff.py`.

Requires the generated Python gRPC stubs from
`browser-visit-sync-server/gen/syncpb_py/`; the script tells you
exactly which `make` target to run if they're missing.

```bash
python3 fetch_vm_snapshot.py \
    --server bvl-vm.example.com:50443 \
    --machine-id $(scutil --get LocalHostName) \
    --client-cert ~/.browser-visit-logger/tls/client.crt \
    --client-key  ~/.browser-visit-logger/tls/client.key \
    --ca          ~/.browser-visit-logger/tls/server-ca.crt \
    --out /tmp/vm-snapshot.db

# Local dev (no TLS)
python3 fetch_vm_snapshot.py --server localhost:50051 \
    --machine-id laptop-a --insecure --out /tmp/vm.db \
    --client-cert /dev/null --client-key /dev/null --ca /dev/null
```

### `gen-prod-certs`

Thin wrapper over `browser-visit-sync-server/test/gen-certs.sh` that pins
`--out-dir` to `~/.browser-visit-logger/ca/` (override with `BVL_CA_DIR`),
so the production CA never lands in the throwaway `test/certs/` that
`make test-integration` wipes.  All other flags pass straight through.

```bash
# First run: mint CA + server cert + one client cert per laptop
./gen-prod-certs --server-host bvl-vm.example.com laptop-a laptop-b

# Later: add one more laptop against the existing CA (non-destructive)
./gen-prod-certs --add-client my-new-mbp
```

Keep the resulting `ca.key` safe — it's the root of trust and what you
need to enrol more laptops later.  See the
[setup runbook](../docs/MULTI_LAPTOP_SETUP.md#step-2--mint-tls-material).

### `enroll_machine.py`

Admin tool that runs on the **EC2 VM** (or wherever you have the
`enrolled_machines.db` SQLite file the sync-server reads).  Inserts
a `(machine_id, cert_sha256, enrolled_at)` row so the server's
mTLS interceptor accepts the laptop's client cert.

```bash
# Enroll
python3 enroll_machine.py \
    --db /var/lib/browser-visit-sync/enrolled_machines.db \
    --machine-id marvins-mbp \
    --cert /etc/browser-visit-sync/clients/marvins-mbp.crt

# List
python3 enroll_machine.py --db enrolled_machines.db --list

# Revoke
python3 enroll_machine.py --db enrolled_machines.db \
    --machine-id marvins-mbp --revoke
```

Reads PEM (preferred — needs `cryptography`) or DER directly.

### `migrate_icloud_to_gdrive.py`

One-time migration for users moving from single-laptop to
multi-laptop mode.  Copies every file under the iCloud snapshots
root to the Google Drive root (preserving the `YYYY-MM-DD/` layout),
verifies each file via SHA-256, and rewrites the `directory` column
on every affected `read_events` / `skimmed_events` row.  Idempotent
— re-runs skip files already present with matching hashes.  Always
leaves the iCloud copy in place so you can verify before deleting
it.

```bash
# Dry run first to see what would happen
python3 migrate_icloud_to_gdrive.py --dry-run \
    --src ~/Documents/browser-visit-logger/snapshots \
    --dst "~/Library/CloudStorage/GoogleDrive/My Drive/browser-visit-logger/snapshots" \
    --db  ~/browser-visits.db

# Run for real
python3 migrate_icloud_to_gdrive.py \
    --src ~/Documents/browser-visit-logger/snapshots \
    --dst "~/Library/CloudStorage/GoogleDrive/My Drive/browser-visit-logger/snapshots" \
    --db  ~/browser-visits.db
```

### `install_laptop.sh`

Orchestrates a multi-laptop-mode install on a Mac.  Three steps:

1. Runs `uninstall_laptop_legacy.sh` (idempotent).
2. Delegates to `browser-visit-logger/install.sh` to build /
   re-sign the BVLHost app bundle and install the Chrome
   native-messaging manifest.
3. Writes `~/.browser-visit-logger/config.json` with this laptop's
   `machine_id` (sanitised `scutil --get LocalHostName`) and an
   optional `sync_server` endpoint.  Ensures `~/.browser-visit-logger/tls/`
   exists with mode 0700 ready for the user to drop in
   `client.crt` / `client.key` / `server-ca.crt`.

```bash
bash install_laptop.sh                              # uses existing config if any
bash install_laptop.sh --server bvl-vm.example.com:50443
```

Prints the resolved `machine_id` at the end — pass it to
`enroll_machine.py` on the VM.

### `uninstall_laptop_legacy.sh`

Removes the legacy `com.browser.visit.logger.snapshot_verifier`
LaunchAgent and its `.app` bundle.  In multi-laptop mode, sealing /
manifest verification / cross-day reconciliation all live on the VM
now.  Idempotent and conservative: never touches user data
(`~/browser-visits-*.log`, `~/browser-visits.db`, the iCloud
archive, `~/.browser-visit-logger/`).  Also sweeps up the
even-older `snapshot_mover` LaunchAgent if any install older than
the verifier consolidation is still lingering.

```bash
bash uninstall_laptop_legacy.sh
```

`install_laptop.sh` runs this script as its first step, so most
users never invoke it directly.

### `manage_vm.py`

Creates and manages the EC2 VM that hosts the sync-server.  Talks to
AWS via `boto3`; credentials come from the ambient chain (env / shared
config / SSO).  Idempotency keys on the tags
`bvl:role=sync-server` / `bvl:managed-by=browser-visit-tools` — `create`
looks for an existing tagged, non-terminated instance before launching
a new one, so re-running is safe.  It also find-or-creates the
security group (gRPC port `50443` ingress only — **no SSH**) and an IAM
instance profile carrying `AmazonSSMManagedInstanceCore` so the VM is
reachable over SSM.

State (region, instance id, arch, …) is persisted to
`~/.browser-visit-logger/vm.json`; every subcommand loads it and falls
back to tag-discovery if it's missing or stale.

| Subcommand | Effect |
|---|---|
| `create` | Idempotently create the VM + SG + IAM profile |
| `start` / `stop` / `reboot` | Lifecycle control |
| `status` | Print state, type, public address |
| `terminate` | Terminate the instance (`--purge-infra` also deletes the shared SG + role) |

```bash
# Create (the only required flag is the CIDR allowed to reach the gRPC port)
python3 manage_vm.py create --allow-cidr 203.0.113.7/32

# Pick arch / size / region (arch must match the deployed binary)
python3 manage_vm.py create --allow-cidr 203.0.113.7/32 \
    --region us-west-2 --arch arm64 --instance-type t4g.small --volume-size 30

python3 manage_vm.py status
python3 manage_vm.py stop
python3 manage_vm.py terminate --purge-infra
```

`create` flags: `--allow-cidr CIDR` (required, repeatable), `--region`,
`--instance-type`, `--volume-size` (GiB, default 20),
`--arch {x86_64,arm64}` (default `arm64`/Graviton — cheaper, and the
pure-Go server has no x86 dependency; the instance type follows the arch:
`t4g.small` for arm64, `t3.small` for x86_64), `--ami` (override the
resolved Amazon Linux 2023 AMI).  Exit codes: 0 ok, 1 AWS/operation
failure, 2 usage.

### `manage_sync_server.py`

Provisions, deploys, and operates the sync-server **process** on the
VM, entirely over AWS SSM `SendCommand` (no SSH, no open port 22).
Resolves the instance the same way `manage_vm.py` does, waits for the
SSM agent to report `Online`, then runs the relevant shell over
`AWS-RunShellScript`.

The cross-compiled binary and the systemd unit / TLS material are
staged through a private, per-account S3 bucket
(`bvl-sync-deploy-<account>-<region>`, 7-day expiry lifecycle; TLS
objects are written with SSE) which the VM pulls from via its instance
profile — SSM's ~100 KB command-parameter cap can't carry the ~20 MB
binary inline.

Build the Linux binary first:

```bash
make -C ../browser-visit-sync-server build-linux GOARCH=amd64   # or arm64
```

| Subcommand | Effect |
|---|---|
| `provision` | Create the `bvlsync` user + data dirs, install the systemd unit and TLS material, `systemctl enable` (idempotent) |
| `deploy` | Stage the binary → S3 → `/usr/local/bin/sync-server`, restart |
| `start` / `stop` / `restart` | `systemctl` control |
| `status` | `systemctl is-active` + `status` |
| `logs` | `journalctl` **snapshot** (`--lines`, `--since`); for a live tail use `aws ssm start-session` |
| `health` | service active + gRPC port answers + disk under 90 % |

```bash
# First-boot setup (TLS material passed as local PEM paths)
python3 manage_sync_server.py provision \
    --server-cert server.crt --server-key server.key \
    --clients-ca clients-ca.crt

# Install the binary and start
python3 manage_sync_server.py deploy \
    --binary ../browser-visit-sync-server/bin/sync-server-linux-amd64
python3 manage_sync_server.py start
python3 manage_sync_server.py health
python3 manage_sync_server.py logs --lines 50
```

`provision --and-deploy --binary <path>` chains a deploy after
provisioning.  `deploy` refuses a binary whose arch token doesn't match
the VM's recorded arch unless you pass `--force`.  Exit codes: 0 ok,
1 remote/AWS failure, 2 usage.

#### Prerequisites

`boto3` (`pip install boto3`) and AWS credentials with, at minimum:

- EC2: `ec2:RunInstances`, `*Instances`, security-group create/authorize/delete,
  `ec2:DescribeImages`/`DescribeInstances`
- IAM: `iam:CreateRole`, `AttachRolePolicy`, `PutRolePolicy`,
  `CreateInstanceProfile`, `AddRoleToInstanceProfile`, and **`iam:PassRole`**
  (RunInstances fails confusingly without it)
- SSM: `ssm:SendCommand`, `GetCommandInvocation`,
  `DescribeInstanceInformation`, `GetParameter`
- S3: full access to the `bvl-sync-deploy-*` bucket
- STS: `sts:GetCallerIdentity`

These tools read the **ambient** boto3 credential chain (no `--profile` flag),
so with AWS SSO you must `aws sso login` *and* `export AWS_PROFILE=<profile>`
in the same shell. Note that `PowerUserAccess` lacks the IAM permissions above,
so `manage_vm.py create` needs an admin-capable role. The end-to-end
[multi-laptop setup runbook](../docs/MULTI_LAPTOP_SETUP.md#troubleshooting) has
a Troubleshooting section covering the SSO sign-in, credential, IAM-denial, and
`RunInstances` errors seen in practice, each with its fix.

## Development

A `Makefile` builds a throwaway virtualenv under `.venv-test/`
(gitignored, auto-created on first run) and runs the suite gated at
100% line coverage — the host's system Python is never touched:

```bash
make test          # venv + pytest, --cov-fail-under=100 across all modules
make clean-venv    # rebuild the venv after editing requirements-test.txt
```

**100% line coverage** across every shipped module: `reading_list.py`,
`db_diff.py`, `fetch_vm_snapshot.py`, `enroll_machine.py`,
`migrate_icloud_to_gdrive.py`, `_bvl_aws.py`, `manage_vm.py`,
`manage_sync_server.py`.  These standalone unit tests are in addition
to the end-to-end exercise the sync tools get from the integration
suite under
[`browser-visit-sync-server/test/`](../browser-visit-sync-server/test/).

Test deps: `pytest`, `pytest-cov`, and `cryptography` (the last only
because `enroll_machine.py`'s PEM-cert path uses it and the tests
exercise that path).  `fetch_vm_snapshot.py`'s gRPC dependency and the
`boto3` used by the EC2-management tools are both *faked* in tests
(every AWS call routes through the single `_bvl_aws.client` seam and
hand-rolled recording fakes), so neither `grpcio` nor `boto3` is
required to run the suite — and neither is in `requirements-test.txt`.

**Production-data safety.** `reading_list.py` is the only tool with
`~`-rooted defaults (`BVL_DB_FILE` to read, `BVL_OUTPUT_DIR` to write);
`tests/conftest.py` forces both to a throwaway sandbox before import so
a mis-isolated test can never touch real data.  The other tools take
all paths as required CLI args, so they have no default to guard.

## Project layout

```
browser-visit-tools/
├── reading_list.py                 # generate the reading list (HTML / Markdown)
├── generate_reading_list           # bash wrapper → reading_list.py
├── db_diff.py                      # diff two SQLite browser-visits DBs
├── fetch_vm_snapshot.py            # gRPC client → ExportDbSnapshot
├── gen-prod-certs                  # wrap gen-certs.sh → ~/.browser-visit-logger/ca/
├── enroll_machine.py               # VM-side admin tool
├── migrate_icloud_to_gdrive.py     # one-time archive migration
├── install_laptop.sh               # multi-laptop laptop install
├── uninstall_laptop_legacy.sh      # remove legacy verifier LaunchAgent
├── manage_vm.py                    # EC2 VM lifecycle (boto3)
├── manage_sync_server.py           # sync-server ops over SSM
├── _bvl_aws.py                     # shared boto3 / SSM / S3 helpers
├── Makefile                        # venv + test (gated at 100% coverage)
├── tests/
│   ├── conftest.py                 # sandboxes BVL_DB_FILE / BVL_OUTPUT_DIR / BVL_VM_STATE_FILE
│   ├── test_reading_list.py
│   ├── test_db_diff.py
│   ├── test_enroll_machine.py
│   ├── test_fetch_vm_snapshot.py
│   ├── test_migrate.py
│   ├── test_bvl_aws.py
│   ├── test_manage_vm.py
│   └── test_manage_sync_server.py
├── requirements-test.txt
├── .venv-test/                     # test virtualenv (gitignored, auto-created)
└── README.md
```
