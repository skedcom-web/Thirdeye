from pathlib import Path

import pytest

from goengine.turso_db import TursoCursor, TursoRow, _split_statements, _strip_line_comments


def test_strip_line_comments_removes_trailing_and_full_line_comments():
    text = "CREATE TABLE x (\n  a INT -- inline comment\n);\n-- full line comment\nSELECT 1;"
    stripped = _strip_line_comments(text)
    assert "comment" not in stripped
    assert "CREATE TABLE x" in stripped
    assert "SELECT 1" in stripped


def test_split_statements_handles_semicolon_inside_comment():
    # Real bug: a semicolon in comment prose used to split one CREATE TABLE
    # into two broken fragments.
    text = (
        "CREATE TABLE x (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    -- some prose; with a semicolon in it\n"
        "    name TEXT NOT NULL\n"
        ");"
    )
    stmts = _split_statements(text)
    assert len(stmts) == 1
    assert stmts[0].strip().startswith("CREATE TABLE x")
    assert stmts[0].strip().endswith(")")
    assert "name TEXT NOT NULL" in stmts[0]


def test_split_statements_handles_semicolon_inside_trigger_string_literal():
    # Real bug: "append-only" contains "END" as a raw substring, and a
    # semicolon inside the RAISE() string literal used to be treated as a
    # top-level statement boundary, splitting one trigger into three
    # broken fragments.
    text = (
        "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete\n"
        "BEFORE DELETE ON audit_log\n"
        "BEGIN\n"
        "    SELECT RAISE(ABORT, 'audit_log is append-only; use a new row instead');\n"
        "END;"
    )
    stmts = _split_statements(text)
    assert len(stmts) == 1
    stmt = stmts[0]
    assert stmt.strip().startswith("CREATE TRIGGER")
    assert "BEGIN" in stmt
    assert stmt.strip().endswith("END")
    assert "append-only; use a new row instead" in stmt  # literal semicolon preserved intact


def test_split_statements_multiple_statements_and_triggers():
    text = (
        "CREATE TABLE a (id INTEGER);\n"
        "CREATE INDEX idx_a ON a(id);\n"
        "CREATE TRIGGER t1\n"
        "BEFORE DELETE ON a\n"
        "BEGIN\n"
        "    SELECT RAISE(ABORT, 'no; deletes');\n"
        "END;\n"
        "CREATE TABLE b (id INTEGER);"
    )
    stmts = _split_statements(text)
    assert len(stmts) == 4
    assert stmts[0].startswith("CREATE TABLE a")
    assert stmts[1].startswith("CREATE INDEX idx_a")
    assert stmts[2].startswith("CREATE TRIGGER t1")
    assert "END" in stmts[2]
    assert stmts[3].startswith("CREATE TABLE b")


def test_split_statements_against_every_real_schema_file():
    # The real regression check: every shipped schema file must split into
    # only well-formed top-level statements, with every trigger body intact.
    schema_dir = Path(__file__).resolve().parent.parent / "goengine"
    schema_files = [
        "schema.sql", "schema_phase2.sql", "schema_phase3.sql",
        "schema_diagnostics.sql", "schema_network_tests.sql", "schema_agent.sql",
    ]
    for fname in schema_files:
        text = (schema_dir / fname).read_text(encoding="utf-8")
        stmts = _split_statements(text)
        assert stmts, f"{fname} produced no statements"
        for stmt in stmts:
            upper = stmt.strip().upper()
            assert upper.startswith(("CREATE", "PRAGMA", "INSERT", "ALTER")), (
                f"malformed statement in {fname}: {stmt[:100]!r}"
            )
            if "CREATE TRIGGER" in upper:
                assert "BEGIN" in upper and upper.rstrip().endswith("END"), (
                    f"broken trigger in {fname}: {stmt!r}"
                )


class _FakeResult:
    def __init__(self, columns, rows, last_insert_rowid=None, rows_affected=0):
        self.columns = columns
        self.rows = rows
        self.last_insert_rowid = last_insert_rowid
        self.rows_affected = rows_affected


def test_turso_row_dict_and_positional_access():
    row = TursoRow((1, "Alice"), ("id", "name"))
    assert row["id"] == 1
    assert row["name"] == "Alice"
    assert row[0] == 1
    assert list(row) == [1, "Alice"]
    assert row.keys() == ["id", "name"]


def test_turso_cursor_fetchone_fetchall_and_metadata():
    result = _FakeResult(
        columns=("id", "name"),
        rows=[(1, "Alice"), (2, "Bob")],
        last_insert_rowid=2,
        rows_affected=2,
    )
    cur = TursoCursor(result)
    assert cur.lastrowid == 2
    assert cur.rowcount == 2

    first = cur.fetchone()
    assert first["name"] == "Alice"
    rest = cur.fetchall()
    assert len(rest) == 1
    assert rest[0]["name"] == "Bob"
    assert cur.fetchone() is None


def test_turso_cursor_iteration():
    result = _FakeResult(columns=("id",), rows=[(1,), (2,), (3,)])
    cur = TursoCursor(result)
    assert [r["id"] for r in cur] == [1, 2, 3]
