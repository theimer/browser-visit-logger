#!/usr/bin/env python3
"""
seal_completed_days.py — VM-side seal pass over the Google Drive snapshot
archive.

The multi-laptop architecture moves sealing/verification off the laptops
onto the EC2 VM (see issue #47).  This is the VM counterpart of the
laptop's Swift `Seal.pass`: it walks the mounted archive
(``BVL_ICLOUD_SNAPSHOTS_DIR``, e.g. ``/mnt/gdrive-snapshots``), and for
every *completed* UTC day (date < today) whose directory has no
``MANIFEST.tsv`` yet, writes the manifest and flips the ``snapshots``
row to sealed=1 in the canonical DB (``BVL_DB_FILE``).

It deliberately differs from the laptop sealer in two ways:

  * **Filename-only event matching** (``BVL_MATCH_BY_FILENAME=1``): the
    canonical DB is populated by the sync server, which records
    ``filename`` but never ``directory``, and the mount path differs from
    every laptop's local Drive path.  Snapshot filenames are globally
    unique, so filename alone is the join key.
  * **No orphan-log-merge pass**: that laptop step scans a flat
    ``LOG_DIR`` of per-day logs, which the VM does not have (its log
    mirrors live under ``<data>/logs/<machine_id>/``).

Idempotent: an already-manifested day is skipped, and re-runs are safe.
Per-directory seal failures are recorded in ``mover_errors`` (surfaced,
never fatal) and do not stop the remaining days.

Config comes from the same ``BVL_*`` env vars ``snapshot_mover`` reads;
``--dest`` / ``--db`` override them for tests, and ``--dry-run`` reports
what would be sealed without writing.

Exit codes: 0 success, 1 setup failure (no archive dir / no DB).
"""

import argparse
import logging
import os
import sqlite3
import sys

import snapshot_mover


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog='seal_completed_days.py',
        description='Seal every completed, not-yet-sealed day directory in '
                    'the snapshot archive.')
    p.add_argument('--dry-run', action='store_true',
                   help='list the directories that would be sealed, and how '
                        'many snapshot files each holds, without writing')
    p.add_argument('--dest', metavar='DIR',
                   help='override the archive root '
                        f'(default {snapshot_mover.ICLOUD_SNAPSHOTS_DIR})')
    p.add_argument('--db', metavar='FILE',
                   help=f'override the DB path (default {snapshot_mover.DB_FILE})')
    p.add_argument('--match-by-filename', action='store_true',
                   help='match files to event rows by filename alone '
                        '(implied on the VM via BVL_MATCH_BY_FILENAME=1)')
    return p.parse_args(argv)


def _snapshot_file_count(subdir):
    """Number of conforming snapshot files in a day dir (for the dry-run)."""
    return sum(
        1 for f in os.listdir(subdir)
        if snapshot_mover._SNAPSHOT_FILENAME_RE.match(f)
    )


def cli(argv=None):
    args = _parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    if args.dest is not None:
        snapshot_mover.ICLOUD_SNAPSHOTS_DIR = args.dest
    if args.db is not None:
        snapshot_mover.DB_FILE = args.db
    if args.match_by_filename:
        snapshot_mover.MATCH_BY_FILENAME_ONLY = True

    root = snapshot_mover.ICLOUD_SNAPSHOTS_DIR
    if not os.path.isdir(root):
        print(f'No archive directory at {root}', file=sys.stderr)
        return 1
    if not os.path.exists(snapshot_mover.DB_FILE):
        print(f'No DB at {snapshot_mover.DB_FILE}', file=sys.stderr)
        return 1

    today_iso = snapshot_mover._today_utc().isoformat()
    # Sort so the sealed order is deterministic (oldest first).
    candidates = []
    for name in sorted(os.listdir(root)):
        if not snapshot_mover._DATE_DIR_RE.match(name):
            continue
        # Only completed days: today's dir is still receiving snapshots
        # (Drive is syncing it), and future-dated dirs are clock skew.
        if name >= today_iso:
            continue
        subdir = os.path.join(root, name)
        if not os.path.isdir(subdir):
            continue
        if os.path.exists(os.path.join(subdir, snapshot_mover.MANIFEST_FILENAME)):
            continue  # already sealed
        candidates.append((name, subdir))

    if args.dry_run:
        for name, subdir in candidates:
            print(f'would seal {name}  ({_snapshot_file_count(subdir)} files)')
        print(f'{len(candidates)} director'
              f'{"y" if len(candidates) == 1 else "ies"} would be sealed')
        return 0

    conn = sqlite3.connect(snapshot_mover.DB_FILE)
    try:
        snapshot_mover._ensure_snapshots_table(conn)
        snapshot_mover._ensure_mover_errors_table(conn)
        for name, subdir in candidates:
            snapshot_mover._seal_directory(conn, subdir, date_key=name)
    finally:
        conn.close()
    print(f'sealed {len(candidates)} director'
          f'{"y" if len(candidates) == 1 else "ies"}')
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(cli())
