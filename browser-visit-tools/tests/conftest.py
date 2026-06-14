"""pytest config for browser-visit-tools.

Adds the parent directory to sys.path so `import reading_list` works
from any test file.  Mirrors the pattern used by browser-visit-logger,
but stays self-contained — no cross-project imports.

Also pins the local timezone to UTC for the test session so
``_format_timestamp`` (which converts stored UTC into local time)
produces deterministic output.  Tests that need to verify the local
conversion actually happens override TZ for their own scope.

Production-data safety net: the only tool with a ~-rooted default is
reading_list (BVL_DB_FILE read default, BVL_OUTPUT_DIR write default);
both are forced to a throwaway sandbox before import so a mis-isolated
test can never read or overwrite real user data.  `setdefault` lets an
intentionally exported value still win.  The other tools (db_diff,
enroll_machine, migrate, fetch_vm_snapshot) take all paths as required
args, so they have no ~ default to guard.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_SANDBOX = tempfile.mkdtemp(prefix='bvl-tools-test-sandbox-')
os.environ.setdefault('BVL_DB_FILE', os.path.join(_SANDBOX, 'browser-visits.db'))
os.environ.setdefault('BVL_OUTPUT_DIR', _SANDBOX)
# manage_vm / manage_sync_server persist VM state to ~/.browser-visit-logger/
# vm.json by default; pin it into the sandbox so tests never touch the real
# file.  Individual tests override BVL_VM_STATE_FILE for their own scope.
os.environ.setdefault('BVL_VM_STATE_FILE', os.path.join(_SANDBOX, 'vm.json'))

os.environ['TZ'] = 'UTC'
time.tzset()
