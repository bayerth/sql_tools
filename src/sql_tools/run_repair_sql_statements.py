"""Correct damaged MySQL examples with LM Studio and validate every proposal."""

import argparse
from copy import deepcopy
from itertools import islice
import json
import logging
import math
import os
from pathlib import Path
from time import perf_counter

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sql_tools.repair import request_repair
    from sql_tools.correct import (
        DEFAULT_SYSTEM_MESSAGE,
        BATCH_SYSTEM_MESSAGE,
        apply_correction,
        correction_payload,
        record_correction,
    )
    from sql_tools.run_expand_sql_statements import (
        CONTEXT_LENGTH,
        MODEL,
        LMSTUDIO_TOKEN,
        LMSTUDIO_MAC_URL,
        MAC_STUDIO_API_KEY,
        _read_rows,
        _normalize_row,
        _append_checkpoint,
    )
else:
    from .repair import request_repair
    from .correct import (
        DEFAULT_SYSTEM_MESSAGE,
        BATCH_SYSTEM_MESSAGE,
        apply_correction,
        correction_payload,
        record_correction,
    )
    from .run_expand_sql_statements import (
        CONTEXT_LENGTH,
        MODEL,
        LMSTUDIO_TOKEN,
        LMSTUDIO_MAC_URL,
        MAC_STUDIO_API_KEY,
        _read_rows,
        _normalize_row,
        _append_checkpoint,
    )
from rwu_llmconnector import LMStudioClient
from sql_tools.logging_utils import get_console_logger, parse_log_level

DRY_RUN_ROWS = 10
OUTCOMES = ("corrected", "pending", "ambiguous", "unchanged", "failed")


def _request_group(
    group,
    *,
    client,
    batch_mode,
    context_length,
    logger,
    require_source_confirmation=False,
):
    """Split oversized groups and yield (row, outcome) in input order."""
    system_message = BATCH_SYSTEM_MESSAGE if batch_mode else DEFAULT_SYSTEM_MESSAGE
    payload = (
        {
            "examples": [
                {"id": index, **payload} for index, (_, payload) in enumerate(group)
            ]
        }
        if batch_mode
        else group[0][1]
    )
    text = json.dumps(payload, ensure_ascii=False)
    # Budget for full SQL/DDL output plus the evidence and response envelope.
    max_tokens = sum(
        math.ceil(
            len(
                json.dumps(
                    {key: row[key] for key in ("answer", "context")}, ensure_ascii=False
                )
            )
            / 3
        )
        + 1024
        for row, _ in group
    )
    input_tokens = math.ceil((len(system_message) + len(text)) / 3) + 1024
    if input_tokens + max_tokens > context_length:
        if len(group) > 1:
            middle = len(group) // 2
            for part in (group[:middle], group[middle:]):
                yield from _request_group(
                    part,
                    client=client,
                    batch_mode=batch_mode,
                    context_length=context_length,
                    logger=logger,
                    require_source_confirmation=require_source_confirmation,
                )
        else:
            row = group[0][0]
            yield (
                row,
                record_correction(
                    row, "failed", {"error": "Estimated request exceeds context budget"}
                ),
            )
        return
    logger.info(f"Request: {len(group):,} rows; estimated max_tokens={max_tokens:,}")
    response_text = None
    try:
        response_text, *_ = request_repair(
            client,
            text,
            system_message=system_message,
            context_length=context_length,
            max_tokens=max_tokens,
            logger=logger,
        )
        decoded = json.loads(response_text)
        if batch_mode:
            results = decoded.get("results") if isinstance(decoded, dict) else None
            if not isinstance(results, list):
                raise ValueError("Batch response requires a results list")
            by_id = {}
            for item in results:
                if (
                    not isinstance(item, dict)
                    or type(item.get("id")) is not int
                    or not 0 <= item["id"] < len(group)
                    or item["id"] in by_id
                ):
                    raise ValueError("Invalid, unknown, or duplicate batch result id")
                by_id[item["id"]] = item
        else:
            by_id = {0: decoded}
    except Exception as error:
        logger.error(f"Correction request failed: {error}")
        for row, _ in group:
            yield (
                row,
                record_correction(
                    row,
                    "failed",
                    {
                        "error": f"LLM request/response failed: {error}",
                        "raw_response": response_text,
                    },
                ),
            )
        return
    for index, (row, _) in enumerate(group):
        if index not in by_id:
            yield (
                row,
                record_correction(
                    row, "failed", {"error": f"Missing batch result id {index}"}
                ),
            )
        else:
            yield (
                row,
                apply_correction(
                    row,
                    by_id[index],
                    require_source_confirmation=require_source_confirmation,
                ),
            )


