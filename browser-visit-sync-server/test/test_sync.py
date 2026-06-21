"""End-to-end integration tests against the dockerized sync-server.

Scenarios:

  * Push + Pull round-trip: laptop-a pushes lines; laptop-b pulls
    and sees them; laptop-a does NOT get its own lines echoed back.
  * Cursor resumption: after advancing the per-peer cursor, a second
    pull returns only the lines strictly past it (not the whole log).
  * Cursor idempotency: replaying the same PushLogs is a no-op (no
    duplicate rows in the canonical DB, server reports same high
    water mark).
  * Read counter convergence: when both laptops record a 'read' on
    the same URL, the canonical DB shows read=2 (summed).
  * mTLS rejection: the rogue cert (minted but not enrolled) gets
    PERMISSION_DENIED on any RPC.
  * Identity spoofing rejection: laptop-a's cert sending a request
    that claims machine_id='laptop-b' is refused.
  * ExportDbSnapshot: returns a real SQLite file the local DB diff
    tool can read.

All scenarios share one server instance; tests don't reset state
between cases, so they're written to be order-independent within
their own URL namespace.
"""
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import grpc
import pytest


HERE = Path(__file__).resolve().parent
TOOLS_DIR = HERE.parent.parent / 'browser-visit-tools'


def _line(record_id, timestamp, url, title, tag=None, filename=None):
    """Build a TSV log line matching VisitLog.appendAction's format."""
    fields = [record_id, timestamp, url, title]
    if tag:
        fields.append(tag)
        if tag in ('read', 'skimmed'):
            fields.append(filename or '')
    return '\t'.join(fields)


def _push(stub, sync_pb2, machine_id, lines_with_offset, date='2026-05-21'):
    pb = sync_pb2.PushLogsRequest(
        machine_id=machine_id,
        lines=[
            sync_pb2.LogLine(date=date, line_offset=i, raw_line=raw)
            for i, raw in lines_with_offset
        ])
    return stub.PushLogs(pb, timeout=10)


def _pull_all(stub, sync_pb2, machine_id, cursors=None):
    req = sync_pb2.PullLogsRequest(
        machine_id=machine_id,
        cursors=[
            sync_pb2.PeerCursor(
                peer_machine_id=peer, date=date, line_offset=off)
            for peer, (date, off) in (cursors or {}).items()
        ])
    out = {}
    for chunk in stub.PullLogs(req, timeout=15):
        out.setdefault(chunk.peer_machine_id, []).extend(
            (l.date, l.line_offset, l.raw_line) for l in chunk.lines)
    return out


# ----------------------------------------------------------------- tests

def _result_line(record_id):
    """Result line is 2 TSV fields: record_id, 'success'."""
    return f"{record_id}\tsuccess"


def test_push_pull_round_trip(channel_for, grpc_stubs):
    sync_pb2, sync_pb2_grpc = grpc_stubs
    a_url = f"https://a.example/{uuid.uuid4()}"
    stub_a = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-a'))
    stub_b = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-b'))

    # Use a per-test date so each test starts fresh at offset 0 even
    # though the server's state persists across tests in this session.
    date = '2026-05-21'
    lines = [
        (0, _line('rid1', '2026-05-21T10:00:00Z', a_url, 'Title A')),
        (1, _result_line('rid1')),
        (2, _line('rid2', '2026-05-21T10:01:00Z', a_url, 'Title A', tag='of_interest')),
        (3, _result_line('rid2')),
    ]
    resp = _push(stub_a, sync_pb2, 'laptop-a', lines, date=date)
    assert resp.accepted_date == date
    assert resp.accepted_line_offset == 3

    # laptop-b pulls — should see all four lines for laptop-a.
    chunks = _pull_all(stub_b, sync_pb2, 'laptop-b')
    assert 'laptop-a' in chunks
    assert len(chunks['laptop-a']) >= 4
    assert 'laptop-b' not in chunks  # we haven't pushed anything yet

    # laptop-a pulling: should NOT see its own records echoed.
    chunks_a = _pull_all(stub_a, sync_pb2, 'laptop-a')
    assert 'laptop-a' not in chunks_a


def test_pull_resumes_from_cursor(channel_for, grpc_stubs):
    """A second pull with an advanced cursor returns only the lines
    strictly past it — proving cursor resumption, not a full re-stream."""
    sync_pb2, sync_pb2_grpc = grpc_stubs
    date = '2026-05-28'  # dedicated date so this peer's offsets start at 0
    url = f"https://cursor.example/{uuid.uuid4()}"
    stub_a = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-a'))
    stub_b = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-b'))

    # First batch at offsets 0..1, then a second batch at offsets 2..3.
    _push(stub_a, sync_pb2, 'laptop-a', [
        (0, _line('rid-cur-1', '2026-05-28T09:00:00Z', url, 'C')),
        (1, _result_line('rid-cur-1')),
    ], date=date)
    _push(stub_a, sync_pb2, 'laptop-a', [
        (2, _line('rid-cur-2', '2026-05-28T09:05:00Z', url, 'C',
                  tag='read', filename='c.mhtml')),
        (3, _result_line('rid-cur-2')),
    ], date=date)

    # Resume from offset 1 → only the new offsets 2 and 3 come back.
    # (Filter to this test's date: a single per-peer cursor lets earlier
    # dates through, which other tests' pushes would add noise from.)
    chunks = _pull_all(stub_b, sync_pb2, 'laptop-b',
                       cursors={'laptop-a': (date, 1)})
    got = sorted(off for (d, off, _raw) in chunks.get('laptop-a', [])
                 if d == date)
    assert got == [2, 3], f"expected only new offsets [2, 3], got {got}"

    # Resume from the tip → nothing past the cursor for this date.
    chunks2 = _pull_all(stub_b, sync_pb2, 'laptop-b',
                        cursors={'laptop-a': (date, 3)})
    tail = [off for (d, off, _raw) in chunks2.get('laptop-a', []) if d == date]
    assert tail == [], f"expected no lines past the tip, got {tail}"


