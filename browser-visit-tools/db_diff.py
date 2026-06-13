#!/usr/bin/env python3
"""
db_diff.py — Compare two browser-visits SQLite DB files.

Useful for verifying that a laptop has converged with the EC2 VM, for
debugging sync regressions, and for offline forensics.

The tool opens both files read-only via ATTACH DATABASE and reports,
per table:

  1. *Presence* — primary keys in A missing from B, and vice versa.
  2. *Value differences* — rows with matching PKs but differing
     non-PK columns.

`visits.read` and `visits.skimmed` are counters that legitimately
diverge between sync windows.  Pass ``--ignore-counters`` to suppress
those columns in the value-diff stage.

Tables compared by default: visits, read_events, skimmed_events,
snapshots.  `sync_state` and `mover_errors` are excluded because both
are intrinsically per-machine.

Usage
-----
    db_diff.py --db-a ~/browser-visits.db --db-b /tmp/vm.db
    db_diff.py --db-a a.db --db-b b.db --ignore-counters --sample 10
    db_diff.py --db-a a.db --db-b b.db --format json

Exit codes
----------
    0   no differences
    1   differences found
    2   tool error (file not found, schema mismatch, ...)
"""

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Tuple


DEFAULT_TABLES = ['visits', 'read_events', 'skimmed_events', 'snapshots']
COUNTER_COLUMNS = {'visits': ('read', 'skimmed')}


def _pk_columns(conn: sqlite3.Connection, db_alias: str, table: str) -> List[str]:
    rows = list(conn.execute(f"PRAGMA {db_alias}.table_info({table})"))
    pk_rows = sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])
    return [r[1] for r in pk_rows]


def _all_columns(conn: sqlite3.Connection, db_alias: str, table: str) -> List[str]:
    rows = list(conn.execute(f"PRAGMA {db_alias}.table_info({table})"))
    return [r[1] for r in rows]


def _table_exists(conn: sqlite3.Connection, db_alias: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {db_alias}.sqlite_master "
        f"WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _presence_diff(conn: sqlite3.Connection, table: str, pk: List[str],
                   sample: int) -> Tuple[int, int, List[Tuple], List[Tuple]]:
    """Return (a_only_count, b_only_count, a_only_sample, b_only_sample)."""
    cols = ', '.join(pk)
    a_only_sql = (
        f"SELECT {cols} FROM a.{table} "
        f"EXCEPT SELECT {cols} FROM b.{table}")
    b_only_sql = (
        f"SELECT {cols} FROM b.{table} "
        f"EXCEPT SELECT {cols} FROM a.{table}")
    a_only = list(conn.execute(a_only_sql))
    b_only = list(conn.execute(b_only_sql))
    return len(a_only), len(b_only), a_only[:sample], b_only[:sample]


def _value_diff(conn: sqlite3.Connection, table: str, pk: List[str],
                cols: List[str], ignore_cols: List[str],
                sample: int) -> Tuple[int, List[Dict[str, Any]]]:
    non_pk = [c for c in cols if c not in pk and c not in ignore_cols]
    if not non_pk:
        return 0, []
    pk_join = ' AND '.join(f"a.{table}.{c} = b.{table}.{c}" for c in pk)
    diff_predicates = ' OR '.join(
        f"COALESCE(a.{table}.{c}, '') != COALESCE(b.{table}.{c}, '')"
        for c in non_pk)
    select_cols = (
        [f"a.{table}.{c} AS pk_{c}" for c in pk]
        + [f"a.{table}.{c} AS a_{c}" for c in non_pk]
        + [f"b.{table}.{c} AS b_{c}" for c in non_pk]
    )
    sql = (
        f"SELECT {', '.join(select_cols)} "
        f"FROM a.{table} INNER JOIN b.{table} ON {pk_join} "
        f"WHERE {diff_predicates}")
    count = 0
    samples: List[Dict[str, Any]] = []
    for row in conn.execute(sql):
        count += 1
        if len(samples) < sample:
            samples.append(dict(zip([d[0] for d in conn.execute(sql).description], row)))
    return count, samples


