# browser-visit-sync-server

Go gRPC service that runs on the EC2 Linux VM and acts as the
canonical store for browser-visit log records produced by every
enrolled laptop.  Pairs with `sync_client.py` on the laptop side
(see [`browser-visit-logger/README.md`](../browser-visit-logger/README.md#multi-laptop-sync-mode)).

## RPCs

| RPC | Direction | Purpose |
|---|---|---|
| `PushLogs` | laptop → server | Mirror new log lines; replay each into the canonical DB.  Idempotent on `(machine_id, date, line_offset)`. |
| `PullLogs` | server → laptop (stream) | Stream every peer's log lines past the per-peer cursors the caller supplies.  Caller's own records are never echoed back. |
| `ExportDbSnapshot` | server → laptop (stream) | Stream a `VACUUM INTO`-produced SQLite snapshot of the canonical DB.  Used by `browser-visit-tools/db_diff.py`. |

Authentication is mTLS.  Two interceptors run per call:

1. **logmw** (outer) — logs one line per RPC with the cert-derived
   caller, method, status, duration, and per-method details
   (e.g. `lines=N` + `accepted_to=...` for `PushLogs`).  Outer so
   it logs even when auth rejects.
2. **auth** (inner) — extracts the peer's verified leaf cert,
   SHA-256s it, looks it up in `enrolled_machines.db`, and binds
   the resulting `machine_id` to the context.  Handlers then call
   `auth.CrossCheck(ctx, req.GetMachineId())` to reject identity
   spoofs (laptop A's cert claiming `machine_id="laptop-b"`).

## Layout

```
proto/sync.proto              service definition (single source of truth)
cmd/sync-server/main.go       entrypoint — flag parsing, TLS, server start
cmd/sync-server/main_test.go  buildTLS unit tests
internal/server/              gRPC handler methods + *_test.go
internal/store/               per-machine log mirror + canonical DB + *_test.go
internal/auth/                mTLS cert → machine_id binding + *_test.go
internal/logmw/               per-RPC logging interceptor + *_test.go
Makefile                      proto / build / test / cover / test-integration
deploy/Dockerfile             multi-stage container build (protoc + go build)
deploy/sync-server.service    systemd unit
docker-compose.test.yml       brings up the server with test certs
test/                         integration suite (see test/README.md)
gen/syncpb/                   generated protobuf bindings (gitignored;
                              produced by `make proto` or the Dockerfile)
go.sum                        gitignored; produced by `go mod tidy`
```

For testability the package boundaries expose two small helpers used
only by tests: `auth.ContextWithMachineID` (construct a context that
looks like a successful mTLS pass) and `store.LogRoot()` (locate the
on-disk mirror).

## Storage layout on disk

```
<log-root>/
  <machine_id>/
    browser-visits-YYYY-MM-DD.log     # exact byte mirror of the laptop's log
<db>                                   # canonical SQLite (visits, *_events, snapshots)
<enrolled>                             # SQLite (machine_id, cert_sha256, enrolled_at)
```

Log files are append-only and never rewritten in place.  Idempotency
of `PushLogs` is enforced by the caller-supplied
`(machine_id, date, line_offset)` tuple — out-of-order or replayed
pushes are deduplicated against the file's current line count.  A
gap (offset beyond current count) is an error and the server returns
`Internal: gap: ...`.

## Generating protobuf bindings (host build)

The Dockerfile does this for you automatically.  If you want to
build the server outside Docker, install the toolchain once:

```
brew install protobuf go
go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.1
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.4.0
make proto       # produces gen/syncpb/sync.pb.go + sync_grpc.pb.go
go mod tidy      # produces go.sum (also gitignored)
make build       # → bin/sync-server
```

To produce the stripped Linux binary the EC2 deploy tooling installs,
cross-compile with `build-linux` (the SQLite driver is pure Go, so
`CGO_ENABLED=0` works).  `GOARCH` must match the instance arch:

```
make build-linux GOARCH=amd64    # → bin/sync-server-linux-amd64  (t3/x86_64)
make build-linux GOARCH=arm64    # → bin/sync-server-linux-arm64  (t4g/arm64)
```

Built and tested with Go 1.22+ (the module's `go` directive) — a
current toolchain (e.g. Go 1.26) works fine.

## Unit tests

Pure-Go tests, no Docker needed.  The SQLite driver is `modernc.org/sqlite`
(pure Go, no cgo), so these run identically on macOS and Linux:

```
make proto && go mod tidy   # once, if gen/ + go.sum aren't present
make cover                  # per-package coverage, excludes generated gen/syncpb
# or: make test             # plain go test ./...
```

Coverage of the hand-written packages: `logmw` 100%, `auth` 98%,
`server` 96%, `store` 94%, `cmd` (only `buildTLS`; `main()` is an
untested process entrypoint).  The residual gaps are unreachable
defensive error returns (`sql.Open`/`tx.Commit` failures, mid-stream
read errors) and `main()` — see the repo's testing notes.  `gen/syncpb`
(generated) is excluded by convention.

## Run locally (no TLS, for development)

```
go run ./cmd/sync-server \
  --listen :50051 \
  --log-root /tmp/vm-logs \
  --db /tmp/vm-browser-visits.db \
  --enrolled /tmp/enrolled.db \
  --insecure
```

In `--insecure` mode the auth interceptor is **not** installed, so
handlers' `CrossCheck(ctx, machineID)` calls will fail.  Only useful
for inspecting wire shapes; not a path you'd actually drive client
RPCs through.

## Run on EC2 (mTLS, enrolled machines only)

```
sync-server \
  --listen :50443 \
  --log-root /var/lib/browser-visit-sync/logs \
  --db /var/lib/browser-visit-sync/browser-visits.db \
  --enrolled /var/lib/browser-visit-sync/enrolled_machines.db \
  --tls-cert /etc/browser-visit-sync/server.crt \
  --tls-key  /etc/browser-visit-sync/server.key \
  --tls-client-ca /etc/browser-visit-sync/clients-ca.crt
```

Use the bundled systemd unit
[`deploy/sync-server.service`](deploy/sync-server.service) — it
pins the user to `bvlsync`, restarts on failure, and confines
filesystem writes to `/var/lib/browser-visit-sync/`.  The Dockerfile
at [`deploy/Dockerfile`](deploy/Dockerfile) is a multi-stage build
that produces a distroless `nonroot` image (~20 MB).

To create and operate the VM itself without doing any of this by hand,
use [`browser-visit-tools/manage_vm.py`](../browser-visit-tools/README.md#manage_vmpy)
(idempotent EC2 create) and
[`manage_sync_server.py`](../browser-visit-tools/README.md#manage_sync_serverpy)
— the latter provisions the `bvlsync` user + dirs, installs this unit
and the TLS material, cross-deploys the `build-linux` binary, and
controls the service, all over AWS SSM (no SSH).

TLS floor is **TLS 1.2** — pragmatic interop with macOS-system
Python 3.9 (LibreSSL 2.8.3) that doesn't speak TLS 1.3.  mTLS is the
actual auth boundary; the TLS version is just transport.  Bump to
1.3 in [`cmd/sync-server/main.go`](cmd/sync-server/main.go) once
every client speaks it.

## Per-RPC logging

The logmw interceptor writes one line per call to stderr (which
`docker compose logs` and `journalctl -u sync-server` both capture).
Format:

```
[caller=<machine_id|?>] <method> status=<code> dur=<duration> <details>
```

Examples from a real test run:

```
[caller=laptop-a] /bvl.sync.v1.BrowserVisitSync/PushLogs status=OK dur=2.151ms lines=4 dates=2026-05-21 accepted_to=2026-05-21:3
[caller=laptop-b] /bvl.sync.v1.BrowserVisitSync/PullLogs status=OK dur=198µs (stream)
[caller=?] /bvl.sync.v1.BrowserVisitSync/PushLogs status=PermissionDenied dur=124µs lines=1 dates=2026-05-21
[caller=laptop-a] /bvl.sync.v1.BrowserVisitSync/PushLogs status=PermissionDenied dur=139µs lines=1 dates=2026-05-21
```

`[caller=?]` means the cert wasn't enrolled (auth rejected before
binding any machine_id); a populated `[caller=...]` with
`status=PermissionDenied` means the cert was enrolled but the
request claimed a different machine_id (spoof attempt — `CrossCheck`
caught it).

## Enrolling a new laptop

First mint that laptop's client cert against the existing CA — without
re-issuing anything — with `test/gen-certs.sh --add-client <machine-id>`
(see the [setup runbook](../docs/MULTI_LAPTOP_SETUP.md#adding-a-laptop-later-incremental-no-downtime)).
Then record it on the VM (or wherever you have the `enrolled_machines.db`
file the server is configured to read):

```
python3 ../browser-visit-tools/enroll_machine.py \
    --db /var/lib/browser-visit-sync/enrolled_machines.db \
    --machine-id <id-printed-by-install_laptop.sh> \
    --cert /path/to/that-laptop-client.crt
```

The script SHA-256s the cert's DER bytes and inserts a row.  At
handshake time, the server's auth interceptor SHA-256s the peer's
leaf cert and looks for the matching row — no match means
`PermissionDenied` with no further detail (defense against
enumeration).

To list / revoke:
```
python3 ../browser-visit-tools/enroll_machine.py --db ... --list
python3 ../browser-visit-tools/enroll_machine.py --db ... --machine-id X --revoke
```

## Integration tests

Full Docker Compose suite under [`test/`](test/) — minted certs,
seeded enrolled DB, pytest driver, 8 end-to-end scenarios.  See
[`test/README.md`](test/README.md) for the workflow.  Short version:

```
make test-integration               # creates test/.venv on first run, ~15s
```

To poke at a live server:

```
BVL_TEST_KEEP_RUNNING=1 make test-integration         # leave stack up
docker compose -f docker-compose.test.yml logs -f sync-server   # watch
BVL_TEST_USE_EXISTING=1 test/.venv/bin/python -m pytest \
    test/test_sync.py::test_push_pull_round_trip -v   # in another terminal
docker compose -f docker-compose.test.yml down -v     # cleanup
```
