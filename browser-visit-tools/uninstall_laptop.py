#!/usr/bin/env python3
"""uninstall_laptop.py — remove what ``browser-visit-logger/install.sh`` and
``install_laptop.sh`` put on this laptop.

Stdlib-only and independent of ``uninstall_aws.py`` (backend teardown is a
separate tool).  Removal is tiered:

  core            installer-made, regenerable software (always removed)
  --repo-artifacts build outputs generated inside the git checkout
  --purge-data    user data and identity: config, TLS keys, signing key,
                  the SQLite DB and logs

Never touched (no flag): the Google Drive / iCloud snapshot archive,
``~/Downloads/browser-visit-snapshots``, Chrome profile data, the loaded
extension and macOS privacy (TCC) grants.

Everything is rooted at ``Path.home()`` (honours ``HOME``); ``BVL_CONFIG_DIR``
and ``BVL_DB_FILE`` are deliberately ignored.  Symlinks are unlinked, never
followed.

Exit codes: 0 success / nothing to do, 1 a step failed or user declined,
2 usage error or non-interactive without --yes.
"""

import argparse
import os
import shutil
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

HOST_NAME = 'com.browser.visit.logger'
AGENT_LABELS = (
    'com.browser.visit.logger.snapshot_verifier',
    'com.browser.visit.logger.snapshot_mover',
)
APP_NAMES = ('BrowserVisitLoggerHost.app', 'BrowserVisitLoggerVerifier.app')

CORE, REPO, DATA = 'core', 'repo-artifacts', 'purge-data'
TIER_TITLES = {
    CORE: 'core (installed software)',
    REPO: 'repo build artifacts (--repo-artifacts)',
    DATA: 'user data (--purge-data)',
}

# kind: 'agent' = bootout + delete plist, 'path' = delete file/tree/symlink,
# 'rmdir' = remove directory (already known to be left empty),
# 'dated_log' = like 'path', but summarised as one line in the printed plan.
Item = namedtuple('Item', 'tier kind path')


def _repo_root():
    return Path(__file__).resolve().parent.parent


def _launchctl(args):
    """Run ``launchctl``; return its exit status (non-zero if unavailable)."""
    try:
        return subprocess.run(['launchctl', *args], capture_output=True,
                              check=False).returncode
    except OSError:
        return 127


def _exists(path):
    return os.path.lexists(path)


def _manifest_dirs(home):
    lib = home / 'Library' / 'Application Support'
    return [
        lib / 'Google' / 'Chrome' / 'NativeMessagingHosts',
        lib / 'Google' / 'Chrome Canary' / 'NativeMessagingHosts',
        lib / 'Chromium' / 'NativeMessagingHosts',
        home / '.config' / 'google-chrome' / 'NativeMessagingHosts',
        home / '.config' / 'chromium' / 'NativeMessagingHosts',
    ]


def _empty_after(directory, removing):
    """True if ``directory`` exists and only contains entries in ``removing``."""
    if not directory.is_dir() or directory.is_symlink():
        return False
    return {c for c in directory.iterdir()} <= set(removing)


def build_plan(home, repo_root, purge_data, repo_artifacts):
    """Items that currently exist, in execution order."""
    plan = []

    def add(tier, kind, path):
        if _exists(path):
            plan.append(Item(tier, kind, path))

    for label in AGENT_LABELS:
        add(CORE, 'agent', home / 'Library' / 'LaunchAgents' / f'{label}.plist')
    app_parent = home / 'Library' / 'Application Support' / 'browser-visit-logger'
    apps = [app_parent / n for n in APP_NAMES]
    for app in apps:
        add(CORE, 'path', app)
    for d in _manifest_dirs(home):
        add(CORE, 'path', d / f'{HOST_NAME}.json')
    cfg = home / '.browser-visit-logger'
    core_cfg = [cfg / 'sync-venv', cfg / 'sync.lock']
    for p in core_cfg:
        add(CORE, 'path', p)
    if _empty_after(app_parent, apps):
        plan.append(Item(CORE, 'rmdir', app_parent))
    if not purge_data and _empty_after(cfg, core_cfg):
        plan.append(Item(CORE, 'rmdir', cfg))

    if repo_artifacts:
        add(REPO, 'path', repo_root / 'browser-visit-logger' / 'extension'
            / 'manifest.json')
        add(REPO, 'path', repo_root / 'browser-visit-logger' / 'swift' / '.build')
        add(REPO, 'path', repo_root / 'browser-visit-sync-server' / 'gen'
            / 'syncpb_py')

    if purge_data:
        add(DATA, 'path', cfg)
        add(DATA, 'path', repo_root / 'browser-visit-logger' / 'native-host'
            / 'generated_key.pem')
        for suffix in ('', '-wal', '-shm', '-journal'):
            add(DATA, 'path', home / f'browser-visits.db{suffix}')
        add(DATA, 'path', home / 'browser-visits-host.log')
        add(DATA, 'path', home / 'browser-visits-verifier.log')
        for p in sorted(home.glob('browser-visits-????-??-??.log')):
            add(DATA, 'dated_log', p)
    return plan


