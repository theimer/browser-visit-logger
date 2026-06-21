#!/usr/bin/env python3
"""
sync_client.py — One-shot sync subprocess for a laptop.

Spawned (detached) by BVLHost after it finishes responding to Chrome.
Two RPCs to the EC2 sync-server:

  1. PushLogs — every local log line past the self-cursor.
  2. PullLogs — every peer log line past each peer-cursor.

Cursor state lives in the `sync_state` table of ``~/browser-visits.db``;
peer log mirrors land under ``~/browser-visits-peers/<peer>/``.

This file is the laptop-side companion to ``browser-visit-sync-server/``.
Authentication is mTLS (or insecure for local dev).

Concurrency: a flock on ``~/.browser-visit-logger/sync.lock`` prevents
overlapping invocations.

Error surfacing: failures are recorded in the existing ``mover_errors``
table with ``operation IN ('sync_push','sync_pull','sync_handshake')``.
The existing escalation pattern (BVL_MOVER_ERROR_THRESHOLD →
NotificationCenter via the host's Notify path) handles user-visible
alerts; this script just upserts the rows.

This module is intentionally importable but normally invoked via
``python3 -m browser_visit_logger.sync_client`` (see __main__ at bottom).
"""

import argparse
import datetime
import errno
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


HOME = os.path.expanduser('~')
CONFIG_DIR = os.environ.get('BVL_CONFIG_DIR', os.path.join(HOME, '.browser-visit-logger'))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
DB_FILE = os.environ.get('BVL_DB_FILE', os.path.join(HOME, 'browser-visits.db'))
LOG_DIR = os.environ.get('BVL_LOG_DIR', HOME)
PEER_LOGS_DIR = os.environ.get('BVL_PEER_LOGS_DIR',
                               os.path.join(HOME, 'browser-visits-peers'))
LOCK_FILE = os.path.join(CONFIG_DIR, 'sync.lock')
TLS_DIR = os.path.join(CONFIG_DIR, 'tls')
SYNC_INTERVAL_SECONDS = int(os.environ.get('BVL_SYNC_INTERVAL_SECONDS', '60'))


log = logging.getLogger('bvl.sync')


# ----------------------------------------------------------------- config

def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def machine_id_from_config() -> Optional[str]:
    env = os.environ.get('BVL_MACHINE_ID')
    if env:
        return env
    return load_config().get('machine_id')


def server_endpoint() -> Optional[str]:
    return os.environ.get('BVL_SYNC_SERVER') or load_config().get('sync_server')


# ----------------------------------------------------------------- db

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    # The visits / event tables are normally created by BVLHost before
    # sync ever runs, but ensure them here too so sync_client is
    # self-contained (e.g. a sync triggered before the first tag, or a
    # standalone test).  All idempotent.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visits (
            url         TEXT PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            of_interest TEXT,
            read        INTEGER NOT NULL DEFAULT 0,
            skimmed     INTEGER NOT NULL DEFAULT 0
        )
    """)
    for tbl in ('read_events', 'skimmed_events'):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
                url       TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                filename  TEXT NOT NULL DEFAULT '',
                directory TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (url, timestamp)
            )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            machine_id            TEXT PRIMARY KEY,
            cursor_log_date       TEXT NOT NULL DEFAULT '',
            cursor_line_offset    INTEGER NOT NULL DEFAULT 0,
            last_sync_attempt_at  TEXT NOT NULL DEFAULT '',
            last_sync_success_at  TEXT NOT NULL DEFAULT '',
            last_error            TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mover_errors (
            key        TEXT PRIMARY KEY,
            operation  TEXT NOT NULL,
            target     TEXT NOT NULL,
            message    TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 1,
            notified   INTEGER NOT NULL DEFAULT 0,
            immediate  INTEGER NOT NULL DEFAULT 0
        )
    """)
    return conn


def get_cursor(conn, machine_id: str) -> Tuple[str, int]:
    row = conn.execute(
        "SELECT cursor_log_date, cursor_line_offset FROM sync_state "
        "WHERE machine_id = ?", (machine_id,)).fetchone()
    if row is None:
        return '', -1
    # cursor_line_offset is NOT NULL, so row[1] is always an int here.
    return row[0] or '', row[1]


def set_cursor(conn, machine_id: str, date: str, offset: int) -> None:
    conn.execute("""
        INSERT INTO sync_state (machine_id, cursor_log_date, cursor_line_offset, last_sync_success_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(machine_id) DO UPDATE SET
            cursor_log_date = excluded.cursor_log_date,
            cursor_line_offset = excluded.cursor_line_offset,
            last_sync_success_at = excluded.last_sync_success_at
    """, (machine_id, date, offset, _now_iso()))


def mark_attempt(conn, machine_id: str, error: Optional[str]) -> None:
    conn.execute("""
        INSERT INTO sync_state (machine_id, last_sync_attempt_at, last_error)
        VALUES (?, ?, ?)
        ON CONFLICT(machine_id) DO UPDATE SET
            last_sync_attempt_at = excluded.last_sync_attempt_at,
            last_error = excluded.last_error
    """, (machine_id, _now_iso(), error or ''))


