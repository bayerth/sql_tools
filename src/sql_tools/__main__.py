"""Classify a saved Hugging Face dataset, streaming results to JSONL."""

import argparse
from collections import Counter
import json
from pathlib import Path

from .classifier import classify_records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--split", help="Default: process every saved split")
    parser.add_argument("--sql-column", default="answer")
    parser.add_argument("--ddl-column", default="context")
    parser.add_argument("--dialect", default=None, help="Default: SQLGlot generic dialect")
    parser.add_argument("--validate-schema", action="store_true")
    args = parser.parse_args()

    from datasets import DatasetDict, load_from_disk
    from sqlglot.dialects import Dialect

    Dialect.get_or_raise(args.dialect)
    dataset = load_from_disk(str(args.dataset))
    splits = dict(dataset) if isinstance(dataset, DatasetDict) else {"data": dataset}
    if args.split:
        splits = {args.split: splits[args.split]}
    for split in splits.values():
        if args.sql_column not in split.column_names:
            parser.error(f"Missing SQL column: {args.sql_column}")
    counts = Counter()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        for split_name, split in splits.items():
            for result in classify_records(
                split, sql_column=args.sql_column, ddl_column=args.ddl_column,
                dialect=args.dialect, validate_schema=args.validate_schema,
            ):
                stream.write(json.dumps({"split": split_name, **result}, ensure_ascii=False) + "\n")
                counts[result["spider_hardness"] or result["classification_status"]] += 1
                if sum(counts.values()) % 5000 == 0:
                    print(f"Processed {sum(counts.values())} rows", flush=True)
    print(json.dumps(dict(counts), indent=2))


if __name__ == "__main__":
    main()
