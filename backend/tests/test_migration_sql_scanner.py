"""The transaction-control guard on migration files.

Pure text analysis, so it runs in the offline suite: this is the check that
decides whether a file is allowed near the database at all, and it should not
need one to be exercised.

It is a guard, not the guarantee — a file that ends the transaction some way
this scanner does not recognise is still caught after execution, by the
is_in_transaction() check in _run_sql. That half needs a real database and lives
in test_migration_runner.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.migrations import MigrationError, read_migration


def write(tmp_path: Path, sql: str, *, down: str | None = None) -> Path:
    path = tmp_path / "001_probe.sql"
    path.write_text(sql)
    if down is not None:
        (tmp_path / "001_probe.down.sql").write_text(down)
    return path


# --------------------------------------------------------------------- #
# refused: the file would break the migration/history pairing           #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nCREATE TABLE widget (id INT);\nCOMMIT;",  # a standalone script
        "CREATE TABLE widget (id INT);\nCOMMIT;",
        "CREATE TABLE widget (id INT);\ncommit;",  # keywords are case-insensitive
        "CREATE TABLE widget (id INT);\nEND;",  # END is COMMIT's synonym
        "START TRANSACTION;\nCREATE TABLE widget (id INT);",
        "CREATE TABLE widget (id INT);\nROLLBACK;",
        "CREATE TABLE widget (id INT);\nABORT;",
        "SAVEPOINT s;\nCREATE TABLE widget (id INT);",
        "SAVEPOINT s;\nCREATE TABLE widget (id INT);\nRELEASE SAVEPOINT s;",
        "PREPARE TRANSACTION 'x';",
        "\n\n   COMMIT   ;\n",  # leading blank statements and loose spacing
    ],
)
def test_a_migration_that_manages_its_own_transaction_is_refused(tmp_path: Path, sql: str):
    path = write(tmp_path, sql)
    with pytest.raises(MigrationError, match="manages its own transaction"):
        read_migration(path)


def test_the_rollback_file_is_held_to_the_same_rule(tmp_path: Path):
    # A down file runs through exactly the same transaction pairing, and it is
    # the more destructive of the two.
    path = write(
        tmp_path,
        "CREATE TABLE widget (id INT);",
        down="BEGIN;\nDROP TABLE widget;\nCOMMIT;",
    )
    with pytest.raises(MigrationError, match="001_probe.down.sql manages its own transaction"):
        read_migration(path)


def test_the_refusal_names_what_it_found(tmp_path: Path):
    path = write(tmp_path, "BEGIN;\nCREATE TABLE widget (id INT);\nCOMMIT;")
    with pytest.raises(MigrationError) as excinfo:
        read_migration(path)
    message = str(excinfo.value)
    assert "BEGIN" in message and "COMMIT" in message
    assert "CREATE TABLE widget" not in message  # names and reasons, never the SQL


# --------------------------------------------------------------------- #
# allowed: words that merely LOOK like transaction control              #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("line comment", "-- remember to COMMIT\nCREATE TABLE widget (id INT);"),
        ("block comment", "/* BEGIN ... COMMIT */\nCREATE TABLE widget (id INT);"),
        (
            "nested block comment",
            "/* outer /* ROLLBACK */ still commented END */\nCREATE TABLE widget (id INT);",
        ),
        ("string literal", "CREATE TABLE note (body TEXT DEFAULT 'COMMIT; END;');"),
        ("doubled quote in string", "CREATE TABLE note (body TEXT DEFAULT 'it''s COMMIT; time');"),
        ("escape string", r"INSERT INTO note (body) VALUES (E'it\'s a COMMIT; really');"),
        ("quoted identifier", 'CREATE TABLE "COMMIT" (id INT);'),
        (
            "plpgsql body",
            "CREATE FUNCTION f() RETURNS INT AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;",
        ),
        (
            "tagged dollar quote",
            "CREATE FUNCTION f() RETURNS INT AS $body$ BEGIN RETURN 1; END; $body$ "
            "LANGUAGE plpgsql;",
        ),
        ("CASE ... END mid-statement", "CREATE VIEW v AS SELECT CASE WHEN true THEN 1 END AS c;"),
        ("PREPARE without TRANSACTION", "PREPARE p AS SELECT 1;"),
        ("a column called end", 'CREATE TABLE span (id INT, "end" TIMESTAMPTZ);'),
        (
            # PostgreSQL 14+ LANGUAGE SQL bodies are not quoted, so the END that
            # closes one reaches the scanner as a statement of its own.
            "sql-standard function body",
            "CREATE FUNCTION f() RETURNS INT LANGUAGE SQL\nBEGIN ATOMIC\n  SELECT 1;\nEND;",
        ),
        (
            "sql-standard body containing CASE ... END",
            "CREATE FUNCTION f(n INT) RETURNS INT LANGUAGE SQL\nBEGIN ATOMIC\n"
            "  SELECT CASE WHEN n > 0 THEN 1 ELSE 0 END;\nEND;",
        ),
    ],
)
def test_transaction_keywords_that_are_not_transaction_control_are_allowed(
    tmp_path: Path, label: str, sql: str
):
    # A false alarm here is not a harmless annoyance: the author cannot work
    # around it without changing SQL that was correct.
    assert read_migration(write(tmp_path, sql)).content == sql, label


def test_the_real_migrations_pass_the_guard():
    # The rule has to hold for what the repository actually ships, not only for
    # fixtures written to satisfy it.
    directory = Path(__file__).resolve().parents[1] / "migrations"
    for path in sorted(directory.glob("*.sql")):
        if not path.name.endswith(".down.sql"):
            read_migration(path)


def test_a_real_commit_is_still_refused_beside_a_sql_standard_function_body(tmp_path: Path):
    # Standing END down for such a file must not stand the rest of the guard
    # down with it.
    path = write(
        tmp_path,
        "CREATE FUNCTION f() RETURNS INT LANGUAGE SQL\nBEGIN ATOMIC\n  SELECT 1;\nEND;\nCOMMIT;",
    )
    with pytest.raises(MigrationError, match="manages its own transaction"):
        read_migration(path)