def should_skip(conn, machine_id: str) -> bool:
    row = conn.execute(
        "SELECT last_sync_attempt_at FROM sync_state WHERE machine_id = ?",
        (machine_id,)).fetchone()
    if not row or not row[0]:
        return False
    try:
        last = datetime.datetime.fromisoformat(row[0])
    except ValueError:
        return False
    delta = datetime.datetime.now(datetime.timezone.utc) - last
    return delta.total_seconds() < SYNC_INTERVAL_SECONDS


def record_error(conn, operation: str, target: str, message: str,
                 immediate: bool = False) -> None:
    key = f"{operation}|{target}"
    now = _now_iso()
    conn.execute("""
        INSERT INTO mover_errors (key, operation, target, message, first_seen, last_seen, attempts, immediate)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(key) DO UPDATE SET
            message = excluded.message,
            last_seen = excluded.last_seen,
            attempts = mover_errors.attempts + 1,
            immediate = MAX(mover_errors.immediate, excluded.immediate)
    """, (key, operation, target, message, now, now, 1 if immediate else 0))


def clear_error(conn, operation: str, target: str) -> None:
    conn.execute("DELETE FROM mover_errors WHERE key = ?",
                 (f"{operation}|{target}",))


# ----------------------------------------------------------------- logs

_LOG_RE = __import__('re').compile(r'^browser-visits-(\d{4}-\d{2}-\d{2})\.log$')


def list_local_log_dates() -> List[str]:
    if not os.path.isdir(LOG_DIR):
        return []
    out = []
    for name in os.listdir(LOG_DIR):
        m = _LOG_RE.match(name)
        if m:
            out.append(m.group(1))
    out.sort()
    return out


def read_log_lines_after(date: str, after_offset: int) -> Iterable[Tuple[int, str]]:
    path = os.path.join(LOG_DIR, f'browser-visits-{date}.log')
    if not os.path.isfile(path):
        return
    with open(path, encoding='utf-8') as f:
        for i, raw in enumerate(f):
            if i <= after_offset:
                continue
            yield i, raw.rstrip('\n')


def append_peer_line(peer: str, date: str, raw: str) -> None:
    d = os.path.join(PEER_LOGS_DIR, peer)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f'browser-visits-{date}.log')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(raw + '\n')


# ----------------------------------------------------------------- replay

def replay_into_db(conn, raw: str) -> None:
    """Apply one TSV log line to the local DB. Mirrors visits_rebuilder.py."""
    fields = raw.split('\t')
    if len(fields) == 2:
        return  # result line — ignore
    if len(fields) < 4:
        return
    _record_id, timestamp, url, title = fields[0], fields[1], fields[2], fields[3]
    conn.execute(
        "INSERT OR IGNORE INTO visits (url, timestamp, title) VALUES (?, ?, ?)",
        (url, timestamp, title))
    if len(fields) == 4:
        return
    tag = fields[4]
    if tag == 'of_interest':
        conn.execute("UPDATE visits SET of_interest = '1' WHERE url = ?", (url,))
        return
    if tag in ('read', 'skimmed') and len(fields) >= 6:
        filename = fields[5]
        events_table = f'{tag}_events'
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {events_table} (url, timestamp, filename) VALUES (?, ?, ?)",
            (url, timestamp, filename))
        # Only bump the counter for a genuinely new event row.  The events
        # PK is (url, timestamp), so replaying the same pulled line — e.g. a
        # re-pull after a crash before the peer cursor advanced — IGNOREs the
        # insert (rowcount 0) and must not double-count.  Mirrors
        # host._insert_event's rowcount gate.
        if cur.rowcount:
            conn.execute(f"UPDATE visits SET {tag} = {tag} + 1 WHERE url = ?", (url,))


# ----------------------------------------------------------------- grpc

def _load_stubs():
    here = os.path.dirname(os.path.abspath(__file__))
    gen = os.path.normpath(os.path.join(
        here, '..', '..', 'browser-visit-sync-server', 'gen', 'syncpb_py'))
    if gen not in sys.path:
        sys.path.insert(0, gen)
    import importlib
    try:
        sync_pb2 = importlib.import_module('sync_pb2')
        sync_pb2_grpc = importlib.import_module('sync_pb2_grpc')
        import grpc
    except ImportError as e:
        raise RuntimeError(
            f"grpc stubs not available: {e}. Run `make proto-py` in browser-visit-sync-server/.")
    return sync_pb2, sync_pb2_grpc, grpc


def build_channel(endpoint: str, insecure: bool):
    sync_pb2, sync_pb2_grpc, grpc = _load_stubs()
    if insecure:
        return grpc.insecure_channel(endpoint), sync_pb2, sync_pb2_grpc
    cert = Path(TLS_DIR, 'client.crt').read_bytes()
    key = Path(TLS_DIR, 'client.key').read_bytes()
    ca = Path(TLS_DIR, 'server-ca.crt').read_bytes()
    creds = grpc.ssl_channel_credentials(
        root_certificates=ca, private_key=key, certificate_chain=cert)
    return grpc.secure_channel(endpoint, creds), sync_pb2, sync_pb2_grpc


