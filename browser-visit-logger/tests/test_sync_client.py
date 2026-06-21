"""Unit tests for native-host/sync_client.py (multi-laptop sync subprocess).

The gRPC stubs are faked — no network, no generated protobuf code.
Module-level path globals are pointed at a temp dir per test.
"""
import importlib
import json
import os
import sqlite3
import sys
import types
import datetime
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.normpath(os.path.join(HERE, '..', 'native-host'))
if NATIVE not in sys.path:
    sys.path.insert(0, NATIVE)

import sync_client as sc  # noqa: E402


# --------------------------------------------------------------- fixtures

class Msg:
    """Generic stand-in for a protobuf message: stores kwargs as attrs."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSyncPb2:
    LogLine = Msg
    PushLogsRequest = Msg
    PeerCursor = Msg
    PullLogsRequest = Msg


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point all module globals at an isolated temp tree + fresh DB."""
    db = str(tmp_path / 'visits.db')
    log_dir = str(tmp_path / 'logs')
    peers = str(tmp_path / 'peers')
    cfg_dir = str(tmp_path / 'config')
    os.makedirs(log_dir)
    os.makedirs(cfg_dir)
    monkeypatch.setattr(sc, 'DB_FILE', db)
    monkeypatch.setattr(sc, 'LOG_DIR', log_dir)
    monkeypatch.setattr(sc, 'PEER_LOGS_DIR', peers)
    monkeypatch.setattr(sc, 'CONFIG_DIR', cfg_dir)
    monkeypatch.setattr(sc, 'CONFIG_FILE', os.path.join(cfg_dir, 'config.json'))
    monkeypatch.setattr(sc, 'LOCK_FILE', os.path.join(cfg_dir, 'sync.lock'))
    monkeypatch.setattr(sc, 'TLS_DIR', str(tmp_path / 'tls'))
    monkeypatch.setattr(sc, 'SYNC_INTERVAL_SECONDS', 60)
    # Clear env that would shadow config lookups.
    for k in ('BVL_MACHINE_ID', 'BVL_SYNC_SERVER'):
        monkeypatch.delenv(k, raising=False)
    return types.SimpleNamespace(
        db=db, log_dir=log_dir, peers=peers, cfg_dir=cfg_dir, tmp=tmp_path)


@pytest.fixture
def conn(env):
    c = sc.open_db()
    yield c
    c.close()


# --------------------------------------------------------------- config

def test_load_config_missing(env):
    assert sc.load_config() == {}


def test_load_config_present(env):
    with open(sc.CONFIG_FILE, 'w') as f:
        json.dump({'machine_id': 'm1', 'sync_server': 'h:1'}, f)
    assert sc.load_config()['machine_id'] == 'm1'


def test_machine_id_from_env(env, monkeypatch):
    monkeypatch.setenv('BVL_MACHINE_ID', 'envid')
    assert sc.machine_id_from_config() == 'envid'


def test_machine_id_from_config_file(env):
    with open(sc.CONFIG_FILE, 'w') as f:
        json.dump({'machine_id': 'cfgid'}, f)
    assert sc.machine_id_from_config() == 'cfgid'


def test_machine_id_none(env):
    assert sc.machine_id_from_config() is None


def test_server_endpoint_env(env, monkeypatch):
    monkeypatch.setenv('BVL_SYNC_SERVER', 'envhost:9')
    assert sc.server_endpoint() == 'envhost:9'


def test_server_endpoint_config(env):
    with open(sc.CONFIG_FILE, 'w') as f:
        json.dump({'sync_server': 'cfg:9'}, f)
    assert sc.server_endpoint() == 'cfg:9'


# --------------------------------------------------------------- db helpers

def test_open_db_creates_tables(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'sync_state', 'mover_errors'} <= names


def test_cursor_roundtrip(conn):
    assert sc.get_cursor(conn, 'm') == ('', -1)
    sc.set_cursor(conn, 'm', '2026-05-21', 7)
    assert sc.get_cursor(conn, 'm') == ('2026-05-21', 7)
    # Update existing row (ON CONFLICT path).
    sc.set_cursor(conn, 'm', '2026-05-22', 0)
    assert sc.get_cursor(conn, 'm') == ('2026-05-22', 0)


def test_mark_attempt_insert_and_update(conn):
    sc.mark_attempt(conn, 'm', error=None)
    row = conn.execute(
        "SELECT last_error FROM sync_state WHERE machine_id='m'").fetchone()
    assert row[0] == ''
    sc.mark_attempt(conn, 'm', error='boom')
    row = conn.execute(
        "SELECT last_error FROM sync_state WHERE machine_id='m'").fetchone()
    assert row[0] == 'boom'


