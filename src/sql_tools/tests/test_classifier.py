"""Behavioral tests for syntax, DDL resolution, and Spider rule boundaries."""

import unittest
from unittest.mock import patch

from sql_tools import classify_records, classify_sql, is_valid_sql


class ClassificationTests(unittest.TestCase):
    def test_ddl_syntax_checked_without_schema_validation(self):
        with patch("sql_tools.validation.validate_sql_schema") as validate:
            result = classify_sql("SELECT missing FROM unknown", dialect="mysql", validate_schema=False,
                                  ddl_statement="CREATE TABLE t (id INT)")
        validate.assert_not_called()
        self.assertEqual(result.ddl_syntax_status, "valid")
        self.assertEqual(result.ddl_syntax_errors, [])
        self.assertEqual(result.schema_status, "not_checked")

    def test_bad_ddl_skips_schema_validation(self):
        for ddl in ("", "-- comment", "CREATE TABLE x", "CREATE TABLE x ()",
                    "CREATE TABLE x (id)", "CREATE TABLE x (id INT",
                    "CREATE TABLE x (id INT); CREATE TABLE y (",
                    "SELECT id FROM x"):
            for validate_schema in (False, True):
                with self.subTest(ddl=ddl, validate_schema=validate_schema):
                    with patch("sql_tools.validation.validate_sql_schema") as validate:
                        result = classify_sql("SELECT id FROM x", dialect="mysql",
                                              ddl_statement=ddl,
                                              validate_schema=validate_schema)
                    validate.assert_not_called()
                    self.assertEqual(result.ddl_syntax_status, "invalid")
                    self.assertTrue(result.ddl_syntax_errors)
                    self.assertEqual(result.schema_status,
                                     "unavailable" if validate_schema else "not_checked")
                    self.assertEqual(result.spider_hardness, "easy")

    def test_ddl_checked_even_when_query_syntax_fails(self):
        result = classify_sql("SELECT (", ddl_statement="CREATE TABLE t (id INT)")
        self.assertEqual(result.syntax_status, "invalid")
        self.assertEqual(result.ddl_syntax_status, "valid")
        result = classify_sql("SELECT (", ddl_statement="CREATE TABLE t (")
        self.assertEqual(result.ddl_syntax_status, "invalid")

    def test_multiple_ddl_statements_and_schema_validation(self):
        result = classify_sql("SELECT t.id FROM t JOIN u ON t.id = u.id",
                              ddl_statement="CREATE TABLE t (id INT); CREATE TABLE u (id INT)",
                              validate_schema=True)
        self.assertEqual(result.ddl_syntax_status, "valid")
        self.assertEqual(result.schema_status, "valid")

    def test_valid_ddl_can_be_unavailable_as_schema_metadata(self):
        for ddl in ("CREATE TABLE t AS SELECT id FROM unknown",
                    "CREATE TABLE t LIKE unknown",
                    "CREATE TABLE t (id INT); ALTER TABLE t ADD name TEXT",
                    "DROP TABLE unknown", "CREATE SCHEMA example",
                    "CREATE TABLE t (id INT, id INT)"):
            with self.subTest(ddl=ddl):
                result = classify_sql("SELECT id FROM t", ddl_statement=ddl,
                                      validate_schema=True, dialect="mysql")
                self.assertEqual(result.ddl_syntax_status, "valid", result.ddl_syntax_errors)
                self.assertEqual(result.schema_status, "unavailable")

    def test_absent_ddl_is_not_syntax_checked(self):
        for validate_schema in (False, True):
            result = classify_sql("SELECT a FROM t", validate_schema=validate_schema)
            self.assertEqual(result.ddl_syntax_status, "not_checked")
            self.assertEqual(result.ddl_syntax_errors, [])
            self.assertEqual(result.schema_status,
                             "unavailable" if validate_schema else "not_checked")

    def test_cached_ddl_errors_cannot_be_mutated_via_result(self):
        result = classify_sql("SELECT a FROM t", ddl_statement="CREATE TABLE t (")
        result.ddl_syntax_errors.clear()
        self.assertTrue(classify_sql("SELECT a FROM t",
                                    ddl_statement="CREATE TABLE t (").ddl_syntax_errors)

    def test_syntax_does_not_require_existing_objects(self):
        self.assertTrue(is_valid_sql("SELECT nonexistent FROM imaginary"))
        self.assertEqual(classify_sql("SELECT x FROM t", validate_schema=False).schema_status, "not_checked")

    def test_malformed_and_multiple_statements(self):
        for sql in ("", "-- comment", "SELECT FROM t", "SELECT a FROM", "SELECT (a FROM t",
                    "SELECT a FROM t; SELECT b FROM t", "just words"):
            with self.subTest(sql=sql):
                self.assertFalse(is_valid_sql(sql))
                self.assertIsNone(classify_sql(sql).spider_hardness)

    def test_invalid_dialect_is_configuration_error(self):
        with self.assertRaises(ValueError):
            classify_sql("SELECT a FROM t", dialect="not_a_dialect")

    def test_official_hardness_examples(self):
        cases = {
            "SELECT a FROM t": "easy",
            "SELECT COUNT(*) FROM t WHERE a > 1": "easy",
            "SELECT a, b FROM t": "medium",
            "SELECT a FROM t WHERE b = 1 ORDER BY a": "medium",
            "SELECT a FROM t WHERE b = 1 ORDER BY a LIMIT 1": "hard",
            "SELECT a FROM t WHERE b = 1 GROUP BY a ORDER BY a LIMIT 1": "extra",
            "SELECT a FROM t WHERE a IN (SELECT b FROM u)": "hard",
            "SELECT a, b FROM t WHERE a IN (SELECT b FROM u)": "extra",
            "SELECT a FROM t UNION SELECT b FROM u": "hard",
        }
        for sql, expected in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(classify_sql(sql).spider_hardness, expected)

    def test_join_or_like_and_aggregate_counts(self):
        result = classify_sql("SELECT a.id, b.id FROM a JOIN b ON a.id = b.id "
                              "WHERE a.name LIKE 'x%' OR b.id = 2")
        self.assertEqual((result.component1, result.component2, result.others), (4, 0, 2))
        result = classify_sql("SELECT MAX(a), MIN(b) FROM t")
        self.assertEqual(result.others, 2)

    def test_nested_query_counts_do_not_leak_to_outer_query(self):
        result = classify_sql("SELECT a FROM t WHERE a IN "
                              "(SELECT b FROM u WHERE c = 1 ORDER BY b LIMIT 1)")
        self.assertEqual((result.component1, result.component2), (1, 1))
        self.assertEqual(result.spider_hardness, "hard")

    def test_unsupported_features_are_not_extra(self):
        for sql in (
            "WITH x AS (SELECT a FROM t) SELECT a FROM x",
            "SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t",
            "SELECT COALESCE(a, 0) FROM t",
            "SELECT a FROM t WHERE a IN (1, 2)",
            "SELECT a FROM t UNION ALL SELECT b FROM u",
            "SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 2",
            "SELECT a FROM t JOIN u USING (a)",
            "SELECT t.* EXCEPT (a) FROM t",
            "SELECT SUM(a) + SUM(b) FROM t",
            "INSERT INTO t (a) VALUES (1)",
        ):
            with self.subTest(sql=sql):
                result = classify_sql(sql)
                self.assertEqual(result.syntax_status, "valid")
                self.assertIsNone(result.spider_hardness)
                self.assertIsNotNone(result.extended_hardness)
                self.assertEqual(result.classification_status, "extended")

    def test_schema_errors_do_not_change_hardness(self):
        sql = "SELECT bad FROM users WHERE id = 1"
        result = classify_sql(sql, ddl_statement="CREATE TABLE users (id INT)",
                              validate_schema=True)
        self.assertEqual(result.schema_status, "invalid")
        self.assertEqual(result.syntax_status, "valid")
        self.assertEqual(result.spider_hardness, classify_sql(sql).spider_hardness)

    def test_dual_hardness(self):
        cases = [
            ("SELECT a FROM t", "easy", "easy"),
            ("SELECT COUNT(*) FROM (SELECT * FROM endowment WHERE amount > 8.5 "
             "GROUP BY school_id HAVING COUNT(*) > 1)", "easy", "hard"),
            ("SELECT a FROM (SELECT a FROM (SELECT a FROM t) x) y", "easy", "extreme"),
            ("SELECT a FROM t WHERE a IN (SELECT b FROM u)", "hard", "hard"),
            ("SELECT a, b FROM t WHERE a IN (SELECT b FROM u)", "extra", "extra"),
            ("SELECT a FROM t WHERE a > (SELECT AVG(b) FROM u WHERE u.id = t.id)",
             "hard", "extreme"),
            ("SELECT a FROM t WHERE a > (SELECT AVG(b) FROM u AS t WHERE t.id = 1)",
             "hard", "hard"),
            ("SELECT a FROM t WHERE a IN (SELECT b FROM u) "
             "AND a > (SELECT AVG(c) FROM v)", "extra", "extreme"),
            ("SELECT a FROM t UNION SELECT b FROM u", "hard", "hard"),
            ("SELECT a FROM (SELECT t.a AS a FROM t JOIN u ON t.id = u.id "
             "JOIN v ON t.id = v.id JOIN w ON t.id = w.id) x", "easy", "hard"),
            ("SELECT a FROM (SELECT t.a AS a FROM t JOIN u ON t.id = u.id "
             "JOIN v ON t.id = v.id JOIN w ON t.id = w.id JOIN z ON t.id = z.id) x",
             "easy", "extreme"),
        ]
        for sql, spider, extended in cases:
            with self.subTest(sql=sql):
                result = classify_sql(sql, validate_schema=False)
                self.assertEqual(result.classification_status, "classified")
                self.assertEqual(result.spider_hardness, spider)
                self.assertFalse(hasattr(result, "hardness"))
                self.assertEqual(result.extended_hardness, extended)

    def test_dual_hardness_unavailable(self):
        broken = classify_sql("SELECT FROM")
        self.assertIsNone(broken.spider_hardness)
        self.assertIsNone(broken.extended_hardness)
        for sql in ("SELECT ROW_NUMBER() OVER () FROM t",
                    "SELECT a FROM (SELECT ROW_NUMBER() OVER () AS a FROM t) x",
                    "SELECT COUNT(*) >= 5 FROM t",
                    "SELECT a FROM t OFFSET 1"):
            with self.subTest(sql=sql):
                result = classify_sql(sql)
                self.assertIsNone(result.spider_hardness)
                self.assertIsNotNone(result.extended_hardness)
                self.assertEqual(result.classification_status, "extended")

    def test_unavailable_ddl(self):
        for ddl in (None, "", "nonsense", "CREATE TABLE t AS SELECT a FROM u",
                    "CREATE TABLE t (a INT); ALTER TABLE t ADD b INT"):
            with self.subTest(ddl=ddl):
                self.assertEqual(classify_sql("SELECT a FROM t", ddl_statement=ddl,
                                              validate_schema=True).schema_status, "unavailable")

    def test_schema_resolution(self):
        ddl = "CREATE TABLE users (id INT, name TEXT); CREATE TABLE admins (user_id INT)"
        cases = {
            "SELECT u.name FROM users u JOIN admins a ON u.id = a.user_id": "valid",
            "SELECT u.missing FROM users u": "invalid",
            "SELECT * FROM missing": "invalid",
            "SELECT COUNT(*) FROM missing": "invalid",
            "SELECT name AS n FROM users ORDER BY n": "valid",
            "WITH names AS (SELECT name FROM users) SELECT name FROM names": "valid",
            "WITH names AS (SELECT missing FROM users) SELECT * FROM names": "invalid",
            "SELECT x.name FROM (SELECT name FROM users) x": "valid",
            "SELECT x.bad FROM (SELECT name FROM users) x": "invalid",
            "SELECT name FROM users u WHERE EXISTS (SELECT user_id FROM admins a "
            "WHERE a.user_id = u.id)": "valid",
            "SELECT id FROM users a JOIN users b ON a.id = b.id": "invalid",
            "SELECT a.* FROM users a": "valid",
            "SELECT missing.* FROM users": "invalid",
        }
        for sql, expected in cases.items():
            with self.subTest(sql=sql):
                result = classify_sql(sql, ddl_statement=ddl, validate_schema=True)
                self.assertEqual(result.schema_status, expected, result.schema_errors)

    def test_dataset_string_quoting_requires_explicit_dialect(self):
        sql = 'SELECT name FROM users WHERE name = "Alice"'
        ddl = 'CREATE TABLE users (name TEXT)'
        self.assertEqual(classify_sql(sql, dialect="mysql", ddl_statement=ddl,
                                      validate_schema=True).schema_status, "valid")
        self.assertEqual(classify_sql(sql, dialect="postgres", ddl_statement=ddl,
                                      validate_schema=True).schema_status, "invalid")

    def test_schema_and_catalog_names(self):
        ddl = "CREATE TABLE sales.users (id INT); CREATE TABLE audit.users (name TEXT)"
        for sql, expected in (
            ("SELECT id FROM sales.users", "valid"),
            ("SELECT sales.users.id FROM sales.users", "valid"),
            ("SELECT wrong.users.id FROM sales.users", "invalid"),
            ("SELECT id FROM wrong.users", "invalid"),
            ("SELECT id FROM users", "invalid"),
        ):
            with self.subTest(sql=sql):
                result = classify_sql(sql, ddl_statement=ddl, validate_schema=True)
                self.assertEqual(result.schema_status, expected, result.schema_errors)

    def test_quoted_identifiers(self):
        ddl = 'CREATE TABLE "Users" ("Name" TEXT)'
        result = classify_sql('SELECT "Name" FROM "Users"', dialect="postgres",
                              ddl_statement=ddl, validate_schema=True)
        self.assertEqual(result.schema_status, "valid", result.schema_errors)
        result = classify_sql('SELECT name FROM "Users"', dialect="postgres",
                              ddl_statement=ddl, validate_schema=True)
        self.assertEqual(result.schema_status, "invalid")

    def test_mysql_column_case_is_insensitive(self):
        for sql in ("SELECT NAME FROM users", "SELECT `NAME` FROM users",
                    "SELECT u.NAME FROM users AS u",
                    "SELECT name AS Label FROM users ORDER BY LABEL"):
            with self.subTest(sql=sql):
                result = classify_sql(sql, dialect="mysql",
                                      ddl_statement="CREATE TABLE users (Name TEXT)",
                                      validate_schema=True)
                self.assertEqual(result.schema_status, "valid", result.schema_errors)

    def test_batch_preserves_rows_and_reports_errors(self):
        rows = [{"answer": "SELECT a FROM t", "context": "CREATE TABLE t (a INT)"},
                {"answer": "SELECT (", "context": ""},
                {"answer": "SELECT a FROM t"}]
        results = list(classify_records(iter(rows), validate_schema=True))
        self.assertEqual([row["row_index"] for row in results], [0, 1, 2])
        self.assertEqual(results[1]["syntax_status"], "invalid")
        self.assertEqual(results[2]["schema_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
