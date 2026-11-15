"""Unit tests for uninstall_laptop.py.

Every test runs with HOME pointed at tmp_path and a fake repo checkout
under tmp_path; ``_launchctl`` is replaced by a recorder so the real
launchctl is never invoked.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import uninstall_laptop as ul  # noqa: E402

LABEL_V = 'com.browser.visit.logger.snapshot_verifier'
LABEL_M = 'com.browser.visit.logger.snapshot_mover'


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(ul, '_repo_root', lambda: repo)
    calls = []
    monkeypatch.setattr(ul, '_launchctl', lambda a: calls.append(a) or 0)
    monkeypatch.setattr('builtins.input',
                        lambda *_: pytest.fail('unexpected prompt'))
    return types_ns(home=home, repo=repo, launchctl=calls)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def _touch(p, text='x'):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def make_tree(home, repo):
    """Fully installed laptop; returns dict of named paths."""
    lib = home / 'Library'
    t = {}
    t['plist_v'] = _touch(lib / 'LaunchAgents' / f'{LABEL_V}.plist')
    t['plist_m'] = _touch(lib / 'LaunchAgents' / f'{LABEL_M}.plist')
    app = lib / 'Application Support' / 'browser-visit-logger'
    t['host_app'] = _touch(app / 'BrowserVisitLoggerHost.app' / 'x')\
        .parent
    t['ver_app'] = _touch(app / 'BrowserVisitLoggerVerifier.app' / 'x').parent
    t['app_parent'] = app
    t['manifests'] = []
    for d in ul._manifest_dirs(home):
        t['manifests'].append(_touch(d / 'com.browser.visit.logger.json'))
    cfg = home / '.browser-visit-logger'
    t['cfg'] = cfg
    t['venv'] = _touch(cfg / 'sync-venv' / 'bin' / 'python').parent.parent
    t['lock'] = _touch(cfg / 'sync.lock')
    t['config'] = _touch(cfg / 'config.json')
    t['tls'] = _touch(cfg / 'tls' / 'client.key')
    t['db'] = [_touch(home / f'browser-visits.db{s}')
               for s in ('', '-wal', '-shm', '-journal')]
    t['logs'] = [_touch(home / 'browser-visits-host.log'),
                 _touch(home / 'browser-visits-verifier.log'),
                 _touch(home / 'browser-visits-2025-01-02.log')]
    t['other_log'] = _touch(home / 'browser-visits-notes.log')
    t['archive'] = _touch(home / 'Documents' / 'browser-visit-logger' / 'a.db')
    t['downloads'] = _touch(
        home / 'Downloads' / 'browser-visit-snapshots' / 's.db')
    bl = repo / 'browser-visit-logger'
    t['key'] = _touch(bl / 'native-host' / 'generated_key.pem')
    t['ext'] = _touch(bl / 'extension' / 'manifest.json')
    t['build'] = _touch(bl / 'swift' / '.build' / 'o').parent
    t['gen'] = _touch(repo / 'browser-visit-sync-server' / 'gen'
                      / 'syncpb_py' / 'a.py').parent
    return t


def snapshot(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        out[str(p)] = (p.is_symlink(), p.read_bytes() if p.is_file() else None)
    return out


def test_nothing_to_remove(env, capsys):
    assert ul.main(['--yes']) == 0
    assert 'nothing to remove' in capsys.readouterr().out
    assert env.launchctl == []


def test_default_run_keeps_data(env, capsys):
    t = make_tree(env.home, env.repo)
    assert ul.main(['--yes']) == 0
    for k in ('plist_v', 'plist_m', 'host_app', 'ver_app', 'venv', 'lock',
              'app_parent'):
        assert not os.path.lexists(t[k]), k
    for m in t['manifests']:
        assert not m.exists()
    keep = [t['cfg'], t['config'], t['tls'], t['key'], t['ext'], t['build'],
            t['gen'], t['archive'], t['downloads'], t['other_log'],
            *t['db'], *t['logs']]
    for k in keep:
        assert k.exists(), k
    assert [LABEL_V in a[1] for a in env.launchctl] == [True, False]
    assert env.launchctl[0][0] == 'bootout'
    out = capsys.readouterr().out
    assert 'chrome://extensions' in out and '--purge-data' in out


def test_purge_data(env):
    t = make_tree(env.home, env.repo)
    assert ul.main(['--yes', '--purge-data']) == 0
    for k in (t['cfg'], t['key'], *t['db'], *t['logs']):
        assert not k.exists(), k
    assert t['other_log'].exists()
    assert t['ext'].exists() and t['build'].exists()
    assert t['archive'].exists() and t['downloads'].exists()


def test_repo_artifacts(env):
    t = make_tree(env.home, env.repo)
    assert ul.main(['--yes', '--repo-artifacts']) == 0
    for k in (t['ext'], t['build'], t['gen']):
        assert not k.exists()
    assert t['key'].exists() and t['config'].exists()


def test_all_flags_never_touch_archives(env):
    t = make_tree(env.home, env.repo)
    assert ul.main(['--yes', '--purge-data', '--repo-artifacts']) == 0
    assert t['archive'].exists() and t['downloads'].exists()


def test_dry_run_changes_nothing(env, capsys):
    make_tree(env.home, env.repo)
    before = (snapshot(env.home), snapshot(env.repo))
    assert ul.main(['-n', '--purge-data', '--repo-artifacts']) == 0
    assert (snapshot(env.home), snapshot(env.repo)) == before
    assert env.launchctl == []
    out = capsys.readouterr().out
    assert 'would remove' in out and 'empty dir' in out


def test_partial_manifests(env):
    dirs = ul._manifest_dirs(env.home)
    present = [_touch(dirs[1] / 'com.browser.visit.logger.json'),
               _touch(dirs[4] / 'com.browser.visit.logger.json')]
    plan = ul.build_plan(env.home, env.repo, False, False)
    assert [i.path for i in plan] == present
    assert ul.main(['--yes']) == 0
    assert not any(p.exists() for p in present)


def test_symlinks_not_followed(env):
    target = env.home / 'target'
    _touch(target / 'keep.txt')
    app = env.home / 'Library/Application Support/browser-visit-logger'
    app.mkdir(parents=True)
    (app / 'BrowserVisitLoggerHost.app').symlink_to(target)
    cfg = env.home / '.browser-visit-logger'
    cfg.mkdir()
    (cfg / 'sync-venv').symlink_to(target)
    assert ul.main(['--yes']) == 0
    assert not os.path.lexists(app / 'BrowserVisitLoggerHost.app')
    assert (target / 'keep.txt').exists()


def test_nonempty_parents_kept_and_empty_removed(env):
    app = env.home / 'Library/Application Support/browser-visit-logger'
    _touch(app / 'BrowserVisitLoggerHost.app' / 'x')
    _touch(app / 'other.txt')
    cfg = env.home / '.browser-visit-logger'
    _touch(cfg / 'sync.lock')
    _touch(cfg / 'config.json')
    assert ul.main(['--yes']) == 0
    assert app.is_dir() and cfg.is_dir()
    (app / 'other.txt').unlink()
    (cfg / 'config.json').unlink()
    _touch(app / 'BrowserVisitLoggerVerifier.app' / 'x')
    _touch(cfg / 'sync.lock')
    assert ul.main(['--yes']) == 0
    assert not app.exists() and not cfg.exists()


def test_empty_after_rejects_symlink_and_missing(env):
    link = env.home / 'l'
    link.symlink_to(env.home)
    assert not ul._empty_after(link, [])
    assert not ul._empty_after(env.home / 'nope', [])


def test_prompt_yes(env, monkeypatch):
    t = make_tree(env.home, env.repo)
    monkeypatch.setattr('builtins.input', lambda *_: ' yes ')
    assert ul.main([]) == 0
    assert not t['host_app'].exists()


def test_prompt_declined(env, monkeypatch):
    t = make_tree(env.home, env.repo)
    monkeypatch.setattr('builtins.input', lambda *_: 'y')
    assert ul.main([]) == 1
    assert t['host_app'].exists() and env.launchctl == []


def test_prompt_eof(env, monkeypatch, capsys):
    t = make_tree(env.home, env.repo)

    def eof(*_):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert ul.main([]) == 2
    assert t['host_app'].exists()
    assert 'non-interactive' in capsys.readouterr().err


@pytest.mark.parametrize('home', ['/', 'relative/home'])
def test_bad_home(env, monkeypatch, home, capsys):
    monkeypatch.setenv('HOME', home)
    assert ul.main(['--yes']) == 2
    assert 'refusing' in capsys.readouterr().err


def test_removal_failure_continues(env, monkeypatch, capsys):
    t = make_tree(env.home, env.repo)

    def boom(path):
        raise OSError('nope')
    monkeypatch.setattr(ul.shutil, 'rmtree', boom)
    assert ul.main(['--yes']) == 1
    assert not t['plist_v'].exists()
    assert not t['manifests'][0].exists()
    assert t['host_app'].exists()
    err = capsys.readouterr().err
    assert 'could not remove' in err and 'failed' in err


def test_launchctl_wrapper(monkeypatch):
    class R:
        returncode = 3
    monkeypatch.undo()
    monkeypatch.setattr(ul.subprocess, 'run', lambda *a, **k: R())
    assert ul._launchctl(['x']) == 3
    R.returncode = 0
    assert ul._launchctl(['x']) == 0

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(ul.subprocess, 'run', missing)
    assert ul._launchctl(['x']) == 127


def test_real_repo_root(monkeypatch):
    monkeypatch.undo()
    assert ul._repo_root() == Path(ul.__file__).resolve().parent.parent


def test_bootout_targets_current_user_gui_domain(env):
    _touch(env.home / 'Library' / 'LaunchAgents' / f'{LABEL_V}.plist')
    assert ul.main(['--yes']) == 0
    assert env.launchctl == [['bootout', f'gui/{os.getuid()}/{LABEL_V}']]


def test_launchctl_failure_does_not_block_plist_removal(env, monkeypatch):
    plist = _touch(env.home / 'Library' / 'LaunchAgents' / f'{LABEL_M}.plist')
    monkeypatch.setattr(ul, '_launchctl', lambda a: 113)  # not loaded
    assert ul.main(['--yes']) == 0
    assert not os.path.lexists(plist)


def test_bvl_env_paths_are_ignored(env, monkeypatch, tmp_path):
    elsewhere = tmp_path / 'elsewhere'
    cfg = _touch(elsewhere / 'cfgdir' / 'config.json')
    db = _touch(elsewhere / 'custom.db')
    monkeypatch.setenv('BVL_CONFIG_DIR', str(cfg.parent))
    monkeypatch.setenv('BVL_DB_FILE', str(db))
    assert ul.main(['--yes', '--purge-data']) == 0
    assert cfg.exists() and db.exists()


@pytest.mark.parametrize('flag', ['--purge', '--purge-d', '--repo',
                                  '--repo-art', '--dry', '--ye'])
def test_abbreviated_flags_rejected(env, flag):
    make_tree(env.home, env.repo)
    before = (snapshot(env.home), snapshot(env.repo))
    with pytest.raises(SystemExit) as exc:
        ul.main([flag])
    assert exc.value.code == 2
    assert (snapshot(env.home), snapshot(env.repo)) == before
    assert env.launchctl == []


def test_parser_disallows_abbrev():
    parser = ul._build_parser()
    assert parser.allow_abbrev is False
    assert parser.parse_args(['--purge-data']).purge_data is True


def _dated_logs(home, days=('02', '03', '04')):
    return [_touch(home / f'browser-visits-2025-01-{d}.log') for d in days]


def test_dated_logs_collapsed_in_plan(env, capsys):
    _dated_logs(env.home)
    _touch(env.home / 'browser-visits-notes.log')
    assert ul.main(['-n', '--purge-data']) == 0
    out = capsys.readouterr().out
    assert '3 dated log files' in out
    assert 'browser-visits-YYYY-MM-DD.log' in out
    assert 'browser-visits-2025-' not in out
    plan = ul.build_plan(env.home, env.repo, True, False)
    assert sum(i.kind == 'dated_log' for i in plan) == 3


def test_dry_run_and_real_plan_agree(env, capsys):
    make_tree(env.home, env.repo)
    _dated_logs(env.home)
    plan = ul.build_plan(env.home, env.repo, True, True)
    ul._print_plan(plan, '')
    real = capsys.readouterr().out
    ul._print_plan(plan, 'would remove ')
    dry = capsys.readouterr().out
    assert dry.replace('would remove ', '') == real


def test_single_dated_log_singular(env, capsys):
    _dated_logs(env.home, days=('02',))
    assert ul.main(['-n', '--purge-data']) == 0
    out = capsys.readouterr().out
    assert '1 dated log file:' in out
    assert '1 dated log files' not in out


def test_each_dated_log_removed_individually(env, capsys):
    logs = _dated_logs(env.home)
    notes = _touch(env.home / 'browser-visits-notes.log')
    assert ul.main(['--yes', '--purge-data']) == 0
    out = capsys.readouterr().out
    assert not any(p.exists() for p in logs)
    assert notes.exists()
    assert sum(l.startswith('removed ') and 'browser-visits-2025-' in l
               for l in out.splitlines()) == 3