def test_should_skip(conn):
    # No row → don't skip.
    assert sc.should_skip(conn, 'm') is False
    # Recent attempt → skip.
    sc.mark_attempt(conn, 'm', None)
    assert sc.should_skip(conn, 'm') is True


def test_should_skip_old(conn):
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(seconds=999)).isoformat()
    conn.execute(
        "INSERT INTO sync_state (machine_id, last_sync_attempt_at) VALUES ('m', ?)",
        (old,))
    assert sc.should_skip(conn, 'm') is False


def test_should_skip_empty_timestamp(conn):
    conn.execute(
        "INSERT INTO sync_state (machine_id, last_sync_attempt_at) VALUES ('m', '')")
    assert sc.should_skip(conn, 'm') is False


def test_should_skip_bad_timestamp(conn):
    conn.execute(
        "INSERT INTO sync_state (machine_id, last_sync_attempt_at) VALUES ('m', 'not-a-date')")
    assert sc.should_skip(conn, 'm') is False


def test_record_and_clear_error(conn):
    sc.record_error(conn, 'sync_push', 'h:1', 'msg1')
    row = conn.execute("SELECT attempts, immediate FROM mover_errors").fetchone()
    assert row == (1, 0)
    # Second occurrence increments attempts, raises immediate via MAX.
    sc.record_error(conn, 'sync_push', 'h:1', 'msg2', immediate=True)
    row = conn.execute("SELECT attempts, immediate, message FROM mover_errors").fetchone()
    assert row[0] == 2 and row[1] == 1 and row[2] == 'msg2'
    sc.clear_error(conn, 'sync_push', 'h:1')
    assert conn.execute("SELECT COUNT(*) FROM mover_errors").fetchone()[0] == 0


# --------------------------------------------------------------- logs

def test_list_local_log_dates(env):
    # No matching files initially.
    assert sc.list_local_log_dates() == []
    for name in ('browser-visits-2026-05-21.log',
                 'browser-visits-2026-05-20.log',
                 'unrelated.txt'):
        open(os.path.join(env.log_dir, name), 'w').close()
    assert sc.list_local_log_dates() == ['2026-05-20', '2026-05-21']


def test_list_local_log_dates_missing_dir(env, monkeypatch):
    monkeypatch.setattr(sc, 'LOG_DIR', str(env.tmp / 'nope'))
    assert sc.list_local_log_dates() == []


def test_read_log_lines_after(env):
    path = os.path.join(env.log_dir, 'browser-visits-2026-05-21.log')
    with open(path, 'w') as f:
        f.write('l0\nl1\nl2\n')
    got = list(sc.read_log_lines_after('2026-05-21', 0))
    assert got == [(1, 'l1'), (2, 'l2')]


def test_read_log_lines_after_missing(env):
    assert list(sc.read_log_lines_after('2026-05-21', -1)) == []


def test_append_peer_line(env):
    sc.append_peer_line('peerA', '2026-05-21', 'hello')
    path = os.path.join(env.peers, 'peerA', 'browser-visits-2026-05-21.log')
    with open(path) as f:
        assert f.read() == 'hello\n'


# --------------------------------------------------------------- replay

def _visits(conn):
    return conn.execute("SELECT url, read, skimmed, of_interest FROM visits").fetchall()


def test_replay_result_line_ignored(conn):
    sc.replay_into_db(conn, 'rid\tsuccess')
    assert _visits(conn) == []


def test_replay_too_few_fields(conn):
    sc.replay_into_db(conn, 'a\tb\tc')  # 3 fields
    assert _visits(conn) == []


def test_replay_basic(conn):
    sc.replay_into_db(conn, '\t'.join(['rid', 'ts', 'https://x', 'Title']))
    assert _visits(conn) == [('https://x', 0, 0, None)]


def test_replay_of_interest(conn):
    sc.replay_into_db(conn, '\t'.join(['rid', 'ts', 'https://x', 'T', 'of_interest']))
    assert _visits(conn)[0][3] == '1'


def test_replay_read_and_skimmed(conn):
    sc.replay_into_db(conn, '\t'.join(['rid', 't1', 'https://x', 'T', 'read', 'a.mhtml']))
    sc.replay_into_db(conn, '\t'.join(['rid', 't2', 'https://x', 'T', 'skimmed', 'b.mhtml']))
    row = conn.execute("SELECT read, skimmed FROM visits WHERE url='https://x'").fetchone()
    assert row == (1, 1)


