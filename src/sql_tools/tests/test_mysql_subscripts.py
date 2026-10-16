import unittest

from sql_tools import classify_sql, validate_sql
from sql_tools.parsing import is_valid_sql


class MySQLSubscriptTests(unittest.TestCase):
    def test_split_column_names_do_not_validate_as_subscript(self):
        sql = ('SELECT isin FROM table_21692771_1 WHERE coupon = "1.02" '
               'AND amount_issued_[€] = "447,000,000"')
        ddl = ('CREATE TABLE table_21692771_1 '
               '(isin VARCHAR, coupon VARCHAR, amount_issued_ VARCHAR, € VARCHAR)')
        result = validate_sql(sql, ddl_statement=ddl, dialect="mysql")
        self.assertEqual(result.syntax_status, "invalid")
        self.assertEqual(result.status_history, ["validation_failed"])
        self.assertIn("bracket subscripts", result.syntax_errors[0])
        self.assertFalse(is_valid_sql(sql, dialect="mysql"))
        self.assertEqual(classify_sql(sql, dialect="mysql").syntax_status, "invalid")

    def test_quoted_column_is_checked_as_one_identifier(self):
        sql = "SELECT `amount_issued_[€]` FROM t"
        result = validate_sql(sql, ddl_statement="CREATE TABLE t (`amount_issued_[€]` VARCHAR(30))",
                              dialect="mysql")
        self.assertEqual(result.status_history, ["validated"])
        result = validate_sql(sql, ddl_statement="CREATE TABLE t (amount_issued_ VARCHAR(30), € INT)",
                              dialect="mysql")
        self.assertEqual(result.schema_status, "invalid")

    def test_json_paths_and_other_dialects_are_unaffected(self):
        for sql in ("SELECT data->'$[0]' FROM t", "SELECT JSON_EXTRACT(data, '$[0]') FROM t",
                    "SELECT '[€]' FROM t"):
            with self.subTest(sql=sql):
                self.assertTrue(is_valid_sql(sql, dialect="mysql"))
        self.assertTrue(is_valid_sql("SELECT values[1] FROM t", dialect="postgres"))
