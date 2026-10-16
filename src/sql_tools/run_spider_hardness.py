"""Classify SQL hardness in a Hugging Face or local dataset without LLM calls."""

import argparse
import logging
from collections import Counter
from collections.abc import Mapping
from itertools import islice
import json
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sql_tools.classifier import classify_sql
    from sql_tools.run_sql_validation import _load_dataset
    from sql_tools.validation import set_row_dialect
else:
    from .classifier import classify_sql
    from .run_sql_validation import _load_dataset
    from .validation import set_row_dialect
from sql_tools.logging_utils import get_console_logger, parse_log_level

DRY_RUN_ROWS = 20
_HARDNESS_ORDER = ("easy", "medium", "hard", "extra", "extreme")


def _log_hardness_summary(logger, summary, *, destination=None):
    """Log counts and shares. Hardness shares use classified rows as the base."""
    processed = summary["processed"]
    classified = summary["classified"]

    def share(count, total):
        return f"{count / total:.2%}" if total else "0.00%"

    title = "Dry-run summary" if summary["dry_run"] else "Hardness summary"
    logger.info(title)
    logger.info(
        f"Processed: {processed:,}; classified: {classified:,} ({share(classified, processed)}); "
        f"unclassified: {summary['unclassified']:,} ({share(summary['unclassified'], processed)})"
    )
    logger.info(
        f"Spider labeled: {summary.get('spider_labeled', classified):,}; "
        f"extended only: {summary.get('extended_only', 0):,}"
    )
    for label, key, base in (
            ("Spider hardness", "spider_hardness", summary.get("spider_labeled", classified)),
            ("Extended hardness", "extended_hardness", classified),
    ):
        counts = summary[key]
        logger.info(f"{label}:")
        names = [name for name in _HARDNESS_ORDER if name in counts]
        names.extend(name for name in counts if name not in _HARDNESS_ORDER)
        if not names:
            logger.info("  none")
            continue
        width = max(len(str(name)) for name in names)
        for name in names:
            count = counts[name]
            logger.info(f"  {name:<{width}}  {count:,} ({share(count, base)})")
    if destination is not None:
        logger.info(f"Rows saved to {destination}")