def _remove_path(path):
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def _run_item(item):
    if item.kind == 'agent':
        _launchctl(['bootout', f'gui/{os.getuid()}/{item.path.stem}'])
        _remove_path(item.path)
    elif item.kind == 'rmdir':
        item.path.rmdir()
    else:
        _remove_path(item.path)


def execute(plan):
    """Run every item; a failure is reported and the rest still run.
    Returns the number of failures."""
    failures = 0
    for item in plan:
        try:
            _run_item(item)
        except OSError as e:
            failures += 1
            print(f"error: could not remove {item.path}: {e}", file=sys.stderr)
        else:
            print(f"removed {item.path}")
    return failures


def _print_plan(plan, prefix):
    for tier in (CORE, REPO, DATA):
        items = [i for i in plan if i.tier == tier]
        if items:
            print(f"{TIER_TITLES[tier]}:")
            for i in items:
                if i.kind != 'dated_log':
                    note = ' (empty dir)' if i.kind == 'rmdir' else ''
                    print(f"  {prefix}{i.path}{note}")
            dated = [i for i in items if i.kind == 'dated_log']
            if dated:
                pattern = dated[0].path.parent / 'browser-visits-YYYY-MM-DD.log'
                s = '' if len(dated) == 1 else 's'
                print(f"  {prefix}{len(dated)} dated log file{s}: {pattern}")


def _print_followups(purge_data):
    print("\nDo manually:")
    print("  - remove the extension at chrome://extensions")
    print("  - review Files & Folders / Full Disk Access grants for "
          "BrowserVisitLogger* in System Settings > Privacy & Security")
    print("  - the Google Drive / iCloud snapshot archive and "
          "~/Downloads/browser-visit-snapshots were NOT touched")
    if not purge_data:
        print("  - config, TLS keys, signing key, database and logs were kept; "
              "re-run with --purge-data to delete them")
    print("  - run uninstall_aws.py separately for backend cleanup "
          "(before --purge-data, which deletes vm.json)")


def _build_parser():
    p = argparse.ArgumentParser(
        prog='uninstall_laptop.py',
        description='Remove the browser-visit-logger laptop install.',
        allow_abbrev=False)
    p.add_argument('-n', '--dry-run', action='store_true',
                   help='show what would be removed; change nothing')
    p.add_argument('-y', '--yes', action='store_true',
                   help='skip the confirmation prompt')
    p.add_argument('--purge-data', action='store_true',
                   help='also delete config, TLS keys, signing key, DB, logs')
    p.add_argument('--repo-artifacts', action='store_true',
                   help='also delete generated build outputs in the checkout')
    return p


def _confirm():
    """True if the user typed ``yes``; raises EOFError if non-interactive."""
    return input("Type 'yes' to remove the items above: ").strip() == 'yes'


def main(argv):
    args = _build_parser().parse_args(argv)
    home = Path.home()
    if not home.is_absolute() or home == Path(home.anchor):
        print(f"error: refusing to run with HOME={home}", file=sys.stderr)
        return 2
    plan = build_plan(home, _repo_root(), args.purge_data,
                      args.repo_artifacts)
    if not plan:
        print("nothing to remove")
        return 0
    if args.dry_run:
        _print_plan(plan, 'would remove ')
        return 0
    _print_plan(plan, '')
    if not args.yes:
        try:
            confirmed = _confirm()
        except EOFError:
            print("error: non-interactive; pass --yes to proceed",
                  file=sys.stderr)
            return 2
        if not confirmed:
            print("aborted; nothing changed")
            return 1
    failures = execute(plan)
    _print_followups(args.purge_data)
    if failures:
        print(f"{failures} step(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
