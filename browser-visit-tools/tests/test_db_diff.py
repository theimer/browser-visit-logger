"""Unit tests for db_diff.py."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import db_diff  # noqa: E402


SCHEMA = """
CREATE TABLE visits (url TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', of_interest TEXT,
    read INTEGER NOT NULL DEFAULT 0, skimmed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE read_events (url TEXT NOT NULL, timestamp TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '', directory TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (url, timestamp));
CREATE TABLE skimmed_events (url TEXT NOT NULL, timestamp TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '', directory TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (url, timestamp));
CREATE TABLE snapshots (date TEXT PRIMARY KEY, sealed INTEGER NOT NULL DEFAULT 0);
"""


def _mkdb(path, visits):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for v in visits:
        c.execute("INSERT INTO visits (url, timestamp, title, read) VALUES (?,?,?,?)", v)
    c.commit()
    c.close()


def test_identical_dbs_no_diff(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    rows = [('u1', 't', 'T', 1)]
    _mkdb(a, rows)
    _mkdb(b, rows)
    rep = db_diff.diff(a, b, db_diff.DEFAULT_TABLES, False, 5)
    assert rep['any_difference'] is False
    for t in db_diff.DEFAULT_TABLES:
        assert rep['tables'][t]['a_only_count'] == 0
        assert rep['tables'][t]['b_only_count'] == 0
        assert rep['tables'][t]['value_diff_count'] == 0


def test_presence_diff(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('u1', 't', 'T', 0), ('only-a', 't', 'T', 0)])
    _mkdb(b, [('u1', 't', 'T', 0), ('only-b', 't', 'T', 0)])
    rep = db_diff.diff(a, b, ['visits'], False, 5)
    v = rep['tables']['visits']
    assert v['a_only_count'] == 1 and v['b_only_count'] == 1
    assert rep['any_difference'] is True


def test_value_diff_and_ignore_counters(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('u1', 't', 'TitleA', 1)])
    _mkdb(b, [('u1', 't', 'TitleB', 9)])  # title + read differ
    # With counters counted, both title and read differ → 1 diff row.
    rep = db_diff.diff(a, b, ['visits'], False, 5)
    assert rep['tables']['visits']['value_diff_count'] == 1
    # Ignoring counters, only title differs → still 1 row.
    rep2 = db_diff.diff(a, b, ['visits'], True, 5)
    assert rep2['tables']['visits']['value_diff_count'] == 1
    assert 'read' in rep2['tables']['visits']['ignored_columns']


def test_value_diff_only_counter_difference_suppressed(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('u1', 't', 'Same', 1)])
    _mkdb(b, [('u1', 't', 'Same', 5)])  # only read differs
    rep = db_diff.diff(a, b, ['visits'], True, 5)
    assert rep['tables']['visits']['value_diff_count'] == 0
    assert rep['any_difference'] is False


def test_missing_table(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [])
    _mkdb(b, [])
    rep = db_diff.diff(a, b, ['nonexistent'], False, 5)
    assert 'error' in rep['tables']['nonexistent']
    assert rep['any_difference'] is True


def test_column_mismatch(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [])
    c = sqlite3.connect(b)
    c.execute("CREATE TABLE visits (url TEXT PRIMARY KEY, extra TEXT)")
    c.commit()
    c.close()
    rep = db_diff.diff(a, b, ['visits'], False, 5)
    assert rep['tables']['visits']['error'] == 'column lists differ'


def test_no_primary_key_uses_whole_row(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    for p in (a, b):
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE t (x TEXT, y TEXT)")
        c.execute("INSERT INTO t VALUES ('1','2')")
        c.commit()
        c.close()
    rep = db_diff.diff(a, b, ['t'], False, 5)
    assert rep['tables']['t']['pk'] == ['x', 'y']
    assert rep['any_difference'] is False


def test_diff_missing_file_raises(tmp_path):
    a = str(tmp_path / 'a.db')
    _mkdb(a, [])
    with pytest.raises(FileNotFoundError):
        db_diff.diff(a, str(tmp_path / 'nope.db'), ['visits'], False, 5)
    with pytest.raises(FileNotFoundError):
        db_diff.diff(str(tmp_path / 'nope.db'), a, ['visits'], False, 5)


def test_format_text(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('only-a', 't', 'T', 0)])
    _mkdb(b, [])
    rep = db_diff.diff(a, b, ['visits', 'nonexistent'], True, 5)
    text = db_diff._format_text(rep)
    assert 'visits:' in text
    assert 'ERROR' in text  # the nonexistent table
    assert 'any_difference: True' in text


def test_format_text_b_only_and_value_samples(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    # b has an extra row (b-only) and a differing value row.
    _mkdb(a, [('shared', 't', 'TitleA', 0)])
    _mkdb(b, [('shared', 't', 'TitleB', 0), ('only-b', 't', 'T', 0)])
    rep = db_diff.diff(a, b, ['visits'], False, 5)
    text = db_diff._format_text(rep)
    assert 'B-only:' in text
    assert 'diff:' in text


# ---- main / CLI ----

def test_main_no_diff_exit_0(tmp_path, capsys):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('u', 't', 'T', 0)])
    _mkdb(b, [('u', 't', 'T', 0)])
    rc = db_diff.main(['--db-a', a, '--db-b', b])
    assert rc == 0


def test_main_diff_exit_1(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('only-a', 't', 'T', 0)])
    _mkdb(b, [])
    rc = db_diff.main(['--db-a', a, '--db-b', b])
    assert rc == 1


def test_main_json_format(tmp_path, capsys):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('u', 't', 'T', 0)])
    _mkdb(b, [('u', 't', 'T', 0)])
    db_diff.main(['--db-a', a, '--db-b', b, '--format', 'json'])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed['any_difference'] is False


def test_main_missing_file_exit_2(tmp_path, capsys):
    a = str(tmp_path / 'a.db')
    _mkdb(a, [])
    rc = db_diff.main(['--db-a', a, '--db-b', str(tmp_path / 'nope.db')])
    assert rc == 2
    assert 'not found' in capsys.readouterr().err


def test_main_custom_tables_and_sample(tmp_path):
    a, b = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _mkdb(a, [('u%d' % i, 't', 'T', 0) for i in range(10)])
    _mkdb(b, [])
    rc = db_diff.main(['--db-a', a, '--db-b', b, '--tables', 'visits', '--sample', '3'])
    assert rc == 1