def test_replay_five_fields_unknown_tag(conn):
    # 5 fields, tag != of_interest → visit inserted, no flag.
    sc.replay_into_db(conn, '\t'.join(['rid', 'ts', 'https://x', 'T', 'weird']))
    assert _visits(conn)[0][3] is None


# --------------------------------------------------------------- _is_auth_error

@pytest.mark.parametrize('msg,expected', [
    ('StatusCode.UNAUTHENTICATED', True),
    ('permission_denied: nope', True),
    ('bad certificate', True),
    ('connection refused', False),
])
def test_is_auth_error(msg, expected):
    assert sc._is_auth_error(Exception(msg)) is expected


def test_now_iso():
    assert sc._now_iso().endswith('+00:00')


# --------------------------------------------------------------- stubs / channel

def test_load_stubs_failure(env, monkeypatch):
    # Ensure the fake modules aren't importable → ImportError → RuntimeError.
    for m in ('sync_pb2', 'sync_pb2_grpc'):
        monkeypatch.setitem(sys.modules, m, None)
    with pytest.raises(RuntimeError, match='grpc stubs not available'):
        sc._load_stubs()


def test_load_stubs_success(env, monkeypatch):
    fake_grpc = types.ModuleType('grpc')
    monkeypatch.setitem(sys.modules, 'sync_pb2', FakeSyncPb2())
    monkeypatch.setitem(sys.modules, 'sync_pb2_grpc', types.ModuleType('sync_pb2_grpc'))
    monkeypatch.setitem(sys.modules, 'grpc', fake_grpc)
    pb2, pb2_grpc, grpc = sc._load_stubs()
    assert grpc is fake_grpc


def _fake_grpc(monkeypatch):
    fake = types.ModuleType('grpc')
    fake.insecure_channel = lambda ep: ('insecure', ep)
    fake.secure_channel = lambda ep, creds: ('secure', ep, creds)
    fake.ssl_channel_credentials = lambda **kw: ('creds', kw)
    return fake


def test_build_channel_insecure(env, monkeypatch):
    fake = _fake_grpc(monkeypatch)
    monkeypatch.setattr(sc, '_load_stubs', lambda: (FakeSyncPb2(), object(), fake))
    ch, pb2, pb2_grpc = sc.build_channel('h:1', insecure=True)
    assert ch == ('insecure', 'h:1')


def test_build_channel_secure(env, monkeypatch):
    fake = _fake_grpc(monkeypatch)
    monkeypatch.setattr(sc, '_load_stubs', lambda: (FakeSyncPb2(), object(), fake))
    os.makedirs(sc.TLS_DIR)
    for fn in ('client.crt', 'client.key', 'server-ca.crt'):
        with open(os.path.join(sc.TLS_DIR, fn), 'wb') as f:
            f.write(b'x')
    ch, _, _ = sc.build_channel('h:1', insecure=False)
    assert ch[0] == 'secure'


# --------------------------------------------------------------- push / pull

class FakeStub:
    def __init__(self, channel):
        self.pushes = []
        self.push_raises = False
        self.push_fail_after = None   # raise once this many pushes have succeeded
        self.pull_chunks = []
        self.pull_raises = None

    def PushLogs(self, req, timeout=None):
        if self.push_raises:
            raise RuntimeError('push boom')
        if self.push_fail_after is not None and len(self.pushes) >= self.push_fail_after:
            raise RuntimeError('push boom (later batch)')
        self.pushes.append(req)

    def PullLogs(self, req, timeout=None):
        if self.pull_raises:
            raise self.pull_raises
        return iter(self.pull_chunks)


class FakeStubFactory:
    def __init__(self, stub):
        self._stub = stub

    def BrowserVisitSyncStub(self, channel):
        return self._stub


def test_do_push_batches_and_sets_cursor(conn, env):
    # 501 lines forces one mid-loop flush (>=500) plus a final flush.
    path = os.path.join(env.log_dir, 'browser-visits-2026-05-21.log')
    with open(path, 'w') as f:
        for i in range(501):
            f.write(f'line{i}\n')
    stub = FakeStub(None)
    sc.do_push(conn, None, FakeSyncPb2(), FakeStubFactory(stub),
               'm', 'h:1')
    # Two PushLogs calls: 500 + 1.
    assert len(stub.pushes) == 2
    assert sc.get_cursor(conn, 'm') == ('2026-05-21', 500)


