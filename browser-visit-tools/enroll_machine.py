#!/usr/bin/env python3
"""
enroll_machine.py — Admin helper that grants a laptop access to the
sync-server by recording its (machine_id, client cert SHA-256) pair
in the VM's ``enrolled_machines.db``.

Run this on the EC2 VM, or wherever you have access to the
``enrolled_machines.db`` SQLite file the sync-server is configured to
read.

Usage
-----
    # Enroll
    enroll_machine.py \\
        --db /var/lib/browser-visit-sync/enrolled_machines.db \\
        --machine-id marvins-mbp \\
        --cert /etc/browser-visit-sync/clients/marvins-mbp.crt

    # List
    enroll_machine.py --db ... --list

    # Revoke
    enroll_machine.py --db ... --machine-id marvins-mbp --revoke
"""

import argparse
import datetime
import hashlib
import os
import sqlite3
import sys
from pathlib import Path


def cert_fingerprint(cert_path: Path) -> str:
    """SHA-256 hex of the DER-encoded leaf cert."""
    data = cert_path.read_bytes()
    if b'-----BEGIN CERTIFICATE-----' in data:
        # PEM → DER
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            print("error: needs `cryptography` (pip install cryptography) "
                  "to decode PEM. Or supply a DER file directly.",
                  file=sys.stderr)
            sys.exit(2)
        cert = x509.load_pem_x509_certificate(data)
        der = cert.public_bytes(serialization.Encoding.DER)
    else:
        der = data
    return hashlib.sha256(der).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrolled_machines (
            machine_id  TEXT PRIMARY KEY,
            cert_sha256 TEXT NOT NULL,
            enrolled_at TEXT NOT NULL
        )
    """)


def enroll(db_path: Path, machine_id: str, cert_path: Path) -> None:
    fp = cert_fingerprint(cert_path)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO enrolled_machines "
            "(machine_id, cert_sha256, enrolled_at) VALUES (?, ?, ?)",
            (machine_id, fp,
             datetime.datetime.now(datetime.timezone.utc).isoformat()))
        conn.commit()
        print(f"enrolled {machine_id} with fingerprint {fp}")
    finally:
        conn.close()


def revoke(db_path: Path, machine_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM enrolled_machines WHERE machine_id = ?",
            (machine_id,))
        conn.commit()
        print(f"revoked {machine_id} (deleted {cur.rowcount} row(s))")
    finally:
        conn.close()


def list_all(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        for row in conn.execute(
                "SELECT machine_id, cert_sha256, enrolled_at "
                "FROM enrolled_machines ORDER BY machine_id"):
            print(f"{row[0]:30s}  {row[1][:16]}...  {row[2]}")
    finally:
        conn.close()


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument('--db', required=True)
    p.add_argument('--machine-id')
    p.add_argument('--cert', help='PEM or DER encoded client cert')
    p.add_argument('--revoke', action='store_true')
    p.add_argument('--list', action='store_true')
    args = p.parse_args(argv)

    db = Path(os.path.expanduser(args.db))
    db.parent.mkdir(parents=True, exist_ok=True)

    if args.list:
        list_all(db)
        return 0
    if not args.machine_id:
        print("error: --machine-id required (or use --list)", file=sys.stderr)
        return 2
    if args.revoke:
        revoke(db, args.machine_id)
        return 0
    if not args.cert:
        print("error: --cert required to enroll", file=sys.stderr)
        return 2
    enroll(db, args.machine_id, Path(os.path.expanduser(args.cert)))
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