# ----------------------------------------------------------------- sync

def do_push(conn, channel, sync_pb2, sync_pb2_grpc, machine_id: str, endpoint: str) -> None:
    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel)
    cur_date, cur_off = get_cursor(conn, machine_id)
    local_dates = list_local_log_dates()
    dates = [d for d in local_dates if d >= cur_date] if cur_date else local_dates
    pb_lines = []
    last_date, last_off = cur_date, cur_off
    for d in dates:
        start_off = cur_off if d == cur_date else -1
        for off, raw in read_log_lines_after(d, start_off):
            pb_lines.append(sync_pb2.LogLine(date=d, line_offset=off, raw_line=raw))
            last_date, last_off = d, off
            if len(pb_lines) >= 500:
                _send_push(stub, sync_pb2, machine_id, pb_lines, endpoint, conn)
                pb_lines = []
    if pb_lines:
        _send_push(stub, sync_pb2, machine_id, pb_lines, endpoint, conn)
    if (last_date, last_off) != (cur_date, cur_off):
        set_cursor(conn, machine_id, last_date, last_off)


def _send_push(stub, sync_pb2, machine_id, pb_lines, endpoint, conn):
    req = sync_pb2.PushLogsRequest(machine_id=machine_id, lines=pb_lines)
    try:
        stub.PushLogs(req, timeout=30)
        clear_error(conn, 'sync_push', endpoint)
    except Exception as e:
        record_error(conn, 'sync_push', endpoint, str(e),
                     immediate=_is_auth_error(e))
        raise


def do_pull(conn, channel, sync_pb2, sync_pb2_grpc, machine_id: str, endpoint: str) -> None:
    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel)
    cursors = []
    for row in conn.execute(
            "SELECT machine_id, cursor_log_date, cursor_line_offset FROM sync_state"):
        cursors.append(sync_pb2.PeerCursor(
            peer_machine_id=row[0], date=row[1] or '',
            line_offset=row[2] if row[2] is not None else -1))
    req = sync_pb2.PullLogsRequest(machine_id=machine_id, cursors=cursors)
    try:
        for chunk in stub.PullLogs(req, timeout=120):
            peer = chunk.peer_machine_id
            last_date, last_off = get_cursor(conn, peer)
            for line in chunk.lines:
                append_peer_line(peer, line.date, line.raw_line)
                replay_into_db(conn, line.raw_line)
                last_date, last_off = line.date, line.line_offset
            set_cursor(conn, peer, last_date, last_off)
        clear_error(conn, 'sync_pull', endpoint)
    except Exception as e:
        record_error(conn, 'sync_pull', endpoint, str(e),
                     immediate=_is_auth_error(e))
        raise


def _is_auth_error(e: Exception) -> bool:
    s = str(e).lower()
    return 'unauthenticated' in s or 'permission_denied' in s or 'certificate' in s


# ----------------------------------------------------------------- lock

class FileLock:
    def __init__(self, path: str):
        self.path = path
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(self.fd)
            self.fd = None
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise SystemExit(0)  # another sync is already running
            raise
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ----------------------------------------------------------------- main

def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--insecure', action='store_true',
                        help='disable TLS (local dev only)')
    parser.add_argument('--force', action='store_true',
                        help='skip the SYNC_INTERVAL_SECONDS debounce')
    parser.add_argument('--server',
                        help='override sync server endpoint (host:port)')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s sync_client %(levelname)s %(message)s')

    machine_id = machine_id_from_config()
    if not machine_id:
        log.error("no machine_id configured; run install_laptop.sh first")
        return 2
    endpoint = args.server or server_endpoint()
    if not endpoint:
        log.info("no sync_server configured; nothing to do")
        return 0

    with FileLock(LOCK_FILE):
        conn = open_db()
        try:
            if not args.force and should_skip(conn, machine_id):
                log.info("last sync attempt was < %ds ago; skipping",
                         SYNC_INTERVAL_SECONDS)
                return 0
            mark_attempt(conn, machine_id, error=None)
            conn.commit()

            try:
                channel, sync_pb2, sync_pb2_grpc = build_channel(
                    endpoint, args.insecure)
            except Exception as e:
                mark_attempt(conn, machine_id, error=str(e))
                record_error(conn, 'sync_handshake', endpoint, str(e),
                             immediate=True)
                conn.commit()
                log.error("handshake failed: %s", e)
                return 1

            try:
                do_push(conn, channel, sync_pb2, sync_pb2_grpc,
                        machine_id, endpoint)
                conn.commit()
                do_pull(conn, channel, sync_pb2, sync_pb2_grpc,
                        machine_id, endpoint)
                conn.commit()
                mark_attempt(conn, machine_id, error=None)
                conn.commit()
                log.info("sync ok")
            except Exception as e:
                mark_attempt(conn, machine_id, error=str(e))
                conn.commit()
                log.error("sync failed: %s", e)
                return 1
        finally:
            conn.close()
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
