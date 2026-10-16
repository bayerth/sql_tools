"""Translate a conservative SQL subset to Spider's SQL dictionary.

This representation is for hardness classification only, not exact matching.
Column IDs are symbolic: schema existence never affects the translation.
Unsupported constructs raise instead of silently producing an easy/extra label.
"""

from sqlglot import exp

from .parsing import UnsupportedSQL
from .spider_hardness import AGG_OPS, WHERE_OPS

_AGGREGATES = {exp.Max: "max", exp.Min: "min", exp.Count: "count",
               exp.Sum: "sum", exp.Avg: "avg"}
_ARITHMETIC = {exp.Sub: 1, exp.Add: 2, exp.Mul: 3, exp.Div: 4}
_COMPARISONS = {exp.Between: "between", exp.EQ: "=", exp.GT: ">",
                exp.LT: "<", exp.GTE: ">=", exp.LTE: "<=", exp.NEQ: "!=",
                exp.In: "in", exp.Like: "like", exp.Is: "is"}


def _unwrap(node):
    while isinstance(node, (exp.Paren, exp.Alias)):
        node = node.this
    return node


def _check_args(node, allowed, *, relaxed=False):
    if relaxed:
        return
    ignored = {key for key, value in node.args.items()
               if value is not None and value is not False and value != []
               and key not in allowed}
    if ignored:
        raise UnsupportedSQL(f"Unsupported {node.key} options: {', '.join(sorted(ignored))}")


def _opaque_column(aggregate=0, distinct=False):
    """One ordinary column standing in for an expression Spider cannot store."""
    return aggregate, "<expr>", distinct


def _column(node, *, relaxed=False):
    node = _unwrap(node)
    aggregate = 0
    distinct = False
    if type(node) in _AGGREGATES:
        _check_args(node, {"this", "expressions", "big_int"}, relaxed=relaxed)
        if node.expressions:
            if not relaxed:
                raise UnsupportedSQL("Multi-argument aggregate")
            return _opaque_column(AGG_OPS.index(_AGGREGATES[type(node)]), False)
        aggregate = AGG_OPS.index(_AGGREGATES[type(node)])
        node = node.this
        if isinstance(node, exp.Distinct):
            if len(node.expressions) != 1:
                if not relaxed:
                    raise UnsupportedSQL("Multi-column DISTINCT aggregate")
                return _opaque_column(aggregate, True)
            distinct = True
            node = node.expressions[0]
    if not isinstance(node, (exp.Column, exp.Star)):
        if relaxed:
            return _opaque_column(aggregate, distinct)
        description = node.sql() if node is not None else "<empty>"
        raise UnsupportedSQL(f"Unsupported column expression: {description}")
    if isinstance(node, exp.Star):
        _check_args(node, set(), relaxed=relaxed)
    elif isinstance(node.this, exp.Star):
        _check_args(node.this, set(), relaxed=relaxed)
    return aggregate, node.sql(), distinct


def _value_unit(node, *, relaxed=False):
    node = _unwrap(node)
    if type(node) in _ARITHMETIC:
        return _ARITHMETIC[type(node)], _column(node.this, relaxed=relaxed), _column(node.expression, relaxed=relaxed)
    return 0, _column(node, relaxed=relaxed), None


def _value(node, *, relaxed=False):
    node = _unwrap(node)
    if isinstance(node, exp.Subquery):
        return to_spider_sql(node.this, relaxed=relaxed)
    if isinstance(node, (exp.Select, exp.SetOperation)):
        return to_spider_sql(node, relaxed=relaxed)
    if isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
        return node.sql()
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return node.sql()
    return _column(node, relaxed=relaxed)


def _conditions(node, *, relaxed=False):
    if node is None:
        return []
    node = _unwrap(node)
    if isinstance(node, (exp.And, exp.Or)):
        return _conditions(node.this, relaxed=relaxed) + [node.key] + _conditions(node.expression, relaxed=relaxed)
    negated = isinstance(node, exp.Not)
    if negated:
        node = _unwrap(node.this)
    operator = _COMPARISONS.get(type(node))
    if operator is None:
        if not relaxed:
            raise UnsupportedSQL(f"Unsupported predicate: {node.sql()}")
        return [(negated, WHERE_OPS.index("="), (0, (0, "<expr>", False), None), "<expr>", None)]
    left = _value_unit(node.this, relaxed=relaxed)
    if isinstance(node, exp.Between):
        first, second = _value(node.args["low"], relaxed=relaxed), _value(node.args["high"], relaxed=relaxed)
    elif isinstance(node, exp.In):
        if node.args.get("query") is None:
            if not relaxed:
                raise UnsupportedSQL("IN value lists are outside the Spider parser subset")
            first, second = "<expr>", None
        else:
            first, second = _value(node.args["query"], relaxed=relaxed), None
    else:
        first, second = _value(node.expression, relaxed=relaxed), None
    return [(negated, WHERE_OPS.index(operator), left, first, second)]


