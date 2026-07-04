"""Tests for seal_completed_days.py — the VM-side seal pass.

Exercises the driver against a throwaway archive + DB where event rows
carry an EMPTY directory (as the synced VM DB does), so the filename-only
match path is what makes sealing work.
"""
import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import host
import snapshot_mover

# seal_completed_days.py is VM code and lives with the sync-server, not in
# native-host; add its directory so the import resolves (conftest already
# puts native-host on the path for host/snapshot_mover).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / 'browser-visit-sync-server' / 'sealer'))
import seal_completed_days as scd  # noqa: E402


class SealCompletedDaysTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, 'archive')
        os.makedirs(self.root)
        self.db = os.path.join(self.tmp.name, 'db.sqlite')
        conn = sqlite3.connect(self.db)
        host.ensure_db(conn)
        snapshot_mover._ensure_snapshots_table(conn)
        conn.close()
        # Freeze "today" so date < today is deterministic.
        p = patch.object(snapshot_mover, '_today_utc',
                         lambda: datetime.date(2024, 2, 1))
        p.start()
        self.addCleanup(p.stop)
        # The CLI sets this module global from --match-by-filename and never
        # resets it (fine for a one-shot process); restore it between tests so
        # it doesn't leak into other suites sharing the module.
        orig = snapshot_mover.MATCH_BY_FILENAME_ONLY
        self.addCleanup(
            setattr, snapshot_mover, 'MATCH_BY_FILENAME_ONLY', orig)

    def _daydir(self, date):
        d = os.path.join(self.root, date)
        os.makedirs(d, exist_ok=True)
        return d

    def _add_snapshot(self, date, hms='10-00-00', tag='read'):
        """Drop a conforming snapshot file + an event row with EMPTY dir."""
        base = f'{date}T{hms}Z-abc.mhtml'
        Path(self._daydir(date), base).write_bytes(b'data')
        ts = f'{date}T{hms.replace("-", ":")}Z'
        conn = sqlite3.connect(self.db)
        host.insert_visit(conn, ts, 'https://a.com', 'A')
        table = 'read_events' if tag == 'read' else 'skimmed_events'
        conn.execute(
            f"INSERT INTO {table} (url, timestamp, filename, directory) "
            f"VALUES (?, ?, ?, '')", ('https://a.com', ts, base))
        conn.commit()
        conn.close()
        return base

    def _run(self, *extra):
        return scd.cli(['--dest', self.root, '--db', self.db,
                        '--match-by-filename', *extra])

    def _sealed(self, date):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT sealed FROM snapshots WHERE date = ?", (date,)).fetchone()
        conn.close()
        return row[0] if row else None

    def test_seals_completed_unsealed_day(self):
        base = self._add_snapshot('2024-01-15')
        self.assertEqual(self._run(), 0)
        manifest = Path(self.root, '2024-01-15', 'MANIFEST.tsv')
        self.assertTrue(manifest.exists())
        # The file matched its event row (by filename) and made the manifest.
        self.assertIn(base, manifest.read_text())
        self.assertEqual(self._sealed('2024-01-15'), 1)

    def test_skips_today_and_future(self):
        self._add_snapshot('2024-02-01')   # today (still receiving snapshots)
        self._add_snapshot('2099-01-01')   # future (clock skew)
        self.assertEqual(self._run(), 0)
        self.assertFalse(Path(self.root, '2024-02-01', 'MANIFEST.tsv').exists())
        self.assertFalse(Path(self.root, '2099-01-01', 'MANIFEST.tsv').exists())

    def test_skips_already_sealed_day(self):
        d = self._daydir('2024-01-10')
        Path(d, 'MANIFEST.tsv').write_text('existing manifest')
        self.assertEqual(self._run(), 0)
        # Left untouched — not rebuilt.
        self.assertEqual(Path(d, 'MANIFEST.tsv').read_text(), 'existing manifest')

    def test_dry_run_writes_nothing(self):
        self._add_snapshot('2024-01-15')
        self.assertEqual(self._run('--dry-run'), 0)
        self.assertFalse(Path(self.root, '2024-01-15', 'MANIFEST.tsv').exists())
        self.assertIsNone(self._sealed('2024-01-15'))

    def test_missing_archive_dir_returns_1(self):
        self.assertEqual(
            scd.cli(['--dest', '/no/such/dir', '--db', self.db]), 1)

    def test_missing_db_returns_1(self):
        self.assertEqual(
            scd.cli(['--dest', self.root, '--db', '/no/such/db']), 1)


if __name__ == '__main__':
    unittest.main()
