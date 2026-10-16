"""Parse SQL without executing it or resolving database objects."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError


class UnsupportedSQL(ValueError):
    """The parser or Spider adapter cannot represent this SQL construct."""


def expression_errors(node: exp.Expression, dialect: str | None = None) -> list[str]:
    """Check AST completeness with a narrow SQLGlot 30.21 compatibility fix."""
    # The parser produces this argument-free marker for AUTO_INCREMENT, but its
    # inherited arg_types incorrectly requires `this`. The marker is complete.
    if isinstance(node, exp.AutoIncrementColumnConstraint) and not node.args:
        return []
    if dialect == "mysql":
        if isinstance(node, exp.Bracket):
            return [f"MySQL does not support bracket subscripts: {node.sql()}"]
        if isinstance(node, exp.Offset) and (
            node.parent is None or node.parent.args.get("limit") is None
        ):
            return ["MySQL OFFSET requires LIMIT in the same query block"]
        if isinstance(node, (exp.Limit, exp.Offset)):
            value = node.expression
            # Bare names can denote integer routine parameters; ? is a prepared
            # statement parameter. Their runtime values cannot be checked here.
            parameter = isinstance(value, exp.Placeholder) or (
                isinstance(value, exp.Column) and not value.table)
            integer = (isinstance(value, exp.Literal) and not value.is_string
                       and value.this.isascii() and value.this.isdigit())
            if not (parameter or integer):
                return [f"MySQL {node.key.upper()} requires a non-negative integer "
                        "or an integer routine/prepared-statement parameter"]
        if isinstance(node, exp.ColumnDef):
            kind = node.args.get("kind")
            if (isinstance(kind, exp.DataType)
                    and kind.this in (exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR,
                                      exp.DataType.Type.VARBINARY)
                    and not kind.expressions):
                return [f"MySQL requires an explicit length for column {node.name}: {kind.sql()}"]
    return node.error_messages()


def parse_statement(sql_statement: str, *, dialect: str | None = None) -> exp.Expression:
    """Parse exactly one statement. None selects SQLGlot's generic dialect.

    Acceptance is a parser-level syntax check, not an engine grammar guarantee.
    No table, column, type, or function existence checks happen here.
    """
    if not isinstance(sql_statement, str) or not sql_statement.strip():
        raise ParseError("Expected a non-empty SQL statement")
    statements = [item for item in sqlglot.parse(
        sql_statement, read=dialect, error_level=ErrorLevel.RAISE
    ) if item is not None]
    if len(statements) != 1:
        raise ParseError("Expected exactly one SQL statement")
    tree = statements[0]
    if isinstance(tree, exp.Command):
        raise UnsupportedSQL("SQLGlot returned an unparsed command")
    if not isinstance(tree, (exp.Query, exp.DML, exp.DDL)):
        raise ParseError("Expected a SQL query, DML, or DDL statement")
    for node in tree.walk():
        errors = expression_errors(node, dialect)
        if errors:
            raise ParseError("; ".join(errors))
        if isinstance(node, exp.Select) and not node.expressions:
            raise ParseError("SELECT requires at least one expression")
    return tree


def is_valid_sql(sql_statement: str, *, dialect: str | None = None) -> bool:
    """Return parser acceptance, independent of DDL and database contents.

    Unsupported commands return False; classify_sql provides separate statuses.
    An invalid dialect is a configuration error and raises ValueError.
    """
    from sqlglot.errors import SqlglotError

    try:
        parse_statement(sql_statement, dialect=dialect)
    except (SqlglotError, UnsupportedSQL):
        return False
    return True