def run_spider_hardness(dataset_id: str, output: str | Path,
                       max_rows: int | None = None, *, dialect: str = "generic",
                       dry_run: bool = False, override: bool = True,
                       foreground: bool = False, log_level: int = logging.INFO) -> dict:
    """Classify answer fields across all splits, preserving existing row metadata.

    None processes every row; zero processes none, matching run_sql_validation.
    Classification fields are updated and the outcome is appended to an
    existing status_history. Validation fields are preserved. No database
    access or SQL execution occurs.
    Dry-run classifies at most 20 rows and writes no result files or directories.
    """
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be non-negative or None")
    logger = get_console_logger("Spider Hardness", level=log_level)
    destination = Path(output).expanduser()
    logger.info(
        f"Parameters: dataset_id={dataset_id}; output={destination}; "
        f"max_rows={max_rows}; dialect={dialect}; dry_run={dry_run}; "
        f"override={override}; foreground={foreground}; "
        f"log_level={logging.getLevelName(log_level)}"
    )
    paths = {name: destination / f"{name}.json" for name in
             ("classified_rows", "unclassified_rows", "summary")}
    if not dry_run and not override:
        for path in paths.values():
            if path.exists():
                raise FileExistsError(f"Result already exists: {path}")
    dataset = _load_dataset(dataset_id)
    splits = dataset if isinstance(dataset, Mapping) else {"data": dataset}

    def iter_rows():
        for split_name, split in splits.items():
            for index, source in enumerate(split):
                row = dict(source)
                row["hardness_source"] = {"split": split_name, "row_index": index}
                yield row

    rows = iter_rows()
    if dry_run:
        max_rows = min(max_rows, DRY_RUN_ROWS) if max_rows is not None else DRY_RUN_ROWS
    if max_rows is not None:
        rows = islice(rows, max_rows)
    available_rows = sum(len(split) for split in splits.values())
    total_rows = min(available_rows, max_rows) if max_rows is not None else available_rows
    logger.info(f"Rows to process: {total_rows:,} (available: {available_rows:,})")
    classified, unclassified = [], []
    counts = Counter()
    extended_counts = Counter()
    statuses = Counter()
    for index, row in enumerate(rows, 1):
        if dry_run:
            logger.info(f"Processing row {index:,}")
        elif index % 10_000 == 0:
            logger.info(f"Processing {index:,} of {total_rows:,}")
        sql = row.get("answer")
        if not isinstance(sql, str) or not sql.strip():
            fields = {
                "classification_status": "not_classified",
                "spider_hardness": None, "extended_hardness": None,
                "classification_errors": ["Row requires a non-empty answer string"],
                "component1": None, "component2": None, "others": None,
            }
        else:
            # "generic" is the CLI name; SQLGlot selects that dialect with None.
            parser_dialect = None if dialect == "generic" else dialect
            result = classify_sql(sql, dialect=parser_dialect, validate_schema=False)
            fields = {key: getattr(result, key) for key in (
                "spider_hardness", "extended_hardness",
                "classification_status", "classification_errors",
                "component1", "component2", "others")}
            if result.classification_status == "not_classified":
                fields["classification_errors"] = list(result.syntax_errors)
        row.update(fields)
        set_row_dialect(row, dialect)
        if "status_history" in row:
            row["status_history"] = list(row["status_history"] or []) + [row["classification_status"]]
        statuses[row["classification_status"]] += 1
        if row.get("extended_hardness"):
            classified.append(row)
            if row.get("spider_hardness"):
                counts[row["spider_hardness"]] += 1
            extended_counts[row["extended_hardness"]] += 1
        else:
            unclassified.append(row)
    summary = {
        "dataset_id": str(dataset_id), "dialect": dialect, "dry_run": dry_run,
        "processed": len(classified) + len(unclassified),
        "classified": len(classified), "unclassified": len(unclassified),
        "spider_labeled": sum(counts.values()),
        "extended_only": len(classified) - sum(counts.values()),
        "classification_status": dict(statuses),
        "spider_hardness": dict(counts), "extended_hardness": dict(extended_counts),
    }
    if dry_run:
        _log_hardness_summary(logger, summary)
        return summary
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in {"classified_rows": classified,
                       "unclassified_rows": unclassified, "summary": summary}.items():
        with paths[name].open("w" if override else "x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    _log_hardness_summary(logger, summary, destination=destination)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", "--dataset_id", required=True,
                        help="Hugging Face dataset ID, saved dataset directory, or local data file")
    parser.add_argument("--max-rows", "--max_rows", type=int, default=None,
                        help="Maximum rows across all splits (default: all)")
    parser.add_argument("--output", type=Path, required=True, help="Result directory")
    parser.add_argument("--dialect", default="generic", help="SQL dialect (default: generic)")
    parser.add_argument("--dry-run", "--dry_run", action="store_true",
                        help="Classify at most 20 rows without writing result files")
    parser.add_argument(
        "--override",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Replace every existing result file (default). "
        "--no-override refuses when any of them already exist.",
    )
    parser.add_argument("--foreground", action="store_true",
                        help="Stay in this process. Shell launchers then skip nohup (default: false)")
    parser.add_argument(
        "--log-level", "--log_level", type=parse_log_level, default=logging.INFO,
        help="Console log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL (default: INFO)",
    )
    args = parser.parse_args(argv)
    run_spider_hardness(args.dataset_id, args.output, args.max_rows,
                       dialect=args.dialect, dry_run=args.dry_run, override=args.override,
                       foreground=args.foreground, log_level=args.log_level)


if __name__ == "__main__":
    main()
