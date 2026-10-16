"""Regression cases from the unclassified SQL corpus and valid counterexamples."""

import unittest

from sql_tools import validate_sql, validate_sql_statements
from sql_tools.ddl import check_ddl_syntax
from sql_tools.parsing import is_valid_sql


class MySQLSyntaxTests(unittest.TestCase):
    def test_offset_requires_limit_in_its_own_block(self):
        for sql in (
            "SELECT MIN(week) FROM t WHERE attendance < 34 OFFSET 336",
            "SELECT a FROM t OFFSET 307.00",
            "SELECT a FROM (SELECT a FROM t OFFSET 3) x LIMIT 5",
            "SELECT a FROM t UNION SELECT a FROM u OFFSET 3",
        ):
            with self.subTest(sql=sql):
                result = validate_sql(sql, dialect="mysql", validate_schema=False)
                self.assertEqual(result.syntax_status, "invalid")
                self.assertEqual(result.status_history, ["validation_failed"])

    def test_limit_offset_numbers_and_parameters(self):
        for suffix in ("LIMIT -1", "LIMIT 1.5", "LIMIT '5'", "LIMIT 1 + 2",
                       "LIMIT 5 OFFSET -1", "LIMIT 5 OFFSET 307.00"):
            with self.subTest(suffix=suffix):
                self.assertFalse(is_valid_sql("SELECT a FROM t " + suffix, dialect="mysql"))
        for suffix in ("LIMIT 0", "LIMIT 5 OFFSET 3", "LIMIT 3, 5",
                       "LIMIT ? OFFSET ?", "LIMIT row_count OFFSET start_row"):
            with self.subTest(suffix=suffix):
                self.assertTrue(is_valid_sql("SELECT a FROM t " + suffix, dialect="mysql"))
        self.assertTrue(is_valid_sql("SELECT a FROM t OFFSET 3", dialect="postgres"))

    def test_mysql_ddl_length_and_shared_query_checks(self):
        for kind in ("VARCHAR", "NVARCHAR", "VARBINARY"):
            with self.subTest(kind=kind):
                ddl = f"CREATE TABLE t (a {kind})"
                self.assertEqual(check_ddl_syntax(ddl, "mysql")[0], "invalid")
                self.assertFalse(is_valid_sql(ddl, dialect="mysql"))
                result = validate_sql("SELECT a FROM t", dialect="mysql", ddl_statement=ddl)
                self.assertEqual(result.ddl_syntax_status, "invalid")
                self.assertEqual(result.schema_status, "unavailable")
                self.assertEqual(result.status_history, ["validation_failed"])
        for kind in ("VARCHAR(255)", "VARCHAR(0)", "VARBINARY(10)", "CHAR", "TEXT", "INT"):
            self.assertEqual(check_ddl_syntax(f"CREATE TABLE t (a {kind})", "mysql")[0], "valid")
        self.assertEqual(check_ddl_syntax("CREATE TABLE t (a VARCHAR)", "postgres")[0], "valid")
        for query in ("SELECT a[1] FROM t", "SELECT a FROM t OFFSET 3"):
            self.assertEqual(check_ddl_syntax("CREATE VIEW v AS " + query, "mysql")[0], "invalid")

    def test_suspicious_but_legal_expressions_are_not_syntax_errors(self):
        ddl = "CREATE TABLE t (a VARCHAR(255), b VARCHAR(255))"
        for sql in ('SELECT "player" FROM t', 'SELECT 2007 FROM t WHERE 2008 = "a"',
                    'SELECT a & b FROM t', 'SELECT MAX(a) = 100 AS flag FROM t',
                    "SELECT COUNT(1) FROM t", "SELECT a FROM t WHERE 'closed' = 'closed'"):
            with self.subTest(sql=sql):
                self.assertEqual(validate_sql(sql, dialect="mysql", ddl_statement=ddl).status_history,
                                 ["validated"])

    def test_stream_is_not_consumed_by_progress_count(self):
        visited = []

        def rows():
            for index in range(3):
                visited.append(index)
                yield {"answer": "SELECT a FROM t", "context": "CREATE TABLE t (a INT)"}

        valid, invalid = validate_sql_statements(rows(), max_rows=2, dialect="mysql")
        self.assertEqual(len(valid), 2)
        self.assertEqual(invalid, [])
        self.assertEqual(visited, [0, 1])