def _output_row(row, include_history):
    """Return the row for JSON output, optionally without correction history."""
    if include_history:
        return row
    return {key: value for key, value in row.items() if key != "correction_history"}


def run_correct_sql_statements(
    input_path,
    output,
    *,
    client,
    openai_format=False,
    dry_run=False,
    batch_size=0,
    max_rows=0,
    checkpoints=False,
    checkpoint_size=100,
    context_length=CONTEXT_LENGTH,
    logger=None,
    require_source_confirmation=False,
    override=True,
    include_history=True,
    foreground=False,
    log_level=logging.INFO,
):
    """Correct rows without overwriting inputs; dry-run makes real LLM requests.

    Zero max_rows means all. Batch mode rounds limits and checkpoint intervals
    up to complete batches; dry-run is capped at DRY_RUN_ROWS. No automatic retries
    or checkpoint resume are performed. include_history writes correction_history;
    False omits that field from result and checkpoint files. override replaces
    every result and checkpoint file in the output directory; False refuses
    when any of them already exist.
    """
    if client is None:
        raise ValueError("client must be provided")
    if batch_size < 0 or max_rows < 0:
        raise ValueError("batch_size and max_rows must be non-negative")
    if checkpoint_size <= 0 or context_length <= 0:
        raise ValueError("checkpoint_size and context_length must be positive")

    logger = logger or get_console_logger("SQL Correction", level=log_level)
    destination = Path(output).expanduser()
    model = getattr(client, "model", None)
    model = model if isinstance(model, str) else "unknown"
    logger.info(
        f"Parameters: input={input_path}; output={destination}; "
        f"openai_format={openai_format}; dry_run={dry_run}; batch_size={batch_size}; "
        f"max_rows={max_rows}; checkpoints={checkpoints}; checkpoint_size={checkpoint_size}; "
        f"context_length={context_length}; require_source_confirmation={require_source_confirmation}; "
        f"model={model}; override={override}; include_history={include_history}; "
        f"foreground={foreground}; log_level={logging.getLevelName(log_level)}"
    )
    paths = {name: destination / f"correction_{name}_rows.json" for name in OUTCOMES}
    paths["stats"] = destination / "correction_stats.json"
    checkpoint_paths = {
        name: destination / f"correction_{name}_rows.tmp.jsonl" for name in OUTCOMES
    }
    save_checkpoints = checkpoints and not dry_run

    if not dry_run and not override:
        # Also protect interrupted runs when restarting without --checkpoints.
        for path in (*paths.values(), *checkpoint_paths.values()):
            if path.exists():
                raise FileExistsError(f"Result or checkpoint already exists: {path}")

    if save_checkpoints:
        destination.mkdir(parents=True, exist_ok=True)
        for path in checkpoint_paths.values():
            if override and path.exists():
                path.unlink()
            path.touch(exist_ok=False)

    started = perf_counter()
    limit = max_rows or None
    if limit is not None and batch_size:
        limit = math.ceil(limit / batch_size) * batch_size
    if dry_run:
        limit = min(limit, DRY_RUN_ROWS) if limit is not None else DRY_RUN_ROWS
    rows = _read_rows(input_path)
    if limit is not None:
        rows = islice(rows, limit)
    progress_total = f"up to {limit:,}" if limit is not None else "unknown total"
    width = batch_size or 1
    interval = math.ceil(checkpoint_size / width) * width
    indexed_rows = enumerate(rows)
    results = {name: [] for name in OUTCOMES}
    pending_checkpoints = {name: [] for name in OUTCOMES}
    processed = since_checkpoint = 0

    def collect(row, outcome):
        nonlocal processed, since_checkpoint
        written = _output_row(row, include_history)
        results[outcome].append(written)
        if save_checkpoints:
            pending_checkpoints[outcome].append(written)
        processed += 1
        since_checkpoint += 1
        logger.info(f"Row {row['correction_source']['row_index'] + 1:,}: {outcome}")
        if dry_run:
            logger.info(
                f"Correction attempt: {json.dumps(row['correction_history'][-1], ensure_ascii=False)}"
            )
        elif outcome == "failed":
            logger.error(
                row["correction_history"][-1].get("error", "Correction failed")
            )

    def flush_checkpoints():
        nonlocal since_checkpoint
        for name, buffer in pending_checkpoints.items():
            _append_checkpoint(checkpoint_paths[name], buffer)
            buffer.clear()
        since_checkpoint = 0
        logger.info(f"Checkpoint saved: {processed:,} rows")

    while chunk := list(islice(indexed_rows, width)):
        group = []
        for index, source in chunk:
            row = deepcopy(source)
            row["correction_source"] = {"input": str(input_path), "row_index": index}
            logger.info(f"Processing row {index + 1:,} of {progress_total}")
            try:
                _normalize_row(row, openai_format)
                if (row.get("dialect") or "mysql") not in (
                    None,
                    "mysql",
                    "generic",
                ):
                    raise ValueError("Identifier correction supports MySQL rows only")
                group.append((row, correction_payload(row)))
            except (ValueError, TypeError) as error:
                collect(
                    row,
                    record_correction(
                        row, "failed", {"error": f"Invalid input: {error}"}
                    ),
                )
        if group:
            for row, outcome in _request_group(
                group,
                client=client,
                batch_mode=batch_size > 0,
                context_length=context_length,
                logger=logger,
                require_source_confirmation=require_source_confirmation,
            ):
                collect(row, outcome)
        if save_checkpoints and since_checkpoint >= interval:
            flush_checkpoints()
    if save_checkpoints and since_checkpoint:
        flush_checkpoints()
    elapsed = perf_counter() - started
    stats = {
        "model": model,
        "dry_run": dry_run,
        "processed": processed,
        "require_source_confirmation": require_source_confirmation,
        "include_history": include_history,
        **{name: len(values) for name, values in results.items()},
        "elapsed_seconds": elapsed,
        "seconds_per_row": elapsed / processed if processed else 0,
    }
    logger.info(f"Correction summary: {json.dumps(stats)}")
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        logger.info(f"Saving results to {destination}")
        for name, data in {**results, "stats": stats}.items():
            with paths[name].open("w" if override else "x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        if save_checkpoints:
            for path in checkpoint_paths.values():
                path.unlink()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        help="JSON/JSONL file, saved dataset directory or HF dataset ID",
    )
    parser.add_argument("--output", required=True, help="Result directory")
    parser.add_argument("--openai-format", "--openai_format", action="store_true")
    parser.add_argument("--dry-run", "--dry_run", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument(
        "--require-source-confirmation",
        "--require_source_confirmation",
        action="store_true",
        help="Keep validated candidates pending when source confirmation is missing",
    )
    parser.add_argument("--batch-size", "--batch_size", type=int, default=0)
    parser.add_argument(
        "--max-rows",
        "--max_rows",
        type=int,
        default=0,
        help="0 means all; batch mode rounds up to a full batch",
    )
    parser.add_argument(
        "--context-length", "--context_length", type=int, default=CONTEXT_LENGTH
    )
    parser.add_argument(
        "--override",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Replace every existing result and checkpoint file (default). "
        "--no-override refuses when any of them already exist.",
    )
    parser.add_argument(
        "--include-history",
        "--include_history",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write correction_history on each row (default). "
        "--no-include-history omits that field and keeps the rest of the row.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Stay in this process. Shell launchers then skip nohup (default: false)",
    )
    parser.add_argument("--checkpoints", action="store_true")
    parser.add_argument("--checkpoint-size", "--checkpoint_size", type=int, default=100)
    parser.add_argument("--model", help="Override the local 20B / remote 120B model")
    parser.add_argument(
        "--log-level", "--log_level", type=parse_log_level, default=logging.INFO,
        help="Console log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL (default: INFO)",
    )
    args = parser.parse_args(argv)
    if args.local:
        base_url = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234")
        api_key = os.getenv("LMSTUDIO_API_KEY", LMSTUDIO_TOKEN)
        model = args.model or MODEL
    else:
        base_url = os.getenv("LMSTUDIO_MAC_URL", LMSTUDIO_MAC_URL)
        api_key = os.getenv("MAC_STUDIO_API_KEY", MAC_STUDIO_API_KEY)
        model = args.model or "openai/gpt-oss-120b"
    logger = get_console_logger("SQL Correction", level=args.log_level)
    logger.info(f"Using LM Studio: {base_url}; model: {model}")
    client = LMStudioClient(base_url=base_url, model=model, api_key=api_key)
    run_correct_sql_statements(
        args.input,
        args.output,
        client=client,
        openai_format=args.openai_format,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        max_rows=args.max_rows,
        context_length=args.context_length,
        checkpoints=args.checkpoints,
        checkpoint_size=args.checkpoint_size,
        logger=logger,
        require_source_confirmation=args.require_source_confirmation,
        override=args.override,
        include_history=args.include_history,
        foreground=args.foreground,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
