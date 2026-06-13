# Integration tests

End-to-end tests that exercise the dockerized sync-server with real
mTLS over a real TCP socket.  Designed to run on the developer's Mac
without an EC2 instance.

## What's tested

| Scenario | Verifies |
|---|---|
| Push + Pull round-trip | A laptop's pushed lines reach other laptops; the sender doesn't get its own lines echoed. |
| Push idempotency | Replaying the same `PushLogs` doesn't double-count. |
| Read counter convergence | `visits.read` counter sums to 2 when two laptops mark the same URL as read. |
| Rogue cert rejection | A signed-but-not-enrolled cert gets `PERMISSION_DENIED`. |
| Identity spoof rejection | Laptop A's cert claiming to be laptop B is refused. |
| `ExportDbSnapshot` validity | Streamed bytes form a valid SQLite file with the expected tables. |
| End-to-end with `db_diff.py` | `db_diff.py` reports zero differences when fed the same snapshot twice. |

## Prereqs

- Docker + `docker compose` (Docker Desktop on Mac, or rootless Docker).
- Python 3 (the venv + deps are created automatically — see Running).
- `openssl` and `bash` on `$PATH` (default on macOS).

`protoc` and `go` are **not** required on the host — the Docker build
installs them and runs `make proto` + `go mod tidy` + `go build`
internally.  The Python test driver generates its own gRPC stubs via
`grpcio-tools` (installed into the test venv).

## Running

```
cd browser-visit-sync-server
make test-integration
```

`make test-integration` is the only command you should ever need.  On
first run it creates `test/.venv/` (a Python virtualenv) and installs
`test/requirements.txt` into it; subsequent runs reuse the venv.  The
host's system Python is never touched.

To rebuild the venv after editing `requirements.txt`:

```
make clean-venv test-integration
```

## Watching the server live

Two terminals.  In the first, bring up a sticky stack and tail logs:

```
BVL_TEST_KEEP_RUNNING=1 make test-integration   # leaves the stack up
docker compose -f docker-compose.test.yml logs -f sync-server
```

In a second terminal, fire individual tests at the *already-running*
stack (do NOT use the normal `make test-integration` here — it would
tear down the server out from under your `logs -f`):

```
BVL_TEST_USE_EXISTING=1 test/.venv/bin/python -m pytest \
    test/test_sync.py::test_push_pull_round_trip -v
```

When done, in either terminal:

```
docker compose -f docker-compose.test.yml down -v
```

To keep the stack running between runs (for debugging):

```
BVL_TEST_KEEP_RUNNING=1 make test-integration
docker compose -f docker-compose.test.yml logs -f sync-server
docker compose -f docker-compose.test.yml down -v   # cleanup
```

## State that lives on disk

```
test/certs/                     minted on every run by gen-certs.sh
test/state/enrolled_machines.db pre-seeded by seed-enrolled.py
docker volume bvl-state         server's /var/lib (cleaned by `down -v`)
```

The certs and state directories are wiped at the start of every test
session, so each run starts deterministic.