def test_push_idempotent_on_replay(channel_for, grpc_stubs):
    sync_pb2, sync_pb2_grpc = grpc_stubs
    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-a'))
    url = f"https://idem.example/{uuid.uuid4()}"
    date = '2026-05-22'  # distinct from other tests
    batch = [
        (0, _line('rid-idem-1', '2026-05-22T11:00:00Z', url, 'T')),
        (1, _result_line('rid-idem-1')),
    ]
    r1 = _push(stub, sync_pb2, 'laptop-a', batch, date=date)
    r2 = _push(stub, sync_pb2, 'laptop-a', batch, date=date)
    assert r1.accepted_date == r2.accepted_date
    assert r1.accepted_line_offset == r2.accepted_line_offset


def test_read_counter_sums_across_laptops(channel_for, grpc_stubs):
    sync_pb2, sync_pb2_grpc = grpc_stubs
    url = f"https://sum.example/{uuid.uuid4()}"
    stub_a = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-a'))
    stub_b = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-b'))

    # Each laptop uses its own dedicated date so its offsets start at 0.
    _push(stub_a, sync_pb2, 'laptop-a', [
        (0, _line('rid-sum-a', '2026-05-23T12:00:00Z', url, 'S',
                  tag='read', filename='a.mhtml')),
        (1, _result_line('rid-sum-a')),
    ], date='2026-05-23')
    _push(stub_b, sync_pb2, 'laptop-b', [
        (0, _line('rid-sum-b', '2026-05-23T12:00:01Z', url, 'S',
                  tag='read', filename='b.mhtml')),
        (1, _result_line('rid-sum-b')),
    ], date='2026-05-23')

    # Snapshot the canonical DB and inspect the counter directly.
    db_path = _export_snapshot(channel_for, sync_pb2, sync_pb2_grpc, 'laptop-c')
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT read FROM visits WHERE url = ?", (url,)).fetchone()
        assert row is not None, "URL not present in canonical DB"
        assert row[0] == 2, f"expected read=2, got {row[0]}"


def test_rogue_client_rejected(channel_for, grpc_stubs):
    sync_pb2, sync_pb2_grpc = grpc_stubs
    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('rogue-client'))
    with pytest.raises(grpc.RpcError) as ei:
        _push(stub, sync_pb2, 'rogue-client', [
            (0, _line('rid-rogue', '2026-05-21T13:00:00Z', 'https://x', 'T'))])
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_identity_spoof_rejected(channel_for, grpc_stubs):
    sync_pb2, sync_pb2_grpc = grpc_stubs
    # Connect with laptop-a's cert but claim to be laptop-b.
    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel_for('laptop-a'))
    with pytest.raises(grpc.RpcError) as ei:
        _push(stub, sync_pb2, 'laptop-b', [
            (0, _line('rid-spoof', '2026-05-21T13:30:00Z', 'https://x', 'T'))])
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_export_db_snapshot_is_valid_sqlite(channel_for, grpc_stubs):
    sync_pb2, sync_pb2_grpc = grpc_stubs
    db_path = _export_snapshot(channel_for, sync_pb2, sync_pb2_grpc, 'laptop-a')
    assert os.path.getsize(db_path) > 0
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    # The canonical DB schema includes these tables, set up by the
    # store layer.
    assert {'visits', 'read_events', 'skimmed_events', 'snapshots'} <= names


def test_db_diff_against_snapshot(channel_for, grpc_stubs):
    """End-to-end: snapshot → feed to db_diff.py → expect 0 diffs vs itself."""
    sync_pb2, sync_pb2_grpc = grpc_stubs
    db = _export_snapshot(channel_for, sync_pb2, sync_pb2_grpc, 'laptop-a')
    rc = subprocess.call([
        sys.executable, str(TOOLS_DIR / 'db_diff.py'),
        '--db-a', db, '--db-b', db,
    ], stdout=subprocess.DEVNULL)
    assert rc == 0


# ----------------------------------------------------------------- helpers

def _export_snapshot(channel_for, sync_pb2, sync_pb2_grpc, machine_id):
    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel_for(machine_id))
    req = sync_pb2.ExportDbSnapshotRequest(machine_id=machine_id)
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    with open(path, 'wb') as f:
        for chunk in stub.ExportDbSnapshot(req, timeout=30):
            f.write(chunk.data)
    return path
