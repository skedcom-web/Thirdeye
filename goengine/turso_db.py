"""Turso (remote libSQL) compatibility layer.

A thin shim so the rest of the codebase's `conn.execute(...)`,
`.executemany(...)`, `.executescript(...)` calls and `sqlite3.Row`-style
dict access on results work unchanged whether `db.connect()` opens a local
SQLite file (dev/tests) or a remote Turso database (production -- Render's
own disk doesn't survive a redeploy, which is the whole reason this module
exists: see the free-tier persistence gap discussed when this was built).

Every write goes over the network and is confirmed by Turso before
`execute()` returns. There is deliberately no local replica file and no
explicit sync() step: Turso's own docs describe embedded-replica writes as
NOT automatically durable (they require an explicit, undocumented-as-
synchronous `.sync()` call), which would silently reintroduce the exact
"data only exists locally, briefly" bug this migration exists to close.
Connecting directly to the remote database trades a small per-query
network round trip for a straightforward guarantee: a write either lands on
Turso or the call raises. Verified empirically against a real Turso
database (not just against docs, which are thin/partly stale for this
package) before this shim was written -- see chat history for the probe.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

import libsql_client

_BEGIN_RE = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_END_RE = re.compile(r"\bEND\b", re.IGNORECASE)


class TursoRow:
    """Wraps libsql_client.result.Row (positional-only) so it supports the
    same `row["col"]` dict-style access sqlite3.Row already gives the rest
    of the codebase, keyed by the parent ResultSet's column list."""

    __slots__ = ("_row", "_columns")

    def __init__(self, row: Any, columns: tuple[str, ...]) -> None:
        self._row = row
        self._columns = columns

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, str):
            return self._row[self._columns.index(key)]
        return self._row[key]

    def keys(self) -> list[str]:
        return list(self._columns)

    def __iter__(self):
        return iter(self._row)

    def __repr__(self) -> str:
        return f"TursoRow({dict(zip(self._columns, self._row))!r})"


class TursoCursor:
    """Mimics the sqlite3.Cursor surface this codebase relies on:
    fetchone/fetchall/lastrowid/rowcount, plus iteration."""

    __slots__ = ("_result", "_rows", "_index")

    def __init__(self, result: Any) -> None:
        self._result = result
        self._rows = [TursoRow(r, result.columns) for r in result.rows]
        self._index = 0

    @property
    def lastrowid(self) -> int | None:
        return self._result.last_insert_rowid

    @property
    def rowcount(self) -> int:
        return self._result.rows_affected

    def fetchone(self) -> TursoRow | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[TursoRow]:
        remaining = self._rows[self._index:]
        self._index = len(self._rows)
        return remaining

    def __iter__(self):
        return iter(self._rows[self._index:])


def _strip_line_comments(script: str) -> str:
    """Removes `-- ...` line comments. A naive split-on-`;` would otherwise
    be corrupted by this codebase's schema files, which have comment prose
    containing literal semicolons (e.g. "...stored so an auditor can see;
    ..."). Does not attempt to special-case `--` inside a string literal --
    none of these schema files use it, so a plain per-line scan is safe."""
    lines = []
    for line in script.splitlines():
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    return "\n".join(lines)


def _split_statements(script: str) -> list[str]:
    """Splits a schema file's DDL text into individual statements for
    Turso's batch(), which -- unlike sqlite3's executescript() -- takes a
    list of separate SQL strings rather than one multi-statement string.

    Strips comments first (see _strip_line_comments), then splits on `;`
    while tracking BEGIN/END nesting depth, so a `CREATE TRIGGER ... BEGIN
    ... END;` block stays one statement even though its body legitimately
    contains its own semicolons -- including, in this codebase, one INSIDE
    a string literal ('documents are write-once; insert a new version
    instead'). Fragments are always rejoined with `;`, so whatever caused
    an interior split (trigger syntax or a string literal) is reconstructed
    byte-for-byte regardless of the reason -- only top-level (depth 0)
    semicolons actually end a statement.
    """
    text = _strip_line_comments(script)
    statements: list[str] = []
    buffer: list[str] = []
    depth = 0
    for fragment in text.split(";"):
        buffer.append(fragment)
        depth += len(_BEGIN_RE.findall(fragment)) - len(_END_RE.findall(fragment))
        if depth <= 0:
            stmt = ";".join(buffer).strip()
            if stmt:
                statements.append(stmt)
            buffer = []
            depth = 0
    tail = ";".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


class TursoConnection:
    """Drop-in-enough replacement for sqlite3.Connection, backed by a
    remote Turso database over HTTP. See module docstring for why."""

    def __init__(self, url: str, auth_token: str) -> None:
        http_url = url.replace("libsql://", "https://", 1) if url.startswith("libsql://") else url
        self._client = libsql_client.create_client_sync(http_url, auth_token=auth_token)
        self.row_factory = None  # accepted for API parity only; rows are already dict-accessible

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> TursoCursor:
        result = self._client.execute(sql, list(params) if params else [])
        return TursoCursor(result)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        for params in seq_of_params:
            self._client.execute(sql, list(params))

    def executescript(self, script: str) -> None:
        statements = _split_statements(script)
        if statements:
            self._client.batch(statements)

    def commit(self) -> None:
        pass  # every execute() is already durable on return -- no-op for API parity

    def close(self) -> None:
        self._client.close()
