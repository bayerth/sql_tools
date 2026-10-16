"""Verify independent validation and reuse of parsing by classification."""

import logging
import unittest
from unittest.mock import patch

from sql_tools import ValidationResult, classify_sql, validate_sql, validate_sql_statements
from sql_tools.parsing import parse_statement
from sql_tools.dataset_validation import check_sql_statements


class ValidationTests(unittest.TestCase):
    def test_batch_validation_limits_and_history(self):
        rows = [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)",
                 "dialect": "mysql", "status_history": ["extended"]},
                {"answer": "SELECT (", "context": None}]
        result, invalid = validate_sql_statements(iter(rows), max_rows=1)
        self.assertEqual(invalid, [])
        self.assertIs(result[0], rows[0])
        self.assertEqual(result[0]["status_history"], ["extended", "validated"])
        self.assertNotIn("status_history", rows[1])
        self.assertEqual(validate_sql_statements(rows, max_rows=0), ([], []))
        with self.assertRaises(ValueError):
            validate_sql_statements(rows, max_rows=-1)
        valid, invalid = validate_sql_statements(rows)
        self.assertEqual(valid, [rows[0]])
        self.assertIs(invalid[0], rows[1])
        self.assertEqual(invalid[0]["status_history"], ["validation_failed"])

    def test_mysql_auto_increment_constraint_is_valid(self):
        ddl = "CREATE TABLE head (age INTEGER, id INTEGER PRIMARY KEY AUTO_INCREMENT)"
        result = validate_sql("SELECT age FROM head", ddl_statement=ddl, dialect="mysql")
        self.assertEqual(result.ddl_syntax_status, "valid")
        self.assertEqual(result.schema_status, "valid")
        self.assertEqual(result.status_history, ["validated"])
        self.assertIsNotNone(parse_statement(ddl, dialect="mysql"))

    def test_validation_history_preserves_events_and_requested_checks(self):
        history = ["extended", "repaired"]
        for sql, ddl, schema, outcome in (
            ("SELECT id FROM t", "CREATE TABLE t (id INT)", True, "validated"),
            ("SELECT absent FROM t", "CREATE TABLE t (id INT)", True, "validation_failed"),
            ("SELECT absent FROM t", "CREATE TABLE t (id INT)", False, "validated"),
            ("SELECT id FROM t", "CREATE TABLE t (", False, "validation_failed"),
            ("SELECT (", None, False, "validation_failed"),
            ("SELECT 1", None, False, "validated"),
            ("SELECT 1", None, True, "validation_failed"),
        ):
            with self.subTest(sql=sql, ddl=ddl, schema=schema):
                result = validate_sql(sql, ddl_statement=ddl, validate_schema=schema,
                                      status_history=history)
                self.assertEqual(result.status_history, history + [outcome])
                self.assertEqual(result.to_dict()["status_history"], history + [outcome])
        self.assertEqual(history, ["extended", "repaired"])

    def test_dataset_validation_appends_on_repeated_runs(self):
        rows = [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)",
                 "status_history": ["extended"]}]
        for _ in range(2):
            check_sql_statements(rows, logger=logging.getLogger("validation-test"))
        self.assertEqual(rows[0]["status_history"], ["extended", "validated", "validated"])

    def test_validation_does_not_call_spider(self):
        with patch("sql_tools.classifier.to_spider_sql", side_effect=AssertionError("Unexpected classification")):
            result = validate_sql(
                "WITH x AS (SELECT id FROM t) SELECT id FROM x",
                ddl_statement="CREATE TABLE t (id INT)",
            )
        self.assertIsInstance(result, ValidationResult)
        self.assertEqual(result.schema_status, "valid")
        self.assertNotIn("hardness", result.to_dict())

    def test_classification_parses_once_and_preserves_validation(self):
        options = {"ddl_statement": "CREATE TABLE t (id INT)", "dialect": "mysql"}
        with patch("sql_tools.validation.parse_statement", wraps=parse_statement) as parse:
            result = classify_sql("SELECT id FROM t", **options)
        parse.assert_called_once()
        self.assertEqual(result.spider_hardness, "easy")
        expected = validate_sql("SELECT id FROM t", **options).to_dict()
        self.assertEqual({key: result.to_dict()[key] for key in expected}, expected)

    def test_invalid_ddl_skips_resolution_in_validation_api(self):
        with patch("sql_tools.validation.validate_sql_schema") as schema:
            result = validate_sql("SELECT id FROM t", ddl_statement="CREATE TABLE t (")
        schema.assert_not_called()
        self.assertEqual(result.syntax_status, "valid")
        self.assertEqual(result.ddl_syntax_status, "invalid")
        self.assertEqual(result.schema_status, "unavailable")

    def test_dataset_validation_without_schema_still_checks_ddl(self):
        rows = [
            {"answer": "SELECT unknown FROM missing", "context": "CREATE TABLE t (id INT)"},
            {"answer": "SELECT id FROM t", "context": "CREATE TABLE t ("},
            {"answer": "SELECT (", "context": "CREATE TABLE t (id INT)"},
        ]
        with patch("sql_tools.classifier.to_spider_sql", side_effect=AssertionError("Unexpected classification")):
            correct, sql_errors, ddl_errors, schema_errors = check_sql_statements(
                rows, validate_schema=False, logger=logging.getLogger("validation-test")
            )
        self.assertEqual(correct, [rows[0]])
        self.assertEqual(sql_errors, [rows[2]])
        self.assertEqual(ddl_errors, [rows[1]])
        self.assertEqual(schema_errors, [])
        self.assertEqual([row["status_history"] for row in rows],
                         [["validated"], ["validation_failed"], ["validation_failed"]])

    def test_dataset_validation_accepts_valid_non_spider_query(self):
        rows = [{"text": [
            {"role": "system", "content": "CREATE TABLE t (id INT)"},
            {"role": "assistant", "content": "WITH x AS (SELECT id FROM t) SELECT id FROM x"},
        ]}]
        correct, *failures = check_sql_statements(
            rows, text_field="text", openai_prompt_format=True,
            logger=logging.getLogger("validation-test"),
        )
        self.assertEqual(correct, rows)
        self.assertEqual(failures, [[], [], []])
