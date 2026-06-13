"""Unit tests for fetch_vm_snapshot.py.

grpc and the generated stubs aren't installed in the test venv, so we
inject fakes into sys.modules before importing the module under test.
"""
import sys
import types
from pathlib import Path

import pytest

# Inject a fake `grpc` BEFORE importing the module (its top-level
# `import grpc` would otherwise sys.exit(2)).
_fake_grpc = types.ModuleType('grpc')
_fake_grpc.insecure_channel = lambda ep: ('insecure', ep)
_fake_grpc.secure_channel = lambda ep, creds: ('secure', ep, creds)
_fake_grpc.ssl_channel_credentials = lambda **kw: ('creds', kw)
sys.modules.setdefault('grpc', _fake_grpc)

sys.path.insert(0, str(Path(__file__).parent.parent))
import fetch_vm_snapshot as fv  # noqa: E402


class Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeChunk:
    def __init__(self, data):
        self.data = data


class FakeStub:
    def __init__(self, channel):
        pass

    def ExportDbSnapshot(self, req):
        return iter([FakeChunk(b'abc'), FakeChunk(b'de')])


def _inject_stubs(monkeypatch):
    pb2 = types.ModuleType('sync_pb2')
    pb2.ExportDbSnapshotRequest = Msg
    pb2_grpc = types.ModuleType('sync_pb2_grpc')
    pb2_grpc.BrowserVisitSyncStub = FakeStub
    monkeypatch.setitem(sys.modules, 'sync_pb2', pb2)
    monkeypatch.setitem(sys.modules, 'sync_pb2_grpc', pb2_grpc)


def test_load_stubs_success(monkeypatch):
    _inject_stubs(monkeypatch)
    pb2, pb2_grpc = fv._load_stubs()
    # _load_stubs must return the actual imported modules (identity),
    # not just something attr-shaped — proves it resolved + returned them.
    assert pb2 is sys.modules['sync_pb2']
    assert pb2_grpc is sys.modules['sync_pb2_grpc']


def test_load_stubs_failure(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, 'sync_pb2', None)
    monkeypatch.setitem(sys.modules, 'sync_pb2_grpc', None)
    with pytest.raises(SystemExit) as ei:
        fv._load_stubs()
    assert ei.value.code == 2
    assert 'stubs not found' in capsys.readouterr().err


def test_main_insecure(tmp_path, monkeypatch, capsys):
    _inject_stubs(monkeypatch)
    out = tmp_path / 'snap.db'
    rc = fv.main([
        '--server', 'localhost:50051', '--machine-id', 'laptop-a',
        '--client-cert', '/dev/null', '--client-key', '/dev/null',
        '--ca', '/dev/null', '--out', str(out), '--insecure',
    ])
    assert rc == 0
    assert out.read_bytes() == b'abcde'
    assert 'wrote 5 bytes' in capsys.readouterr().out


def test_main_secure(tmp_path, monkeypatch):
    _inject_stubs(monkeypatch)
    # Real cert/key/ca files (contents irrelevant — fake grpc ignores them).
    for fn in ('c.crt', 'c.key', 'ca.crt'):
        (tmp_path / fn).write_bytes(b'x')
    out = tmp_path / 'snap.db'
    rc = fv.main([
        '--server', 'vm:50443', '--machine-id', 'laptop-a',
        '--client-cert', str(tmp_path / 'c.crt'),
        '--client-key', str(tmp_path / 'c.key'),
        '--ca', str(tmp_path / 'ca.crt'),
        '--out', str(out),
    ])
    assert rc == 0
    assert out.read_bytes() == b'abcde'
