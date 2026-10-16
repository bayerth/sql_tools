"""Rewrite double-quoted SQL string literals to single quotes.

The sql_context copy stores MySQL string values in double quotes. The generic
SQLGlot dialect reads those quotes as identifiers, so validation reports a
missing column. This rewrite touches only the answer field. Question text and
DDL stay unchanged. An apostrophe inside a literal is doubled. A double-quoted
name directly after FROM is left alone, because those three rows use it as a
table identifier rather than a string.
"""

import argparse
import json
import re
from pathlib import Path

_QUOTED = re.compile(r'"([^"]*)"')
_FROM_QUOTED_TABLE = re.compile(r'\bFROM\s+"[^"]*"', re.IGNORECASE)


def single_quote_literals(sql: str) -> str:
    """Return SQL with paired double-quoted literals written as single quotes."""
    if '"' not in sql:
        return sql
    protected = []

    def hold(match):
        protected.append(match.group(0))
        return f"\x00FROMQ{len(protected) - 1}\x00"

    shielded = _FROM_QUOTED_TABLE.sub(hold, sql)

    def replace(match):
        return "'" + match.group(1).replace("'", "''") + "'"

    converted = _QUOTED.sub(replace, shielded)
    for index, original in enumerate(protected):
        converted = converted.replace(f"\x00FROMQ{index}\x00", original)
    return converted


def rewrite_dataset(dataset_id: str | Path, output: str | Path) -> dict:
    """Load a saved dataset and write one JSON list of rewritten rows."""
    from datasets import load_from_disk

    source = Path(dataset_id).expanduser()
    destination = Path(output).expanduser()
    if destination.exists():
        raise FileExistsError(f"Result already exists: {destination}")
    dataset = load_from_disk(str(source))
    splits = dataset if isinstance(dataset, dict) else {"train": dataset}
    rows = []
    changed = apostrophes = preserved_tables = 0
    for split in splits.values():
        for source_row in split:
            row = dict(source_row)
            answer = row.get("answer") or ""
            rewritten = single_quote_literals(answer)
            if rewritten != answer:
                changed += 1
                body = _FROM_QUOTED_TABLE.sub("", answer)
                apostrophes += sum(literal.count("'") for literal in _QUOTED.findall(body))
                preserved_tables += len(_FROM_QUOTED_TABLE.findall(rewritten))
            row["answer"] = rewritten
            rows.append(row)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {
        "rows": len(rows),
        "changed": changed,
        "apostrophes_doubled": apostrophes,
        "preserved_from_tables": preserved_tables,
        "output": str(destination),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-id",
        "--dataset_id",
        default="data/sql_context_copy",
        help="Saved dataset directory (default: data/sql_context_copy)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/sql/sql_context_copy_single_quoted.json"),
        help="JSON file to write (default: data/sql/sql_context_copy_single_quoted.json)",
    )
    args = parser.parse_args(argv)
    summary = rewrite_dataset(args.dataset_id, args.output)
    print(
        f"Wrote {summary['rows']:,} rows to {summary['output']}; "
        f"changed {summary['changed']:,}; "
        f"doubled apostrophes in {summary['apostrophes_doubled']:,} spots; "
        f"kept {summary['preserved_from_tables']:,} quoted FROM tables"
    )


if __name__ == "__main__":
    main()
