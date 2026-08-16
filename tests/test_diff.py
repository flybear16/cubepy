"""P0.2 — schema diff: breaking-change detection + rename detection + CLI exit."""

import pytest

from cubepy.diff import diff_yaml_files, main


def write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


OLD = """
cubes:
  - name: orders
    sql: SELECT * FROM orders
    measures:
      - {name: revenue, sql: amount, type: sum}
      - {name: cnt, type: count}
    dimensions:
      - {name: status, sql: status, type: string}
    joins:
      users: {relationship: belongsTo, sql: "orders.user_id = users.id"}
"""

NEW = """
cubes:
  - name: orders
    sql: SELECT * FROM orders_v2
    measures:
      - {name: revenue, sql: amount, type: sum, status: deprecated}
      - {name: income, sql: amount, type: sum}
      - {name: avg_amt, sql: amount, type: avg}
    dimensions:
      - {name: state, sql: status, type: string}
    joins:
      users: {relationship: hasMany, sql: "orders.user_id = users.id"}
"""


@pytest.fixture
def paths(tmp_path):
    return write(tmp_path, "old.yml", OLD), write(tmp_path, "new.yml", NEW)


def test_breaking_changes_detected(paths):
    old, new = paths
    changes = diff_yaml_files(old, new)
    kinds = [(c.severity, c.kind) for c in changes]
    assert ("breaking", "cube-sql-changed") in kinds       # orders -> orders_v2
    assert ("warning", "member-renamed") in kinds          # cnt -> ? no; check below
    assert ("breaking", "join-changed") in kinds           # belongsTo -> hasMany
    assert ("info", "member-added") in kinds               # avg_amt added
    assert ("breaking", "member-removed") in kinds         # cnt removed (no twin)
    # status -> deprecated is warning
    assert ("warning", "status-changed") in kinds


def test_rename_detection(tmp_path):
    old = write(tmp_path, "a.yml", """
cubes:
  - name: t
    sql: SELECT * FROM t
    measures:
      - {name: gmv, sql: amt, type: sum}
""")
    new = write(tmp_path, "b.yml", """
cubes:
  - name: t
    sql: SELECT * FROM t
    measures:
      - {name: total_amt, sql: amt, type: sum}
""")
    changes = diff_yaml_files(old, new)
    renames = [c for c in changes if c.kind == "member-renamed"]
    assert len(renames) == 1 and "total_amt" in renames[0].detail
    assert not [c for c in changes if c.kind == "member-removed"]


def test_cli_check_exit_code(paths, capsys):
    old, new = paths
    assert main([old, new]) == 0            # report only
    assert main([old, new, "--check"]) == 1  # breaking -> CI fail
    out = capsys.readouterr().out
    assert "breaking" in out


def test_cli_clean_diff(tmp_path):
    old = write(tmp_path, "same.yml", OLD)
    new = write(tmp_path, "same2.yml", OLD)
    assert main([old, new, "--check"]) == 0
