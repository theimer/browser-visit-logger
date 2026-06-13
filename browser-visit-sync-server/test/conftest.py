"""Pytest fixtures for integration tests against a live sync-server.

Session-scoped fixtures bring the stack up once per `pytest` invocation:

  1. Compile the proto into Python stubs on the fly (so we don't have to
     check generated code in or require a separate codegen step on the
     host).
  2. Mint the cert hierarchy via test/gen-certs.sh.
  3. Seed the enrolled_machines.db (excluding 'rogue-client' so the
     "unenrolled rejection" test can use that cert).
  4. Start the docker-compose stack (assumed: caller has set up Docker).
  5. Wait until the server's listening socket accepts a TLS handshake.

After all tests, tear the stack down.
"""
import os
import shutil
import socket
import ssl
import subprocess
import sys
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE.parent          # browser-visit-sync-server/
CERTS = HERE / 'certs'
STATE = HERE / 'state'
PROTO = SERVER_DIR / 'proto' / 'sync.proto'

MACHINES = ['laptop-a', 'laptop-b', 'laptop-c']
ROGUE = 'rogue-client'            # minted but NOT enrolled


# ----------------------------------------------------------------- proto

@pytest.fixture(scope='session')
def grpc_stubs(tmp_path_factory):
    """Compile sync.proto into a session-scoped temp dir and import it.

    Returns (sync_pb2, sync_pb2_grpc).
    """
    out = tmp_path_factory.mktemp('syncpb')
    from grpc_tools import protoc
    rc = protoc.main([
        'protoc',
        f'-I{PROTO.parent}',
        f'--python_out={out}',
        f'--grpc_python_out={out}',
        str(PROTO),
    ])
    if rc != 0:
        pytest.fail(f"protoc failed (rc={rc})")
    sys.path.insert(0, str(out))
    import importlib
    sync_pb2 = importlib.import_module('sync_pb2')
    sync_pb2_grpc = importlib.import_module('sync_pb2_grpc')
    return sync_pb2, sync_pb2_grpc


# ----------------------------------------------------------------- certs

@pytest.fixture(scope='session')
def certs_dir():
    # When reusing an existing server, the on-disk certs were minted
    # by the original session and the running server has the matching
    # CA loaded.  Regenerating now would break TLS — leave them be.
    if os.environ.get('BVL_TEST_USE_EXISTING') == '1':
        if not CERTS.exists():
            pytest.fail(f"BVL_TEST_USE_EXISTING=1 but {CERTS} doesn't exist")
        return CERTS
    subprocess.check_call(
        ['bash', str(HERE / 'gen-certs.sh'), *MACHINES, ROGUE])
    return CERTS


# ----------------------------------------------------------------- enrolled DB

@pytest.fixture(scope='session')
def enrolled_db(certs_dir):
    STATE.mkdir(exist_ok=True)
    db = STATE / 'enrolled_machines.db'
    # Same reasoning as certs_dir: the running server has already
    # opened this DB, and the fingerprints inside have to match its
    # CA.  Don't touch.
    if os.environ.get('BVL_TEST_USE_EXISTING') == '1':
        if not db.exists():
            pytest.fail(f"BVL_TEST_USE_EXISTING=1 but {db} doesn't exist")
        return db
    if db.exists():
        db.unlink()
    subprocess.check_call([
        sys.executable, str(HERE / 'seed-enrolled.py'),
        '--certs-dir', str(certs_dir),
        '--db', str(db),
        '--exclude', ROGUE,
    ])
    return db


# ----------------------------------------------------------------- server

@pytest.fixture(scope='session')
def server(certs_dir, enrolled_db):
    """Bring up the dockerised sync-server for the test session.

    Two env-var knobs control lifecycle:

    BVL_TEST_USE_EXISTING=1
        Don't touch docker at all.  Assume a server is already
        running on localhost:50443 and just verify it's reachable.
        Use this when you've left a stack running (with
        BVL_TEST_KEEP_RUNNING=1 previously) and want to fire
        additional tests against it from a second terminal without
        tearing down the one a `logs -f` is following.

    BVL_TEST_KEEP_RUNNING=1
        Bring the stack up normally, but skip the teardown at
        session end so you can tail logs / poke at the server
        afterwards.

    With neither set, the fixture is fully self-contained: down,
    build, up, run tests, down.
    """
    compose = ['docker', 'compose', '-f', str(SERVER_DIR / 'docker-compose.test.yml')]
    use_existing = os.environ.get('BVL_TEST_USE_EXISTING') == '1'

    if not use_existing:
        # Always start clean.
        subprocess.run(compose + ['down', '-v'], cwd=SERVER_DIR,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(compose + ['build'], cwd=SERVER_DIR)
        subprocess.check_call(compose + ['up', '-d'], cwd=SERVER_DIR)

    try:
        _wait_for_tls('127.0.0.1', 50443, certs_dir, timeout=60)
        yield {'addr': '127.0.0.1:50443', 'certs': certs_dir}
    finally:
        # Never tear down a stack we didn't bring up; never tear
        # down when the user explicitly asked us to keep it.
        if not use_existing and os.environ.get('BVL_TEST_KEEP_RUNNING') != '1':
            subprocess.run(compose + ['down', '-v'], cwd=SERVER_DIR,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for_tls(host, port, certs, timeout):
    deadline = time.time() + timeout
    ctx = ssl.create_default_context(cafile=str(certs / 'ca.crt'))
    ctx.load_cert_chain(certfile=str(certs / 'laptop-a.crt'),
                        keyfile=str(certs / 'laptop-a.key'))
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2) as sock:
                with ctx.wrap_socket(sock, server_hostname='localhost'):
                    return
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"server didn't come up on {host}:{port}: {last_err}")


# ----------------------------------------------------------------- per-test channel

@pytest.fixture
def channel_for(server, grpc_stubs):
    """Returns a factory: channel_for('laptop-a') → secure gRPC channel."""
    import grpc
    certs = server['certs']
    addr = server['addr']

    created = []
    def factory(machine_id):
        with open(certs / 'ca.crt', 'rb') as f: ca = f.read()
        with open(certs / f'{machine_id}.crt', 'rb') as f: cert = f.read()
        with open(certs / f'{machine_id}.key', 'rb') as f: key = f.read()
        creds = grpc.ssl_channel_credentials(
            root_certificates=ca, private_key=key, certificate_chain=cert)
        # Override authority to match SAN on server cert.
        ch = grpc.secure_channel(
            addr, creds,
            options=[('grpc.ssl_target_name_override', 'localhost')])
        created.append(ch)
        return ch

    yield factory

    for ch in created:
        ch.close()
