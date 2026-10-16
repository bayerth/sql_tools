"""Independent, cached DDL syntax checks without schema resolution."""

from functools import lru_cache

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, SqlglotError

from .parsing import UnsupportedSQL, expression_errors


@lru_cache(maxsize=2048)
def check_ddl_syntax(ddl_statement: str, dialect: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Check one or more DDL statements and return immutable status/errors.

    This is parser acceptance plus basic CREATE TABLE completeness checks, not
    an engine grammar guarantee. References, duplicate definitions, and types
    are not resolved. Valid DDL may still be unsupported as schema metadata.
    None handling belongs to classify_sql; an empty supplied string is invalid.
    """
    try:
        if not isinstance(ddl_statement, str) or not ddl_statement.strip():
            raise ParseError("Expected non-empty DDL")
        statements = [statement for statement in sqlglot.parse(
            ddl_statement, read=dialect, error_level=ErrorLevel.RAISE
        ) if statement is not None]
        if not statements:
            raise ParseError("Expected at least one DDL statement")
        for statement in statements:
            if isinstance(statement, exp.Command):
                raise UnsupportedSQL("SQLGlot returned an unparsed DDL command")
            if not isinstance(statement, (exp.DDL, exp.Alter, exp.Drop,
                                          exp.TruncateTable, exp.Comment)):
                raise ParseError(f"Expected DDL, found {statement.key}")
            for node in statement.walk():
                errors = expression_errors(node, dialect)
                if errors:
                    raise ParseError("; ".join(errors))
                if isinstance(node, exp.Select) and not node.expressions:
                    raise ParseError("SELECT requires at least one expression")
            if isinstance(statement, exp.Create) and statement.args.get("kind") == "TABLE":
                has_source = (statement.args.get("expression") is not None
                              or statement.args.get("clone") is not None
                              or statement.find(exp.LikeProperty) is not None)
                definition = statement.this
                has_columns = isinstance(definition, exp.Schema) and bool(definition.expressions)
                if not has_columns and not has_source:
                    raise ParseError("CREATE TABLE requires column definitions, AS, LIKE, or CLONE")
                # SQLGlot accepts bare column names even where a type is required.
                if has_columns and not has_source and dialect != "sqlite":
                    for column in definition.expressions:
                        if isinstance(column, exp.Identifier) or (
                            isinstance(column, exp.ColumnDef) and column.args.get("kind") is None
                        ):
                            raise ParseError(f"Missing data type for column: {column.sql()}")
    except SqlglotError as error:
        return "invalid", (str(error),)
    except (UnsupportedSQL, RecursionError) as error:
        return "unsupported", (str(error),)
    return "valid", ()
