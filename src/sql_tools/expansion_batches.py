"""Prepare independent DDL examples for one request and validate each result."""

from itertools import islice
import json
import math

from .expand import DEFAULT_SYSTEM_MESSAGE, ExpansionResponseError, expand_statement, _record_failure

BATCH_CONTEXT_LENGTH = 32768
BATCH_SYSTEM_MESSAGE = DEFAULT_SYSTEM_MESSAGE + """

BATCH PROTOCOL (overrides the single-example input/output shape above):
Input is {"examples": [{"id": 0, "question": ..., "answer": ..., "context": ..., "dialect": ...}]}.
Treat each example independently. Never share tables or columns between examples.
Return {"results": [{"id": 0, "context": "complete DDL"}, ...]}.
For an individual failure return {"id": 0, "FAILURE": "reason"}.
Return exactly one result for every supplied integer id. Preserve those ids.
"""


def prepare_expansion_batches(rows, batch_size, *, context_length=BATCH_CONTEXT_LENGTH):
    """Yield rows, serialized payload and a conservative character-based output budget.

    Three characters per token is a heuristic, not an exact tokenizer count.
    Reserve output for the existing DDL plus 512 tokens per example for growth.
    Split batches further if their estimated input/output exceeds the context.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if context_length <= 0:
        raise ValueError("context_length must be positive")

    def prepare(batch):
        examples = [{"id": index, **{key: row[key] for key in
                    ("question", "answer", "context") if key in row},
                     "dialect": row.get("dialect") or "mysql"}
                    for index, row in enumerate(batch)]
        payload = json.dumps({"examples": examples}, ensure_ascii=False)
        max_tokens = sum(math.ceil(len(json.dumps(row.get("context", ""), ensure_ascii=False)) / 3)
                         + 512 for row in batch)
        input_tokens = math.ceil((len(BATCH_SYSTEM_MESSAGE) + len(payload)) / 3) + 1024
        if input_tokens + max_tokens > context_length:
            if len(batch) > 1:
                middle = len(batch) // 2
                yield from prepare(batch[:middle])
                yield from prepare(batch[middle:])
            else:
                yield batch, payload, None
        else:
            yield batch, payload, max_tokens

    iterator = iter(rows)
    while batch := list(islice(iterator, batch_size)):
        yield from prepare(batch)


def process_expansion_batch(batch, response_text):
    """Match by id rather than response order; reject missing/duplicate/unknown ids."""
    try:
        response = json.loads(response_text)
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            raise ValueError("Expected a results list")
        grouped = {}
        for item in results:
            if not isinstance(item, dict) or type(item.get("id")) is not int or not 0 <= item["id"] < len(batch):
                raise ValueError("Result has an invalid or unknown id")
            grouped.setdefault(item["id"], []).append(item)
    except (TypeError, ValueError) as error:
        for row in batch:
            _record_failure(row, f"Invalid batch response: {error}")
        return [], [], batch
    expanded, unchanged, failed = [], [], []
    for index, row in enumerate(batch):
        matches = grouped.get(index, [])
        if len(matches) != 1:
            _record_failure(row, f"Expected one result for id {index}; received {len(matches)}")
        else:
            try:
                expand_statement(row, matches[0])
            except ExpansionResponseError:
                pass
        outcome = row["status_history"][-1]
        if outcome == "extended":
            expanded.append(row)
        elif outcome == "extension_unchanged":
            unchanged.append(row)
        else:
            failed.append(row)
    return expanded, unchanged, failed


def request_expansion_batches(batches, *, client, logger, context_length=BATCH_CONTEXT_LENGTH, batch_offset=0):
    """Request prepared batches and collect independently validated row outcomes.

    batch_offset is the number of batches already completed in this run. The log
    line shows that running total and, in parentheses, the count within this call.
    """
    expanded, unchanged, failed = [], [], []
    request_seconds = 0.0
    index = 0
    for index, (batch, payload, max_tokens) in enumerate(batches, 1):
        if max_tokens is None:
            for row in batch:
                _record_failure(row, "Estimated input/output exceeds batch context budget")
            failed.extend(batch)
            continue
        logger.info(
                f"Batch {batch_offset + index:,} ({index:,}): {len(batch):,} rows; "
                f"estimated max_tokens={max_tokens:,}"
                )
        try:
            client.clear_history()
            try:
                response, _pt, _ct, _rt, seconds = client.send_request(
                    payload, system_message=BATCH_SYSTEM_MESSAGE, max_tokens=max_tokens,
                    context_length=context_length, temperature=0.0,
                    ignore_history=True, logger=logger,
                )
            finally:
                client.clear_history()
            request_seconds += seconds or 0
        except Exception as error:
            for row in batch:
                _record_failure(row, f"Batch request failed: {error}")
            failed.extend(batch)
            continue
        successful, same, unsuccessful = process_expansion_batch(batch, response)
        expanded.extend(successful)
        unchanged.extend(same)
        failed.extend(unsuccessful)
        logger.info(
                f"Expanded: {len(expanded):,}; unchanged: {len(unchanged):,}; "
                f"failed: {len(failed):,}"
                )
    return expanded, unchanged, failed, request_seconds, index