def diff(db_a: str, db_b: str, tables: List[str], ignore_counters: bool,
         sample: int) -> Dict[str, Any]:
    if not os.path.isfile(db_a):
        raise FileNotFoundError(db_a)
    if not os.path.isfile(db_b):
        raise FileNotFoundError(db_b)
    conn = sqlite3.connect(':memory:')
    conn.execute("ATTACH DATABASE ? AS a", (f"file:{db_a}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS b", (f"file:{db_b}?mode=ro",))
    # PySQLite ignores mode=ro on attached files in some versions; we
    # never write so the distinction is academic.

    report: Dict[str, Any] = {'db_a': db_a, 'db_b': db_b, 'tables': {}}
    any_diff = False

    for t in tables:
        if not (_table_exists(conn, 'a', t) and _table_exists(conn, 'b', t)):
            report['tables'][t] = {'error': 'missing in one or both DBs'}
            any_diff = True
            continue
        cols_a = _all_columns(conn, 'a', t)
        cols_b = _all_columns(conn, 'b', t)
        if cols_a != cols_b:
            report['tables'][t] = {
                'error': 'column lists differ',
                'a_columns': cols_a, 'b_columns': cols_b,
            }
            any_diff = True
            continue
        pk = _pk_columns(conn, 'a', t)
        if not pk:
            pk = cols_a  # treat the whole row as the key
        ignore = list(COUNTER_COLUMNS.get(t, ())) if ignore_counters else []
        a_only_n, b_only_n, a_only_s, b_only_s = _presence_diff(
            conn, t, pk, sample)
        val_n, val_s = _value_diff(conn, t, pk, cols_a, ignore, sample)
        report['tables'][t] = {
            'a_only_count': a_only_n,
            'b_only_count': b_only_n,
            'value_diff_count': val_n,
            'a_only_sample': [list(r) for r in a_only_s],
            'b_only_sample': [list(r) for r in b_only_s],
            'value_diff_sample': val_s,
            'pk': pk,
            'ignored_columns': ignore,
        }
        if a_only_n or b_only_n or val_n:
            any_diff = True

    report['any_difference'] = any_diff
    return report


def _format_text(report: Dict[str, Any]) -> str:
    lines = [f"diff: {report['db_a']}  vs  {report['db_b']}"]
    for tname, t in report['tables'].items():
        if 'error' in t:
            lines.append(f"  {tname}: ERROR — {t['error']}")
            continue
        lines.append(
            f"  {tname}: "
            f"{t['a_only_count']} in A only, "
            f"{t['b_only_count']} in B only, "
            f"{t['value_diff_count']} value differences"
            + (f" (ignoring {','.join(t['ignored_columns'])})" if t['ignored_columns'] else ''))
        for r in t['a_only_sample']:
            lines.append(f"      A-only: {r}")
        for r in t['b_only_sample']:
            lines.append(f"      B-only: {r}")
        for r in t['value_diff_sample']:
            lines.append(f"      diff:   {r}")
    lines.append(f"any_difference: {report['any_difference']}")
    return '\n'.join(lines)


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--db-a', required=True)
    p.add_argument('--db-b', required=True)
    p.add_argument('--tables', default=','.join(DEFAULT_TABLES))
    p.add_argument('--ignore-counters', action='store_true')
    p.add_argument('--sample', type=int, default=5)
    p.add_argument('--format', choices=['text', 'json'], default='text')
    args = p.parse_args(argv)

    try:
        report = diff(args.db_a, args.db_b,
                      [t.strip() for t in args.tables.split(',') if t.strip()],
                      args.ignore_counters, args.sample)
    except FileNotFoundError as e:
        print(f"error: db file not found: {e}", file=sys.stderr)
        return 2

    if args.format == 'json':
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_format_text(report))
    return 1 if report['any_difference'] else 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
