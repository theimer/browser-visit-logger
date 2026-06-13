#!/usr/bin/env python3
"""
fetch_vm_snapshot.py — Download a transactionally-consistent copy of
the VM's canonical browser-visits SQLite DB via the
`ExportDbSnapshot` gRPC RPC.

The result is a plain .db file ready to feed into ``db_diff.py``.

Usage
-----
    fetch_vm_snapshot.py \\
        --server bvl-vm.example.com:50443 \\
        --machine-id $(scutil --get LocalHostName) \\
        --client-cert ~/.browser-visit-logger/tls/client.crt \\
        --client-key  ~/.browser-visit-logger/tls/client.key \\
        --ca          ~/.browser-visit-logger/tls/server-ca.crt \\
        --out /tmp/vm-snapshot.db

Composed with db_diff.py for the common case:

    fetch_vm_snapshot.py --out /tmp/vm.db
    db_diff.py --db-a ~/browser-visits.db --db-b /tmp/vm.db --ignore-counters

Requires the generated Python grpc stubs to exist under
``browser-visit-sync-server/gen/syncpb_py/``.  Run
``protoc --python_out --grpc_python_out`` against ``sync.proto`` to
produce them; see the sync-server README.
"""

import argparse
import os
import sys

try:
    import grpc
except ImportError:  # pragma: no cover - import-time environment guard
    print("error: this tool requires `grpcio` (pip install grpcio)", file=sys.stderr)
    sys.exit(2)


def _load_stubs():
    here = os.path.dirname(os.path.abspath(__file__))
    gen = os.path.normpath(os.path.join(
        here, '..', 'browser-visit-sync-server', 'gen', 'syncpb_py'))
    if gen not in sys.path:
        sys.path.insert(0, gen)
    try:
        import sync_pb2          # noqa: F401
        import sync_pb2_grpc     # noqa: F401
    except ImportError as e:
        print(f"error: generated grpc stubs not found at {gen}: {e}",
              file=sys.stderr)
        print("Run `make proto-py` in browser-visit-sync-server/ first.",
              file=sys.stderr)
        sys.exit(2)
    return sys.modules['sync_pb2'], sys.modules['sync_pb2_grpc']


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument('--server', required=True,
                   help='host:port of the sync-server')
    p.add_argument('--machine-id', required=True,
                   help='this laptop\'s machine_id (must match client cert CN)')
    p.add_argument('--client-cert', required=True)
    p.add_argument('--client-key', required=True)
    p.add_argument('--ca', required=True,
                   help='PEM bundle that signed the server cert')
    p.add_argument('--out', required=True,
                   help='destination path for the downloaded snapshot')
    p.add_argument('--insecure', action='store_true',
                   help='skip TLS; for local development only')
    args = p.parse_args(argv)

    sync_pb2, sync_pb2_grpc = _load_stubs()

    if args.insecure:
        channel = grpc.insecure_channel(args.server)
    else:
        with open(args.client_cert, 'rb') as f: cert = f.read()
        with open(args.client_key, 'rb') as f: key = f.read()
        with open(args.ca, 'rb') as f: ca = f.read()
        creds = grpc.ssl_channel_credentials(
            root_certificates=ca, private_key=key, certificate_chain=cert)
        channel = grpc.secure_channel(args.server, creds)

    stub = sync_pb2_grpc.BrowserVisitSyncStub(channel)
    req = sync_pb2.ExportDbSnapshotRequest(machine_id=args.machine_id)
    written = 0
    with open(args.out, 'wb') as f:
        for chunk in stub.ExportDbSnapshot(req):
            f.write(chunk.data)
            written += len(chunk.data)
    print(f"wrote {written} bytes to {args.out}")
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
