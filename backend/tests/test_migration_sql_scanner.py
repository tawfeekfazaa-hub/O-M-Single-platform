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
        # PostgreSQL accepts `$` inside an unquoted identifier, so `foo$$` is a
        # table name. Reading its `$$` as a dollar-quote opener blanks the file
        # through to the next `$$`, hiding the COMMIT in between.
        "CREATE TABLE foo$$ (id int); COMMIT; CREATE TABLE bar$$ (id int);",
        # A genuine END after a BEGIN ATOMIC body: PostgreSQL commits on it.
        "CREATE FUNCTION f() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT 1; END; END;",
        # `foo$E` is one identifier, so the quote after it opens a PLAIN string
        # in which a backslash escapes nothing. Reading that E as a string
        # prefix ran the literal on to the next quote and swallowed the COMMIT.
        "SELECT foo$E'abc\\'; COMMIT; SELECT 'done';",
        # `foo$BEGIN` is one identifier and `ATOMIC` its alias — not a function
        # body opener, so the END that follows is a real commit.
        "SELECT 1 FROM foo$BEGIN ATOMIC; END;",
        # `begin` is an unreserved word, so this is a table named `begin`
        # aliased `atomic` — not a function body, so the END is a real commit.
        "SELECT * FROM begin atomic; END;",
        # Same two words inside a CREATE that defines no routine: a table named
        # `begin` with a column named `atomic`.
        "CREATE TABLE begin (atomic int); END;",
        # PostgreSQL ends a line comment at a bare CR as well as a LF, so this
        # COMMIT is not commented out.
        "CREATE TABLE t (id int); -- note\rCOMMIT;",
        # The two words in a routine's SIGNATURE — a parameter named `begin` of
        # a type named `atomic` — with a quoted body. No SQL-standard body is
        # opened, so the END that follows is a real commit.
        "CREATE FUNCTION f(begin atomic) RETURNS int LANGUAGE SQL AS 'SELECT 1'; END;",
        # Same, with a block comment after the `(`. The comment must not cost
        # the tokenizer its place inside the parentheses.
        "CREATE FUNCTION f(/* note */ begin atomic) RETURNS int LANGUAGE SQL AS 'SELECT 1'; END;",
        # `begin.atomic` is a schema-qualified NAME, not the two keywords: the
        # dot separates them in the SQL even though they are adjacent words.
        "CREATE SCHEMA begin; CREATE FUNCTION begin.atomic() RETURNS int "
        "LANGUAGE SQL AS 'SELECT 1'; END;",
        # A non-ASCII character is an identifier character to PostgreSQL, so
        # these dollars belong to the table names, not to a quote.
        "CREATE TABLE aq\u0301$$ (id int); COMMIT; CREATE TABLE bq\u0301$$ (id int);",
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
            "CREATE OR REPLACE of a sql-standard body",
            "CREATE OR REPLACE FUNCTION f() RETURNS INT LANGUAGE SQL\n"
            "BEGIN ATOMIC\n  SELECT 1;\nEND;",
        ),
        (
            # The CR rule must not swallow the rest of the file either.
            "line comment ended by a bare CR",
            "-- a note\rCREATE TABLE t (id int);",
        ),
        (
            # Comments are whitespace, so they must NOT break the adjacency the
            # opener rule relies on.
            "sql-standard body with a comment between the keywords",
            "CREATE FUNCTION f() RETURNS INT LANGUAGE SQL "
            "BEGIN /* still a body */ ATOMIC SELECT 1; END;",
        ),
        (
            "sql-standard body containing CASE ... END",
            "CREATE FUNCTION f(n INT) RETURNS INT LANGUAGE SQL\nBEGIN ATOMIC\n"
            "  SELECT CASE WHEN n > 0 THEN 1 ELSE 0 END;\nEND;",
        ),
        (
            # One exemption per body, so two bodies must both be exempt.
            "two sql-standard function bodies",
            "CREATE FUNCTION a() RETURNS INT LANGUAGE SQL BEGIN ATOMIC SELECT 1; END;\n"
            "CREATE FUNCTION b() RETURNS INT LANGUAGE SQL BEGIN ATOMIC SELECT 2; END;",
        ),
        (
            # The identifier rule must not break real dollar quoting elsewhere.
            "dollar-quoted body beside an identifier containing $",
            "CREATE TABLE tbl$x (id INT);\n"
            "CREATE FUNCTION f() RETURNS INT AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;",
        ),
        (
            # A dollar-quote tag follows identifier rules, which include
            # non-ASCII letters. Refusing this would refuse valid SQL.
            "unicode dollar-quote tag",
            "CREATE FUNCTION uni() RETURNS INT AS $\u00e9$ BEGIN RETURN 1; END; $\u00e9$ "
            "LANGUAGE plpgsql;",
        ),
        (
            "tagged body whose tag contains digits and underscores",
            "CREATE FUNCTION f() RETURNS INT AS $body_2$ BEGIN RETURN 1; END; $body_2$ "
            "LANGUAGE plpgsql;",
        ),
        (
            # $1 is a positional parameter, not a quote delimiter.
            "positional parameters",
            "CREATE FUNCTION f(int) RETURNS INT AS 'SELECT $1' LANGUAGE sql;",
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
    forward = [p for p in sorted(directory.glob("*.sql")) if not p.name.endswith(".down.sql")]
    # Same reason as the carriage-return sweep below: a loop over an empty glob
    # is a test that passes having read nothing.
    assert forward, f"no forward migrations found under {directory}"
    for path in forward:
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


def test_a_commit_between_identifiers_containing_dollars_is_still_found(tmp_path: Path):
    # The narrow version of the case above: the COMMIT sits exactly where a
    # naive dollar-quote reading would blank it.
    with pytest.raises(MigrationError) as excinfo:
        read_migration(
            write(tmp_path, "CREATE TABLE foo$$ (id int); COMMIT; CREATE TABLE bar$$ (id int);")
        )
    assert "COMMIT" in str(excinfo.value)


def test_the_end_exemption_is_spent_by_the_body_that_earned_it(tmp_path: Path):
    # One BEGIN ATOMIC exempts exactly one statement-initial END. The second is
    # a transaction commit and must still be refused.
    with pytest.raises(MigrationError) as excinfo:
        read_migration(
            write(
                tmp_path,
                "CREATE FUNCTION f() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT 1; END; END;",
            )
        )
    assert "END" in str(excinfo.value)


def words_of(sql: str) -> list[list[str]]:
    from app.db.migrations import _statements

    return [[t.word for t in statement] for statement in _statements(sql)]


def test_an_identifier_is_read_as_one_token(tmp_path: Path):
    """The property the whole guard rests on, stated directly.

    PostgreSQL lexes an identifier greedily and `$` is an identifier character
    after the first, so `foo$$`, `foo$E` and `foo$BEGIN` are single tokens.
    Every bypass found in review came from reading a delimiter, a string prefix
    or a keyword out of the middle of one.
    """
    assert words_of("SELECT foo$$ FROM bar$BEGIN") == [["SELECT", "FOO$$", "FROM", "BAR$BEGIN"]]
    assert words_of("SELECT foo$E'x'") == [["SELECT", "FOO$E"]]
    assert words_of("SELECT E'x'") == [["SELECT"]]  # a real escape-string prefix


def test_words_inside_quotes_and_comments_are_not_tokens(tmp_path: Path):
    assert words_of("SELECT 'COMMIT'; -- COMMIT\n/* COMMIT */ SELECT $$COMMIT$$") == [
        ["SELECT"],
        ["SELECT"],
    ]


def test_tokens_carry_their_parenthesis_depth(tmp_path: Path):
    """What separates a routine's parameters from its body.

    `CREATE FUNCTION f(begin atomic)` names a parameter and a type inside the
    signature; a SQL-standard body sits outside it. Only the second opens a
    body, and depth is what tells them apart without a grammar.
    """
    from app.db.migrations import _statements

    (statement,) = _statements("CREATE FUNCTION f(begin atomic) RETURNS int LANGUAGE SQL")
    depths = {t.word: t.depth for t in statement}
    assert depths["BEGIN"] == 1 and depths["ATOMIC"] == 1
    assert depths["CREATE"] == 0 and depths["RETURNS"] == 0

    (body, *_) = _statements("CREATE FUNCTION f() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT 1")
    assert {t.word: t.depth for t in body}["BEGIN"] == 0

    # A comment inside the signature must not cost the tokenizer its place: the
    # comment's own nesting counter used to overwrite the parenthesis depth.
    (commented,) = _statements("CREATE FUNCTION f(/* note */ begin atomic)")
    assert {t.word: t.depth for t in commented}["BEGIN"] == 1


def test_tokens_know_whether_punctuation_separated_them(tmp_path: Path):
    # `begin.atomic` is one qualified name. Without this, the dot vanished and
    # the two halves read as the keywords that open a routine body.
    from app.db.migrations import _statements

    (qualified,) = _statements("CREATE FUNCTION begin.atomic()")
    assert [(t.word, t.adjacent) for t in qualified] == [
        ("CREATE", True),
        ("FUNCTION", True),
        ("BEGIN", True),
        ("ATOMIC", False),  # the dot separates it
    ]

    (keywords,) = _statements("BEGIN /* c */ ATOMIC")
    assert [t.adjacent for t in keywords] == [True, True]  # comments are whitespace


def test_only_a_routine_definition_opens_a_body(tmp_path: Path):
    """The exemption belongs to CREATE FUNCTION/PROCEDURE, not to CREATE.

    `BEGIN` and `ATOMIC` land side by side in ordinary SQL — a table and its
    alias, a table and its column — and each of those used to spend the
    exemption on a real transaction-ending END.
    """
    from app.db.migrations import _defines_a_routine

    assert _defines_a_routine(["CREATE", "FUNCTION", "F"])
    assert _defines_a_routine(["CREATE", "OR", "REPLACE", "PROCEDURE", "P"])
    assert not _defines_a_routine(["CREATE", "TABLE", "BEGIN"])
    assert not _defines_a_routine(["CREATE", "OR", "REPLACE", "VIEW", "V"])
    assert not _defines_a_routine(["SELECT"])


def test_a_bare_carriage_return_ends_a_line_comment(tmp_path: Path):
    # Everything after the CR is live SQL, exactly as PostgreSQL reads it.
    assert words_of("SELECT 1; -- note\rCOMMIT;") == [["SELECT"], ["COMMIT"], []]


def test_the_file_is_executed_exactly_as_written(tmp_path: Path):
    """Newline normalization stops at the checksum.

    A CRLF inside a string literal or a dollar-quoted body is part of the
    VALUE. Folding it to keep checksums stable across checkouts would quietly
    store different data than the migration says — measured before the split:
    `'first\r\nsecond'` reached the column as `first\nsecond`.
    """
    path = tmp_path / "001_probe.sql"
    payload = "INSERT INTO t VALUES ('first\r\nsecond');\r\n"
    path.write_bytes(payload.encode())
    assert read_migration(path).content == payload


def test_files_that_execute_differently_never_share_a_checksum(tmp_path: Path):
    """The checksum is over the exact executed text — no normalization.

    This reverses an earlier design in which CRLF was folded before hashing, so
    that a Windows checkout could not refuse every run. That folding made two
    files that insert DIFFERENT DATA hash identically: a physical CRLF inside a
    literal is part of the value. Immutability that cannot see a difference the
    database will see is not immutability.

    The checkout problem moved to `.gitattributes`, which the next test checks.
    """
    lf = tmp_path / "001_probe.sql"
    lf.write_bytes(b"INSERT INTO t VALUES ('first\nsecond');")
    crlf = tmp_path / "002_probe.sql"
    crlf.write_bytes(b"INSERT INTO t VALUES ('first\r\nsecond');")

    assert read_migration(lf).content != read_migration(crlf).content
    assert read_migration(lf).checksum != read_migration(crlf).checksum


def test_no_migration_in_this_repository_carries_a_carriage_return():
    """What `.gitattributes` is supposed to guarantee, actually checked.

    Hashing exact bytes means a CRLF checkout would refuse every run, so
    `*.sql text eol=lf` pins the working tree. A rule nothing verifies is a
    rule that quietly stops holding, so this asserts the outcome rather than
    the configuration: if a CR ever reaches a migration file, this fails before
    an operator meets it as a checksum mismatch.

    A migration that genuinely needs a CR in a value should write it as an
    escape — `E'first\\r\\nsecond'` — since file line endings are normalized by
    git and are not a reliable way to carry data.
    """
    directory = Path(__file__).resolve().parents[1] / "migrations"
    files = sorted(directory.glob("*.sql"))
    # Assert the sweep found something before concluding it found nothing wrong.
    # A renamed or moved directory makes the glob empty and every CR in the
    # repository invisible, with this test still green — the shape review has
    # found in my tests three rounds running.
    assert files, f"no migration files found under {directory}"
    offenders = [p.name for p in files if b"\r" in p.read_bytes()]
    assert offenders == []


def test_a_migration_numbered_with_unicode_digits_is_refused(tmp_path: Path):
    """``\\d`` matches Unicode decimal digits; ``[0-9]`` does not.

    ``٠٠٢_beta.sql`` passed the filename pattern and then compared unequal to
    ``002`` as a string, so it slipped a reused sequence number past the
    duplicate check — the safeguard added one round earlier.
    """
    from app.db.migrations import discover

    (tmp_path / "002_alpha.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "\u0660\u0660\u0662_beta.sql").write_text("CREATE TABLE b (id INT);")

    with pytest.raises(MigrationError, match="does not match NNN_name.sql"):
        discover(tmp_path)
