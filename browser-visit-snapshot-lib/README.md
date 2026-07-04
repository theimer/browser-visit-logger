# browser-visit-snapshot-lib

Shared, **stdlib-only** Python library for the browser-visit snapshot
archive — sealing a day's directory (writing a read-only `MANIFEST.tsv`
from the events tables), the `mover_errors` bookkeeping, the
orphan-log-merge pass, and the shared filename/date regexes and helpers.

It lives in its own top-level component because it is imported from
**both** sides of the multi-laptop architecture:

- **Laptop** — the manual CLI tools
  [`snapshot_sealer.py`](../browser-visit-logger/native-host/snapshot_sealer.py)
  and
  [`visits_rebuilder.py`](../browser-visit-logger/native-host/visits_rebuilder.py)
  (in `browser-visit-logger/native-host/`, run via the
  `seal_snapshot_directory` / `rebuild_visits_data` wrappers).
- **VM** — the server-side seal pass
  [`seal_completed_days.py`](../browser-visit-sync-server/sealer/seal_completed_days.py),
  which `manage_sync_server.py provision-sealer` deploys next to this
  module under `/usr/local/lib/bvl/` (so the same-directory import
  resolves on the VM).

Each importer adds this directory to `sys.path` before `import
snapshot_mover`, so it resolves however the tool is launched.

The production laptop move/seal/verify pipeline runs in Swift
(`BVLHost` / `BVLVerifier`); this module is the Python implementation the
surviving CLI tools and the VM sealer share. Its tests are
[`browser-visit-logger/tests/test_snapshot_mover.py`](../browser-visit-logger/tests/test_snapshot_mover.py).
