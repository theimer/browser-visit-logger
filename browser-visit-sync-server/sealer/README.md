# VM-side snapshot sealer

Code that runs **on the EC2 VM** (alongside the Go sync-server) to seal
completed days in the Google Drive snapshot archive — the server-side
half of the multi-laptop architecture (see
[issue #47](https://github.com/theimer/browser-visit-logger/issues/47)).

- **`seal_completed_days.py`** — walks the Drive mount
  (`/mnt/gdrive-snapshots`) and, for every completed UTC day whose
  directory has no `MANIFEST.tsv`, writes the manifest and flips the
  `snapshots` row to `sealed=1` in the canonical DB. Filename-only event
  matching (`BVL_MATCH_BY_FILENAME=1`), since the synced DB records
  `filename` but not `directory`. Idempotent; `--dry-run` previews.

Deployed and scheduled by
[`browser-visit-tools/manage_sync_server.py`](../../browser-visit-tools/manage_sync_server.py)
(`provision-sealer` / `sealer-run`) as the `gdrive-verifier` systemd
oneshot + hourly timer (units in [`../deploy/`](../deploy/)).

It depends on the shared, stdlib-only sealing library `snapshot_mover.py`
(currently in `browser-visit-logger/native-host/`, still shared with the
laptop-side manual tools; `provision-sealer` ships both files
side-by-side into `/usr/local/lib/bvl/` on the VM). Tests live in
[`browser-visit-logger/tests/test_seal_completed_days.py`](../../browser-visit-logger/tests/test_seal_completed_days.py).
