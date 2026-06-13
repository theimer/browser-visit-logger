"""Unit tests for migrate_icloud_to_gdrive.py."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import migrate_icloud_to_gdrive as mig  # noqa: E402


def _seed_snapshots(src):
    d = src / '2026-05-21'
    d.mkdir(parents=True)
    (d / 'a.mhtml').write_text('alpha')
    (d / 'b.pdf').write_text('bravo')
    return d


def _seed_db(path, directory):
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE read_events (url TEXT, timestamp TEXT, filename TEXT,
            directory TEXT, PRIMARY KEY (url, timestamp));
        CREATE TABLE skimmed_events (url TEXT, timestamp TEXT, filename TEXT,
            directory TEXT, PRIMARY KEY (url, timestamp));
    """)
    c.execute("INSERT INTO read_events VALUES ('u1','t','a.mhtml',?)",
              (directory + '/2026-05-21',))
    c.execute("INSERT INTO skimmed_events VALUES ('u2','t','b.pdf',?)",
              (directory + '/2026-05-21',))
    c.commit()
    c.close()


def test_sha256_of(tmp_path):
    f = tmp_path / 'x'
    f.write_text('hello')
    import hashlib
    assert mig.sha256_of(f) == hashlib.sha256(b'hello').hexdigest()


def test_migrate_files_copies(tmp_path):
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    n = mig.migrate_files(src, dst, dry_run=False)
    assert n == 2
    assert (dst / '2026-05-21' / 'a.mhtml').read_text() == 'alpha'
    # No leftover .partial files.
    assert not list(dst.rglob('*.partial'))


def test_migrate_files_dry_run(tmp_path, capsys):
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    n = mig.migrate_files(src, dst, dry_run=True)
    assert n == 2
    assert not dst.exists()  # nothing actually written
    assert 'would copy' in capsys.readouterr().out


def test_migrate_files_skip_matching_hash(tmp_path):
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    mig.migrate_files(src, dst, dry_run=False)
    # Second run: destination already has identical files → 0 copied.
    n = mig.migrate_files(src, dst, dry_run=False)
    assert n == 0


def test_migrate_files_warn_on_hash_mismatch(tmp_path, capsys):
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    # Pre-create a destination file with different content.
    (dst / '2026-05-21').mkdir(parents=True)
    (dst / '2026-05-21' / 'a.mhtml').write_text('DIFFERENT')
    (dst / '2026-05-21' / 'b.pdf').write_text('bravo')  # matches → skipped
    n = mig.migrate_files(src, dst, dry_run=False)
    assert n == 0  # neither copied (one mismatch warned, one identical)
    assert 'different hash' in capsys.readouterr().err


def test_rewrite_db_directories(tmp_path):
    db = tmp_path / 'v.db'
    old = tmp_path / 'icloud'
    new = tmp_path / 'gdrive'
    _seed_db(db, str(old))
    n = mig.rewrite_db_directories(db, old, new, dry_run=False)
    assert n == 2
    c = sqlite3.connect(str(db))
    rows = [r[0] for r in c.execute(
        "SELECT directory FROM read_events UNION SELECT directory FROM skimmed_events")]
    c.close()
    assert all(str(new) in r for r in rows)
    assert all(str(old) not in r for r in rows)


def test_rewrite_db_directories_dry_run(tmp_path, capsys):
    db = tmp_path / 'v.db'
    old, new = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_db(db, str(old))
    n = mig.rewrite_db_directories(db, old, new, dry_run=True)
    assert n == 2
    # Unchanged on disk.
    c = sqlite3.connect(str(db))
    rows = [r[0] for r in c.execute("SELECT directory FROM read_events")]
    c.close()
    assert str(old) in rows[0]
    assert 'would update' in capsys.readouterr().out


# ---- main ----

def test_main_dry_run(tmp_path, capsys):
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    db = tmp_path / 'v.db'
    _seed_db(db, str(src))
    rc = mig.main(['--src', str(src), '--dst', str(dst), '--db', str(db), '--dry-run'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'would copy' in out and 'would update' in out


def test_main_real_run(tmp_path, capsys):
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    db = tmp_path / 'v.db'
    _seed_db(db, str(src))
    rc = mig.main(['--src', str(src), '--dst', str(dst), '--db', str(db)])
    assert rc == 0
    assert (dst / '2026-05-21' / 'a.mhtml').exists()
    assert 'done' in capsys.readouterr().out


def test_main_src_missing(tmp_path, capsys):
    db = tmp_path / 'v.db'
    _seed_db(db, str(tmp_path / 'icloud'))
    rc = mig.main(['--src', str(tmp_path / 'nope'), '--dst', str(tmp_path / 'g'),
                   '--db', str(db)])
    assert rc == 2
    assert 'src not found' in capsys.readouterr().err


def test_main_db_missing(tmp_path, capsys):
    src = tmp_path / 'icloud'
    _seed_snapshots(src)
    rc = mig.main(['--src', str(src), '--dst', str(tmp_path / 'g'),
                   '--db', str(tmp_path / 'nope.db')])
    assert rc == 2
    assert 'db not found' in capsys.readouterr().err


def test_migrate_files_hash_mismatch_after_copy(tmp_path, monkeypatch):
    # Force the post-copy hash check to fail → RuntimeError.
    src, dst = tmp_path / 'icloud', tmp_path / 'gdrive'
    _seed_snapshots(src)
    calls = {'n': 0}
    real = mig.sha256_of

    def flaky(path):
        # Return a wrong hash for the temp (.partial) verification only.
        if str(path).endswith('.partial'):
            return 'deadbeef'
        return real(path)
    monkeypatch.setattr(mig, 'sha256_of', flaky)
    with pytest.raises(RuntimeError, match='hash mismatch'):
        mig.migrate_files(src, dst, dry_run=False)
