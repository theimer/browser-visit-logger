"""
pytest configuration for browser-visit-logger tests.

Production-data safety net
--------------------------
Every BVL_* path is forced to a throwaway sandbox directory *before* any
production module is imported.  The native-host modules read their path
globals once at import time from ``os.environ.get('BVL_*', <~ default>)``
— so unless the environment already points elsewhere, those defaults
resolve to the developer's real home (``~/browser-visits.db`` etc.).

Well-written tests still isolate themselves per-case (tmp_path /
monkeypatch / unittest.mock).  This net only governs the *fallback*: if a
test ever forgets to override a path, it lands in the sandbox instead of
touching real user data.  `setdefault` is used so an intentionally
exported BVL_* value (e.g. a developer pointing a run somewhere specific)
still wins.

The sandbox lives under the OS temp dir and is intentionally not cleaned
up here — it only ever holds stray output from a mis-isolated test, and
keeping it aids debugging.  It is never `~`.
"""
import os
import sys
import tempfile
from pathlib import Path

# One sandbox root for the whole session.  Subdirs are pre-created for
# the dir-typed vars so code that appends without mkdir still works.
_SANDBOX = tempfile.mkdtemp(prefix='bvl-test-sandbox-')


def _sub(name: str) -> str:
    p = os.path.join(_SANDBOX, name)
    os.makedirs(p, exist_ok=True)
    return p


# Must all be set before `import host` / `import snapshot_mover` /
# `import sync_client` anywhere in the test session.
os.environ.setdefault('BVL_HOST_LOG', os.path.join(_SANDBOX, 'host.log'))
os.environ.setdefault('BVL_DB_FILE', os.path.join(_SANDBOX, 'browser-visits.db'))
os.environ.setdefault('BVL_LOG_DIR', _sub('logs'))
os.environ.setdefault('BVL_DOWNLOADS_SNAPSHOTS_DIR', _sub('downloads'))
os.environ.setdefault('BVL_ICLOUD_SNAPSHOTS_DIR', _sub('icloud'))
os.environ.setdefault('BVL_GDRIVE_SNAPSHOTS_DIR', _sub('gdrive'))
os.environ.setdefault('BVL_CONFIG_DIR', _sub('config'))
os.environ.setdefault('BVL_PEER_LOGS_DIR', _sub('peers'))

# Make `import host` work from any test file.
sys.path.insert(0, str(Path(__file__).parent.parent / 'native-host'))
# The shared sealing library (snapshot_mover) was extracted from native-host
# into its own top-level component; add it so the native-host tools and the
# VM sealer resolve `import snapshot_mover` in tests.
sys.path.insert(0, str(
    Path(__file__).resolve().parents[2] / 'browser-visit-snapshot-lib'))
