"""Public SQL classification API and streaming dataset helpers."""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .parsing import UnsupportedSQL, parse_statement
from .validation import ValidationResult, _validate_sql
from .spider_adapter import to_spider_sql
from .spider_hardness import Evaluator, count_component1, count_component2, count_others


@dataclass
class ClassificationResult(ValidationResult):
    """Validation plus the two hardness labels."""

    classification_status: str = "not_classified"
    spider_hardness: str | None = None
    extended_hardness: str | None = None
    classification_errors: list[str] = field(default_factory=list)
    component1: int | None = None
    component2: int | None = None
    others: int | None = None


def _extended_hardness(tree, spider_hardness):
    """Raise extended hardness for nesting the outer Spider score does not see.

    spider_hardness stays unchanged. A nested SELECT has another SELECT as an
    ancestor. It is extreme when any nested SELECT has depth 2 or more, when
    at least two nested SELECTs exist and one of them aggregates, when one
    nested SELECT is both correlated and aggregated, or when a nested SELECT
    is Spider extra on its own. Any other nesting is at least hard, and an
    outer Spider extra stays extra. No nested SELECT keeps the Spider label.
    """
    selects = list(tree.find_all(exp.Select))
    nested = [node for node in selects if node.find_ancestor(exp.Select) is not None]
    if not nested:
        return spider_hardness

    def depth(node):
        count = 0
        parent = node.find_ancestor(exp.Select)
        while parent is not None:
            count += 1
            parent = parent.find_ancestor(exp.Select)
        return count

    def source_names(node):
        sources = [join.this for join in node.args.get("joins", [])]
        if node.args.get("from_"):
            sources.append(node.args["from_"].this)
        return {source.alias_or_name for source in sources if source.alias_or_name}

    def is_correlated(node):
        local_names = source_names(node)
        outer_names = set()
        parent = node.find_ancestor(exp.Select)
        while parent is not None:
            outer_names.update(source_names(parent))
            parent = parent.find_ancestor(exp.Select)
        return any(column.table and column.table not in local_names
                   and column.table in outer_names
                   for column in node.find_all(exp.Column)
                   if column.find_ancestor(exp.Select) is node)

    def has_aggregation(node):
        return bool(node.args.get("group") or node.args.get("having")) or any(
            aggregate.find_ancestor(exp.Select) is node
            for aggregate in node.find_all(exp.AggFunc))

    def is_spider_extra(node):
        # Score this SELECT alone. Spider still ignores FROM-subqueries inside it.
        # A nested SELECT the official adapter rejects is scored with the
        # extended translation, which does not change the outer spider label.
        try:
            spider_sql = to_spider_sql(node)
        except (UnsupportedSQL, RecursionError):
            try:
                spider_sql = to_spider_sql(node, relaxed=True)
            except (UnsupportedSQL, RecursionError):
                return False
        return Evaluator().eval_hardness(spider_sql) == "extra"

    if (max(depth(node) for node in nested) >= 2
            or (len(nested) >= 2 and any(has_aggregation(node) for node in nested))
            or any(is_correlated(node) and has_aggregation(node) for node in nested)
            or any(is_spider_extra(node) for node in nested)):
        return "extreme"
    return "extra" if spider_hardness == "extra" else "hard"


def classify_sql(sql_statement: str, *, dialect: str | None = None,
                 ddl_statement: str | None = None,
                 validate_schema: bool = True,
                 status_history: list[str] | None = None
                 ) -> ClassificationResult:
    """Classify one SQL statement without any database access.

    None uses SQLGlot's permissive generic dialect, not dialect auto-detection.
    Syntax valid means parser acceptance, not a database grammar guarantee.
    When the official Spider adapter rejects a parsed statement, spider_hardness
    stays null and extended_hardness is still set. The two labels are comparable
    only when both are set. An expression Spider cannot store counts as one
    ordinary column for that extended label. Supplied DDL is syntax-checked
    even when validate_schema=False or SQL fails
    to parse. Invalid/unsupported DDL skips schema resolution. Schema and DDL
    errors do not affect structural hardness.
    """
    validation, tree = _validate_sql(
        sql_statement, dialect=dialect, ddl_statement=ddl_statement,
        validate_schema=validate_schema,
        status_history=status_history,
    )
    result = ClassificationResult(**validation.to_dict())
    if tree is None and dialect in (None, "generic"):
        # Backticks are identifiers in MySQL and fail the generic parser.
        try:
            tree = parse_statement(sql_statement, dialect="mysql")
        except (UnsupportedSQL, RecursionError, SqlglotError):
            tree = None
    if tree is None:
        return result
    try:
        spider_sql = to_spider_sql(tree)
        spider_hardness = Evaluator().eval_hardness(spider_sql)
        result.spider_hardness = spider_hardness
        result.extended_hardness = _extended_hardness(tree, spider_hardness)
        result.component1 = count_component1(spider_sql)
        result.component2 = count_component2(spider_sql)
        result.others = count_others(spider_sql)
        result.classification_status = "classified"
    except (UnsupportedSQL, RecursionError) as error:
        result.classification_errors.append(str(error))
        try:
            relaxed_sql = to_spider_sql(tree, relaxed=True)
            relaxed_label = Evaluator().eval_hardness(relaxed_sql)
            result.extended_hardness = _extended_hardness(tree, relaxed_label)
        except (UnsupportedSQL, RecursionError):
            result.extended_hardness = _extended_hardness(tree, "extra")
        result.classification_status = "extended"
    return result


def classify_records(records: Iterable[Mapping], *, sql_column: str = "answer",
                     ddl_column: str = "context", dialect: str | None = None,
                     validate_schema: bool = False
                     ) -> Iterator[dict]:
    """Yield one result per record, preserving order and zero-based row index.

    Defaults match sql-create-context-copy. Missing SQL fields are configuration
    errors; missing/empty DDL produces schema_status=unavailable when requested.
    """
    for index, record in enumerate(records):
        result = classify_sql(
                record[sql_column], dialect=dialect,
                ddl_statement=record.get(ddl_column),
                validate_schema=validate_schema,
                status_history=record.get("status_history"),
                )
        yield {"row_index": index, **result.to_dict()}