def test_do_push_with_existing_cursor(conn, env):
    path = os.path.join(env.log_dir, 'browser-visits-2026-05-21.log')
    with open(path, 'w') as f:
        f.write('l0\nl1\n')
    sc.set_cursor(conn, 'm', '2026-05-21', 0)  # already pushed l0
    stub = FakeStub(None)
    sc.do_push(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    # Only l1 sent.
    assert len(stub.pushes) == 1
    assert len(stub.pushes[0].lines) == 1
    assert sc.get_cursor(conn, 'm') == ('2026-05-21', 1)


def test_do_push_nothing_to_send(conn, env):
    # No logs → no push, cursor unchanged.
    stub = FakeStub(None)
    sc.do_push(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    assert stub.pushes == []


def test_send_push_records_error_and_raises(conn, env):
    path = os.path.join(env.log_dir, 'browser-visits-2026-05-21.log')
    with open(path, 'w') as f:
        f.write('l0\n')
    stub = FakeStub(None)
    stub.push_raises = True
    with pytest.raises(RuntimeError):
        sc.do_push(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    n = conn.execute(
        "SELECT COUNT(*) FROM mover_errors WHERE operation='sync_push'").fetchone()[0]
    assert n == 1


def test_send_push_clears_error_on_success(conn, env):
    sc.record_error(conn, 'sync_push', 'h:1', 'stale')
    path = os.path.join(env.log_dir, 'browser-visits-2026-05-21.log')
    with open(path, 'w') as f:
        f.write('l0\n')
    stub = FakeStub(None)
    sc.do_push(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    assert conn.execute("SELECT COUNT(*) FROM mover_errors").fetchone()[0] == 0


def test_do_pull_applies_chunks(conn, env):
    chunk = Msg(peer_machine_id='peerB', lines=[
        Msg(date='2026-05-21', line_offset=0,
            raw_line='\t'.join(['rid', 'ts', 'https://x', 'T'])),
        Msg(date='2026-05-21', line_offset=1,
            raw_line='\t'.join(['rid', 't2', 'https://x', 'T', 'read', 'a.mhtml'])),
    ])
    stub = FakeStub(None)
    stub.pull_chunks = [chunk]
    sc.do_pull(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    # Replayed into DB.
    row = conn.execute("SELECT read FROM visits WHERE url='https://x'").fetchone()
    assert row[0] == 1
    # Peer cursor advanced + mirror written.
    assert sc.get_cursor(conn, 'peerB') == ('2026-05-21', 1)
    mirror = os.path.join(env.peers, 'peerB', 'browser-visits-2026-05-21.log')
    assert os.path.exists(mirror)


def test_do_pull_replay_is_idempotent(conn, env):
    # Re-pulling the same lines (e.g. a re-pull after a crash before the peer
    # cursor was persisted) must NOT double-count the read counter.
    chunk = Msg(peer_machine_id='peerB', lines=[
        Msg(date='2026-05-21', line_offset=0,
            raw_line='\t'.join(['rid', 'ts', 'https://x', 'T'])),
        Msg(date='2026-05-21', line_offset=1,
            raw_line='\t'.join(['rid', 't2', 'https://x', 'T', 'read', 'a.mhtml'])),
    ])
    stub = FakeStub(None)
    stub.pull_chunks = [chunk]
    sc.do_pull(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    sc.do_pull(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    assert conn.execute(
        "SELECT read FROM visits WHERE url='https://x'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM read_events WHERE url='https://x'").fetchone()[0] == 1


def test_do_push_partial_failure_does_not_advance_cursor(conn, env):
    # 501 lines → two PushLogs (500 + 1). The first batch is accepted, the
    # second raises; the cursor must NOT advance, so the already-accepted
    # lines re-push next run (PushLogs is idempotent server-side).
    path = os.path.join(env.log_dir, 'browser-visits-2026-05-21.log')
    with open(path, 'w') as f:
        for i in range(501):
            f.write(f'line{i}\n')
    stub = FakeStub(None)
    stub.push_fail_after = 1            # first push ok, second raises
    with pytest.raises(RuntimeError):
        sc.do_push(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    assert len(stub.pushes) == 1                      # only the first batch sent
    assert sc.get_cursor(conn, 'm') == ('', -1)       # cursor untouched


def test_do_pull_sends_known_cursors(conn, env):
    sc.set_cursor(conn, 'peerB', '2026-05-21', 3)
    captured = {}
    stub = FakeStub(None)

    def pull(req, timeout=None):
        captured['cursors'] = req.cursors
        return iter([])
    stub.PullLogs = pull
    sc.do_pull(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    assert any(c.peer_machine_id == 'peerB' for c in captured['cursors'])


def test_do_pull_records_error(conn, env):
    stub = FakeStub(None)
    stub.pull_raises = RuntimeError('pull boom')
    with pytest.raises(RuntimeError):
        sc.do_pull(conn, None, FakeSyncPb2(), FakeStubFactory(stub), 'm', 'h:1')
    n = conn.execute(
        "SELECT COUNT(*) FROM mover_errors WHERE operation='sync_pull'").fetchone()[0]
    assert n == 1


# --------------------------------------------------------------- FileLock

def test_filelock_acquire_release(env):
    with sc.FileLock(sc.LOCK_FILE):
        pass  # acquired + released without error


def test_filelock_contended_exits(env):
    held = sc.FileLock(sc.LOCK_FILE)
    held.__enter__()
    try:
        with pytest.raises(SystemExit):
            with sc.FileLock(sc.LOCK_FILE):
                pass
    finally:
        held.__exit__()


def test_filelock_other_oserror_propagates(env, monkeypatch):
    import fcntl

    def boom(fd, op):
        raise OSError(99, 'unexpected')
    monkeypatch.setattr(fcntl, 'flock', boom)
    with pytest.raises(OSError):
        with sc.FileLock(sc.LOCK_FILE):
            pass


# --------------------------------------------------------------- main

def _patch_main_io(monkeypatch, stub, machine='m', endpoint='h:1'):
    monkeypatch.setattr(sc, 'machine_id_from_config', lambda: machine)
    monkeypatch.setattr(sc, 'server_endpoint', lambda: endpoint)
    monkeypatch.setattr(sc, 'build_channel',
                        lambda ep, insecure: (None, FakeSyncPb2(), FakeStubFactory(stub)))


def test_main_no_machine_id(env, monkeypatch):
    monkeypatch.setattr(sc, 'machine_id_from_config', lambda: None)
    assert sc.main([]) == 2


def test_main_no_endpoint(env, monkeypatch):
    monkeypatch.setattr(sc, 'machine_id_from_config', lambda: 'm')
    monkeypatch.setattr(sc, 'server_endpoint', lambda: None)
    assert sc.main([]) == 0


def test_main_skips_when_recent(env, monkeypatch):
    stub = FakeStub(None)
    _patch_main_io(monkeypatch, stub)
    # Pre-seed a recent attempt so should_skip returns True.
    c = sc.open_db()
    sc.mark_attempt(c, 'm', None)
    c.commit()
    c.close()
    assert sc.main([]) == 0
    assert stub.pushes == []  # never got to push


def test_main_success(env, monkeypatch):
    stub = FakeStub(None)
    _patch_main_io(monkeypatch, stub)
    assert sc.main(['--force']) == 0


def test_main_handshake_failure(env, monkeypatch):
    monkeypatch.setattr(sc, 'machine_id_from_config', lambda: 'm')
    monkeypatch.setattr(sc, 'server_endpoint', lambda: 'h:1')

    def boom(ep, insecure):
        raise RuntimeError('no route')
    monkeypatch.setattr(sc, 'build_channel', boom)
    assert sc.main(['--force']) == 1
    c = sc.open_db()
    n = c.execute(
        "SELECT COUNT(*) FROM mover_errors WHERE operation='sync_handshake'").fetchone()[0]
    c.close()
    assert n == 1


def test_main_sync_failure(env, monkeypatch):
    stub = FakeStub(None)
    stub.push_raises = True
    _patch_main_io(monkeypatch, stub)
    # Provide a local log so do_push attempts a send and fails.
    with open(os.path.join(env.log_dir, 'browser-visits-2026-05-21.log'), 'w') as f:
        f.write('l0\n')
    assert sc.main(['--force']) == 1


def test_main_server_arg_overrides(env, monkeypatch):
    stub = FakeStub(None)
    monkeypatch.setattr(sc, 'machine_id_from_config', lambda: 'm')
    monkeypatch.setattr(sc, 'server_endpoint', lambda: None)  # no config endpoint
    monkeypatch.setattr(sc, 'build_channel',
                        lambda ep, insecure: (None, FakeSyncPb2(), FakeStubFactory(stub)))
    # --server supplies the endpoint despite server_endpoint() → None.
    assert sc.main(['--force', '--server', 'cli:1']) == 0

