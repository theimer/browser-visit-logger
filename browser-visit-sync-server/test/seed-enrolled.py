#!/usr/bin/env python3
"""Seed the enrolled_machines.db from a fingerprints.tsv.

Used by the test harness before starting the server.  Reads
`<certs-dir>/fingerprints.tsv` (one `machine_id<TAB>cert_sha256` per
line) and inserts a row for each (skipping any IDs passed via
``--exclude``, which lets us mint a "rogue" cert that isn't enrolled
so we can prove the auth path rejects it).
"""
import argparse
import datetime
import os
import sqlite3
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--certs-dir', required=True)
    p.add_argument('--db', required=True)
    p.add_argument('--exclude', action='append', default=[],
                   help='machine_id values to omit from the enrolled table')
    args = p.parse_args()

    fp_path = os.path.join(args.certs_dir, 'fingerprints.tsv')
    conn = sqlite3.connect(args.db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrolled_machines (
            machine_id TEXT PRIMARY KEY,
            cert_sha256 TEXT NOT NULL,
            enrolled_at TEXT NOT NULL
        )
    """)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    skipped = set(args.exclude)
    with open(fp_path) as f:
        for line in f:
            mid, fp = line.rstrip('\n').split('\t')
            if mid in skipped:
                print(f"skipping {mid}")
                continue
            conn.execute(
                "INSERT OR REPLACE INTO enrolled_machines "
                "(machine_id, cert_sha256, enrolled_at) VALUES (?, ?, ?)",
                (mid, fp, now))
            print(f"enrolled {mid}")
    conn.commit()
    conn.close()


if __name__ == '__main__':
    main()