def to_spider_sql(tree: exp.Expression, *, relaxed=False) -> dict:
    """Build the official dictionary shape or raise UnsupportedSQL.

    relaxed=True is only for extended_hardness. An expression Spider cannot
    store becomes one ordinary column, and clauses Spider cannot store (such
    as OFFSET) are ignored. The official spider_hardness path never sets this.
    """
    tree = _unwrap(tree)
    if isinstance(tree, exp.Subquery):
        return to_spider_sql(tree.this, relaxed=relaxed)
    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        _check_args(tree, {"this", "expression", "distinct"}, relaxed=relaxed)
        if tree.args.get("distinct") is False and not relaxed:
            raise UnsupportedSQL("ALL set operations are outside the Spider parser subset")
        left = to_spider_sql(tree.this, relaxed=relaxed)
        if any(left[key] is not None for key in ("union", "intersect", "except")):
            if not relaxed:
                raise UnsupportedSQL("Left-nested set operations cannot be represented faithfully")
            return left
        left[tree.key] = to_spider_sql(tree.expression, relaxed=relaxed)
        return left
    if not isinstance(tree, exp.Select):
        raise UnsupportedSQL(f"Spider hardness supports SELECT queries, not {tree.key}")
    _check_args(tree, {"expressions", "from_", "joins", "where", "group", "having",
                       "order", "limit", "distinct"}, relaxed=relaxed)
    distinct = tree.args.get("distinct")
    if distinct is not None and distinct.args.get("on") and not relaxed:
        raise UnsupportedSQL("DISTINCT ON")
    result = {"select": (distinct is not None, []),
              "from": {"table_units": [], "conds": []},
              "where": [], "groupBy": [], "having": [], "orderBy": [],
              "limit": None, "union": None, "intersect": None, "except": None}
    for item in tree.expressions:
        item = _unwrap(item)
        aggregate = 0
        if type(item) in _AGGREGATES:
            aggregate, column_id, is_distinct = _column(item, relaxed=relaxed)
            unit = (0, (0, column_id, is_distinct), None)
        else:
            unit = _value_unit(item, relaxed=relaxed)
            if not relaxed and (unit[1][0] or (unit[2] and unit[2][0])):
                raise UnsupportedSQL("Arithmetic over aggregates in SELECT")
        result["select"][1].append((aggregate, unit))
    sources = []
    if tree.args.get("from_"):
        sources.append(tree.args["from_"].this)
    for join in tree.args.get("joins", []):
        _check_args(join, {"this", "on", "side", "kind"}, relaxed=relaxed)
        sources.append(join.this)
        conditions = _conditions(join.args.get("on"), relaxed=relaxed)
        if conditions:
            if result["from"]["conds"]:
                result["from"]["conds"].append("and")
            result["from"]["conds"].extend(conditions)
    for source in sources:
        if isinstance(source, exp.Subquery):
            _check_args(source, {"this", "alias"}, relaxed=relaxed)
            result["from"]["table_units"].append(("sql", to_spider_sql(source.this, relaxed=relaxed)))
            continue
        if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
            if not relaxed:
                raise UnsupportedSQL("Table functions are outside the Spider parser subset")
            result["from"]["table_units"].append(("table_unit", source.sql() if source is not None else "<expr>"))
            continue
        _check_args(source, {"this", "db", "catalog", "alias"}, relaxed=relaxed)
        result["from"]["table_units"].append(("table_unit", source.sql()))
    for clause in ("where", "having"):
        node = tree.args.get(clause)
        result[clause] = _conditions(node.this if node else None, relaxed=relaxed)
    group = tree.args.get("group")
    if group:
        _check_args(group, {"expressions"}, relaxed=relaxed)
        result["groupBy"] = [_column(item, relaxed=relaxed) for item in group.expressions]
    order = tree.args.get("order")
    if order:
        _check_args(order, {"expressions"}, relaxed=relaxed)
        directions = {bool(item.args.get("desc")) for item in order.expressions}
        if len(directions) > 1 and not relaxed:
            raise UnsupportedSQL("Mixed ORDER BY directions")
        result["orderBy"] = (
            "desc" if True in directions else "asc",
            [_value_unit(item.this, relaxed=relaxed) for item in order.expressions],
        )
    limit = tree.args.get("limit")
    if limit:
        _check_args(limit, {"expression"}, relaxed=relaxed)
        value = limit.expression
        if not isinstance(value, exp.Literal) or not value.is_int:
            if not relaxed:
                raise UnsupportedSQL("Non-integer LIMIT")
            result["limit"] = 1
        else:
            result["limit"] = int(value.this)
    return result
