"""Optional, cached DDL-based name resolution. No SQL is executed."""

from functools import lru_cache

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from .parsing import UnsupportedSQL


def _normalize(tree, dialect):
    tree = normalize_identifiers(tree, dialect=dialect)
    # MySQL column names are case-insensitive, including quoted names. SQLGlot's
    # default MySQL normalization is case-sensitive to preserve table names.
    if dialect == "mysql":
        for node in tree.walk():
            identifiers = []
            if isinstance(node, (exp.Column, exp.ColumnDef)):
                identifiers.append(node.this)
            elif isinstance(node, exp.Alias):
                identifiers.append(node.args.get("alias"))
            elif isinstance(node, exp.TableAlias):
                identifiers.extend(node.args.get("columns") or [])
            for identifier in identifiers:
                if isinstance(identifier, exp.Identifier):
                    identifier.set("this", identifier.name.lower())
    return tree


@lru_cache(maxsize=2048)
def _ddl_tables(ddl_statement: str, dialect: str | None):
    """Return immutable table/column metadata so cached objects cannot be mutated."""
    statements = sqlglot.parse(ddl_statement, read=dialect, error_level=ErrorLevel.RAISE)
    tables = {}
    for statement in statements:
        if statement is None:
            continue
        statement = _normalize(statement, dialect)
        if not (isinstance(statement, exp.Create)
                and statement.args.get("kind") == "TABLE"
                and isinstance(statement.this, exp.Schema)
                and isinstance(statement.this.this, exp.Table)):
            raise UnsupportedSQL("DDL must contain explicit CREATE TABLE column definitions only")
        table = statement.this.this
        name = tuple(part.name for part in table.parts)
        columns = tuple(item.name for item in statement.this.expressions
                        if isinstance(item, exp.ColumnDef))
        if not columns or len(set(columns)) != len(columns) or name in tables:
            raise UnsupportedSQL("Missing or duplicate DDL table/column definitions")
        if statement.args.get("expression") is not None:
            raise UnsupportedSQL("CREATE TABLE AS is not supported as schema metadata")
        tables[name] = columns
    if not tables:
        raise UnsupportedSQL("No CREATE TABLE definitions found")
    return tuple(tables.items())


def validate_sql_schema(tree: exp.Expression, ddl_statement: str | None, *,
                        dialect: str | None = None) -> tuple[str, list[str]]:
    """Check relation and column names against DDL, including scoped aliases.

    Returns valid, invalid, or unavailable. DDL is treated as the complete schema
    for this statement. Types, functions, permissions and data are not validated.
    Unqualified tables may resolve by a unique suffix; ambiguous names fail.
    """
    if not isinstance(ddl_statement, str) or not ddl_statement.strip():
        return "unavailable", ["No DDL supplied"]
    try:
        tables = dict(_ddl_tables(ddl_statement, dialect))
    except (SqlglotError, UnsupportedSQL) as error:
        return "unavailable", [str(error)]
    if not isinstance(tree, exp.Query):
        return "unavailable", ["Schema validation currently supports queries only"]
    query = _normalize(tree.copy(), dialect)
    scopes = traverse_scope(query)
    if not scopes:
        return "unavailable", ["No query scope could be built"]
    errors = []
    resolved = {}
    # Only physical table sources are checked; CTE/derived sources are scopes.
    try:
        for scope in scopes:
            for alias, (_, source) in scope.selected_sources.items():
                if not isinstance(source, exp.Table):
                    continue
                if not isinstance(source.this, exp.Identifier):
                    return "unavailable", ["Table functions are not supported by DDL validation"]
                name = tuple(part.name for part in source.parts)
                matches = [key for key in tables if len(key) >= len(name)
                           and key[-len(name):] == name]
                if len(matches) != 1:
                    reason = "Unknown" if not matches else "Ambiguous"
                    errors.append(f"{reason} table: {'.'.join(name)}")
                else:
                    resolved[id(source)] = (source, alias, matches[0])
        if errors:
            return "invalid", errors
        # Check explicit schema/catalog qualifiers before canonicalizing tables.
        for scope in scopes:
            for column in scope.columns:
                if not (column.db or column.catalog):
                    continue
                owner = scope
                while owner is not None and column.table not in owner.sources:
                    owner = owner.parent
                source = owner.sources.get(column.table) if owner else None
                entry = resolved.get(id(source))
                prefix = tuple(part.name for part in column.parts[:-1])
                if entry is None or entry[2][-len(prefix):] != prefix:
                    errors.append(f"Unknown qualified column: {column.sql()}")
                else:
                    column.set("db", None)
                    column.set("catalog", None)
        if errors:
            return "invalid", errors
        # A flat internal schema supports mixed table/schema/catalog DDL depths.
        mapping = {}
        for index, (source, alias, key) in enumerate(resolved.values()):
            internal_name = f"__ddl_table_{index}"
            mapping[internal_name] = {column: "UNKNOWN" for column in tables[key]}
            if not source.args.get("alias"):
                source.set("alias", exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
            source.set("this", exp.to_identifier(internal_name, quoted=True))
            source.set("db", None)
            source.set("catalog", None)
        qualify(query, dialect=dialect,
                schema=MappingSchema(mapping, dialect=dialect, normalize=False), infer_schema=False,
                validate_qualify_columns=True, allow_partial_qualification=False)
    except OptimizeError as error:
        return "invalid", [str(error)]
    except SqlglotError as error:
        return "unavailable", [str(error)]
    return "valid", []
