#!/usr/bin/env python3
"""
migrate_icloud_to_gdrive.py — One-time migration of historical snapshots
from the iCloud archive directory to the new Google Drive archive.

What it does:
  1. Copies every file under the iCloud snapshots root to the Drive
     snapshots root, preserving the YYYY-MM-DD/ subdir layout.
  2. Verifies each file via SHA-256 before considering it migrated.
  3. Rewrites the `directory` column in `read_events` and
     `skimmed_events` for rows whose `directory` points into the
     iCloud root, swapping in the Drive root.
  4. Leaves the iCloud copy untouched.  The user can delete it after
     confirming everything still works.

Idempotent: re-running skips files already present at the destination
with a matching hash, and the SQL UPDATE only touches rows whose
`directory` still starts with the iCloud root.

Usage
-----
    migrate_icloud_to_gdrive.py \\
        --src ~/Documents/browser-visit-logger/snapshots \\
        --dst "~/Library/CloudStorage/GoogleDrive/My Drive/browser-visit-logger/snapshots" \\
        --db  ~/browser-visits.db

    # Dry run — print what would be copied / what SQL would be emitted
    migrate_icloud_to_gdrive.py --src ... --dst ... --db ... --dry-run
"""

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def migrate_files(src: Path, dst: Path, dry_run: bool) -> int:
    copied = 0
    for root, _, files in os.walk(src):
        rel = Path(root).relative_to(src)
        out_dir = dst / rel
        for fn in files:
            src_path = Path(root) / fn
            dst_path = out_dir / fn
            if dst_path.exists():
                # Verify hashes match; if they do, skip.
                if sha256_of(src_path) == sha256_of(dst_path):
                    continue
                print(f"warn: {dst_path} exists with different hash, leaving alone", file=sys.stderr)
                continue
            if dry_run:
                print(f"would copy: {src_path}  ->  {dst_path}")
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                # Copy via a temp name then rename to avoid partials
                # being picked up by the verifier on the VM.
                tmp = dst_path.with_suffix(dst_path.suffix + '.partial')
                shutil.copy2(src_path, tmp)
                if sha256_of(tmp) != sha256_of(src_path):
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"hash mismatch after copy: {src_path}")
                tmp.rename(dst_path)
            copied += 1
    return copied


def rewrite_db_directories(db_path: Path, old_root: Path, new_root: Path,
                           dry_run: bool) -> int:
    conn = sqlite3.connect(str(db_path))
    total = 0
    try:
        old = str(old_root).rstrip('/') + '/'
        new = str(new_root).rstrip('/') + '/'
        for table in ('read_events', 'skimmed_events'):
            sql = (f"UPDATE {table} SET directory = "
                   f"REPLACE(directory, ?, ?) WHERE directory LIKE ?")
            params = (old, new, old + '%')
            if dry_run:
                # Count affected rows without modifying.
                count_sql = f"SELECT COUNT(*) FROM {table} WHERE directory LIKE ?"
                n = conn.execute(count_sql, (old + '%',)).fetchone()[0]
                print(f"would update {n} rows in {table}: {old} -> {new}")
                total += n
            else:
                cur = conn.execute(sql, params)
                total += cur.rowcount or 0
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return total


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument('--src', required=True, help='iCloud snapshots root')
    p.add_argument('--dst', required=True, help='Google Drive snapshots root')
    p.add_argument('--db', required=True, help='browser-visits.db path')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)

    src = Path(os.path.expanduser(args.src))
    dst = Path(os.path.expanduser(args.dst))
    db = Path(os.path.expanduser(args.db))

    if not src.exists():
        print(f"error: --src not found: {src}", file=sys.stderr)
        return 2
    if not db.exists():
        print(f"error: --db not found: {db}", file=sys.stderr)
        return 2
    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    copied = migrate_files(src, dst, args.dry_run)
    print(f"{'would copy' if args.dry_run else 'copied'} {copied} files")

    updated = rewrite_db_directories(db, src, dst, args.dry_run)
    print(f"{'would update' if args.dry_run else 'updated'} {updated} DB rows")

    if not args.dry_run:
        print("done. The iCloud copy was left in place; remove it manually "
              "after verifying things still work.")
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
