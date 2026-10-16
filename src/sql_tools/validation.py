"""SQL/DDL syntax and optional schema validation, independent of Spider."""

from dataclasses import asdict, dataclass, field
from collections.abc import Iterable, Sized
from itertools import islice
import logging

from sql_tools.logging_utils import get_console_logger

from sqlglot import exp
from sqlglot.dialects import Dialect
from sqlglot.errors import SqlglotError

from .ddl import check_ddl_syntax
from .parsing import UnsupportedSQL, parse_statement
from .schema import validate_sql_schema

logger = get_console_logger("SQL Validation", level=logging.INFO)


@dataclass
class ValidationResult:
    """Independent validation statuses and diagnostics for one statement."""

    syntax_status: str = "invalid"
    ddl_syntax_status: str = "not_checked"
    schema_status: str = "not_checked"
    syntax_errors: list[str] = field(default_factory=list)
    ddl_syntax_errors: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    dialect: str = "generic"
    status_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def set_row_dialect(row: dict, dialect: str | None) -> None:
    """Store one dialect on the row. Create it when missing, otherwise replace it."""
    row["dialect"] = dialect or "generic"
    row.pop("sql_dialect", None)


def _validate_sql(sql_statement: str, *, dialect: str | None = None,
                  ddl_statement: str | None = None,
                  validate_schema: bool = True,
                  status_history: list[str] | None = None,
                  max_rows: int | None = None
                  ) -> tuple[ValidationResult, exp.Expression | None]:
    """Return validation and its parsed query for internal reuse by classification."""
    result, tree = _check_sql(
            sql_statement, dialect=dialect, ddl_statement=ddl_statement,
            validate_schema=validate_schema,
            )
    # Success means every requested check passed; disabled checks are not failures.
    valid = (result.syntax_status == "valid"
             and (ddl_statement is None or result.ddl_syntax_status == "valid")
             and (not validate_schema or result.schema_status == "valid"))
    result.status_history = list(status_history or [])
    result.status_history.append("validated" if valid else "validation_failed")
    return result, tree


def _check_sql(sql_statement: str, *, dialect: str | None = None,
               ddl_statement: str | None = None, validate_schema: bool = True
               ) -> tuple[ValidationResult, exp.Expression | None]:
    """Run checks; the caller records an event even when parsing exits early."""
    Dialect.get_or_raise(dialect)
    result = ValidationResult(dialect=dialect or "generic")
    if ddl_statement is not None:
        result.ddl_syntax_status, errors = check_ddl_syntax(ddl_statement, dialect)
        result.ddl_syntax_errors = list(errors)
    try:
        tree = parse_statement(sql_statement, dialect=dialect)
    except UnsupportedSQL as error:
        result.syntax_status = "unsupported"
        result.syntax_errors.append(str(error))
        return result, None
    except SqlglotError as error:
        result.syntax_errors.append(str(error))
        return result, None
    except RecursionError:
        result.syntax_status = "unsupported"
        result.syntax_errors.append("Parser recursion limit exceeded")
        return result, None
    result.syntax_status = "valid"
    if validate_schema:
        if result.ddl_syntax_status != "valid":
            result.schema_status = "unavailable"
            result.schema_errors = [
                    "No DDL supplied" if ddl_statement is None else
                    "Schema validation skipped because DDL syntax is " + result.ddl_syntax_status
                    ]
        else:
            try:
                result.schema_status, result.schema_errors = validate_sql_schema(
                        tree, ddl_statement, dialect=dialect
                        )
            except RecursionError:
                result.schema_status = "unavailable"
                result.schema_errors = ["Schema analysis recursion limit exceeded"]
    return result, tree


def validate_sql(sql_statement: str,
                 *, dialect: str | None = None,
                 ddl_statement: str | None = None,
                 validate_schema: bool = True,
                 status_history: list[str] | None = None,
                 max_rows: int | None = None
                 ) -> ValidationResult:
    """Validate SQL and supplied DDL without executing SQL or classifying hardness.

    DDL syntax is always checked when supplied, even if SQL parsing fails.
    Invalid or unsupported DDL skips schema resolution. Disabling schema
    validation leaves schema_status=not_checked. A requested schema check
    without usable DDL yields unavailable. None dialect means generic SQLGlot.
    Syntax validity denotes parser acceptance, not guaranteed engine acceptance.
    Existing status_history is copied, then validated or validation_failed is
    appended to the result. The caller's list is not modified.
    """
    result, _ = _validate_sql(
            sql_statement, dialect=dialect,
            ddl_statement=ddl_statement, validate_schema=validate_schema,
            status_history=status_history,
            max_rows=max_rows
            )
    return result


def validate_sql_statements(rows: Iterable[dict], *, max_rows: int | None = None,
                            dialect: str | None = None, validate_schema: bool = True,
                            logger: logging.Logger = logger
                            ) -> tuple[list[dict], list[dict]]:
    """Validate rows in place and return (valid_rows, invalid_rows).

    Each list preserves input order. Invalid rows include checks that could not
    be completed, such as unsupported syntax or unavailable schema validation.

    None max_rows processes every row; zero processes none. An explicit dialect
    overrides row metadata; otherwise the row's dialect is used, then generic.
    The row stores that choice in dialect and drops a stale sql_dialect field.
    Every attempt appends a validation event while preserving earlier history.
    This function performs no dataset loading, file writing or LLM requests.
    """
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be non-negative or None")
    total = len(rows) if isinstance(rows, Sized) else None
    if total is not None and max_rows is not None:
        total = min(total, max_rows)
    valid_rows, invalid_rows = [], []
    valid = 0
    progress_total = (f"{total:,}" if total is not None else
                      f"up to {max_rows:,}" if max_rows is not None else "an unknown number of")
    logger.info(f"Starting SQL/DDL validation of {progress_total} rows")
    for index, row in enumerate(rows if max_rows is None else islice(rows, max_rows)):
        selected_dialect = dialect if dialect is not None else row.get("dialect")
        result = validate_sql(
                row["answer"], ddl_statement=row.get("context"),
                dialect=None if selected_dialect == "generic" else selected_dialect,
                validate_schema=validate_schema, status_history=row.get("status_history"),
                )
        row.update(result.to_dict())
        set_row_dialect(row, result.dialect)
        success = result.status_history[-1] == "validated"
        (valid_rows if success else invalid_rows).append(row)
        valid += int(success)
        logger.debug(f"Row {index + 1:,}: {result.to_dict()}")
        if (index + 1) % 10_000 == 0:
            logger.info(f"Processed: {index + 1:,}; valid: {valid:,}")
    total = len(valid_rows) + len(invalid_rows)
    logger.info(
            f"Processed: {total:,}; valid: {valid:,} ({valid / (total or 1):.2%}); "
            f"failed: {total - valid:,} ({(total - valid) / (total or 1):.2%})"
            )
    return valid_rows, invalid_rows
