# %%
import logging

from sql_tools.validation import validate_sql
from .logging_utils import get_console_logger

logger = get_console_logger("DatasetUtils", level=logging.INFO)
# -- Version Info

__version__ = "0.9.0"


# -- Dataset Validation -------------------------------------------------------------

def extract_sql_from_row(row, text_field=None, openai_prompt_format=False):
    sql_statement = None
    ddl_statement = None
    if text_field is not None:
        row = row[text_field]

    if openai_prompt_format:
        for role in row:
            if role['role'] == 'assistant':
                sql_statement = role['content']
            if role['role'] == 'system':
                ddl_statement = role['content']
    else:
        sql_statement = row['answer']
        ddl_statement = row['context']

    return sql_statement, ddl_statement


def check_sql_statements(dataset,
                         validate_schema=True,
                         dialect="mysql",
                         max_rows=0,
                         text_field=None,
                         openai_prompt_format=False,
                         logger=logger,
                         logging_steps=10_000
                         ):
    """Validate dataset rows without computing Spider hardness.

    Supplied DDL must pass syntax validation even when schema checks are disabled.
    Returns accepted rows and separate SQL, DDL, and schema failure lists.
    """
    correct_counter = 0
    failure_counter = 0
    dml_failure_counter = 0
    ddl_failure_counter = 0
    schema_validation_failure_counter = 0

    correct_prompts = []
    syntactical_error_prompts = []
    syntactical_ddl_error_prompts = []
    schema_validation_faild_prompts = []

    logger.info(
        f"Processing {len(dataset):,} rows, dialect={dialect}, validate_schema={validate_schema}, max_rows={max_rows:,}"
        )

    for i, data in enumerate(dataset):
        sql_statement, ddl_statement = extract_sql_from_row(
                data,
                text_field=text_field,
                openai_prompt_format=openai_prompt_format
                )
        if i % logging_steps == 0 and i > 0:
            logger.info(
                    f"Processing row {i:,} of {len(dataset):,} ({i / len(dataset):.2%}), Current counts: correct={correct_counter:,}, failures={failure_counter:,}, "
                    )
        if sql_statement is not None:
            cr = validate_sql(
                    sql_statement,
                    ddl_statement=ddl_statement,
                    validate_schema=validate_schema,
                    dialect=dialect,
                    status_history=data.get("status_history"),
                    )
            if cr is not None:
                data["status_history"] = cr.status_history
                data["dialect"] = cr.dialect
                data["syntax_status"] = cr.syntax_status
                data["ddl_syntax_status"] = cr.ddl_syntax_status
                data["schema_status"] = cr.schema_status
                if (cr.syntax_status == "valid"
                        and (ddl_statement is None or cr.ddl_syntax_status == "valid")
                        and (not validate_schema or cr.schema_status == "valid")):
                    correct_prompts.append(data)
                    correct_counter += 1
                else:
                    failure_counter += 1
                    logger.debug(
                            f"SQL statement {i:,} excluded, sql_syntax: {cr.syntax_status}, "
                            f"ddl_syntax_status={cr.ddl_syntax_status}, Valid: {cr.schema_status}"
                            )
                    logger.debug(f"   SQL: {sql_statement}")
                    logger.debug(f"   DDL: {ddl_statement}")
                    if cr.syntax_status != "valid":
                        logger.debug(f"   SQL statement: {cr.syntax_errors}")
                        data['syntax_errors'] = cr.syntax_errors
                        syntactical_error_prompts.append(data)
                        dml_failure_counter += 1
                    else:
                        if ddl_statement is not None and cr.ddl_syntax_status != "valid":
                            logger.debug(f"   DDL statement: {cr.ddl_syntax_errors}")
                            data['ddl_syntax_errors'] = cr.ddl_syntax_errors
                            syntactical_ddl_error_prompts.append(data)
                            ddl_failure_counter += 1
                        if validate_schema and cr.schema_status != "valid":
                            logger.debug(f"   Schema statement: {cr.schema_errors}")
                            data['schema_errors'] = cr.schema_errors
                            schema_validation_faild_prompts.append(data)
                            schema_validation_failure_counter += 1

        else:
            data["status_history"] = list(data.get("status_history") or []) + ["validation_failed"]
            logger.debug(f"Row {i:,} has no SQL statement.")

        if max_rows > 0 and i >= max_rows - 1:
            logger.info(f"Reached max_rows limit of {max_rows:,}. Stopping validation.")
            break

    nr_of_statements = correct_counter + failure_counter
    percentage_factor = 100 / nr_of_statements if nr_of_statements else 0
    logger.info(
            f"Total correct: {correct_counter:,} out of {nr_of_statements:,} "
            f"({correct_counter * percentage_factor:.2f}%)"
            )
    logger.info(
            f"Total failures: {failure_counter:,} out of {nr_of_statements:,} "
            f"({failure_counter * percentage_factor:.2f}%)"
            )
    logger.info(
            f"Total DML failures: {dml_failure_counter:,} "
            f"({dml_failure_counter * percentage_factor:.2f}%)"
            )
    logger.info(
            f"Total DDL failures: {ddl_failure_counter:,} "
            f"({ddl_failure_counter * percentage_factor:.2f}%)"
            )
    logger.info(
            f"Total schema validation failures: {schema_validation_failure_counter:,} "
            f"({schema_validation_failure_counter * percentage_factor:.2f}%)"
            )
    return correct_prompts, syntactical_error_prompts, syntactical_ddl_error_prompts, schema_validation_faild_prompts
