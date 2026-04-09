"""Best-effort processing progress updates to DynamoDB.

Writes ProcessingStage, ProcessingProgress, and ProcessingTotalSections
to the statement header row so the Flask UI can show granular progress
during extraction. All updates are non-blocking — failures are logged
but never fail the extraction pipeline.
"""

from typing import Any

from config import tenant_statements_table
from logger import logger


def update_processing_stage(tenant_id: str, statement_id: str, stage: str, *, progress: str | None = None, total_sections: int | None = None) -> None:
    """Update processing progress on the statement header row.

    Args:
        tenant_id: Tenant partition key.
        statement_id: Statement sort key.
        stage: Current processing stage (queued, chunking, extracting,
            post_processing, complete, failed).
        progress: Chunk progress string like "3/10". When None the
            ProcessingProgress attribute is removed.
        total_sections: Total number of sections. When None the
            ProcessingTotalSections attribute is removed.
    """
    if tenant_statements_table is None:
        return

    try:
        _do_update(tenant_id, statement_id, stage, progress, total_sections)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to update processing stage", tenant_id=tenant_id, statement_id=statement_id, stage=stage, error=str(exc), exc_info=True)


def _do_update(tenant_id: str, statement_id: str, stage: str, progress: str | None, total_sections: int | None) -> None:
    """Build and execute the DynamoDB update_item call."""
    set_parts: list[str] = ["#stage = :stage"]
    remove_parts: list[str] = []
    attr_names: dict[str, str] = {"#stage": "ProcessingStage"}
    attr_values: dict[str, Any] = {":stage": stage}

    if progress is not None:
        set_parts.append("#progress = :progress")
        attr_names["#progress"] = "ProcessingProgress"
        attr_values[":progress"] = progress
    else:
        # Use the literal attribute name in REMOVE — ProcessingProgress is not a DynamoDB
        # reserved word, so no alias is required.
        remove_parts.append("ProcessingProgress")

    if total_sections is not None:
        set_parts.append("#total_sections = :total_sections")
        attr_names["#total_sections"] = "ProcessingTotalSections"
        attr_values[":total_sections"] = total_sections
    else:
        # Same rationale as ProcessingProgress — no alias needed for REMOVE.
        remove_parts.append("ProcessingTotalSections")

    update_expr = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)

    tenant_statements_table.update_item(
        Key={"TenantID": tenant_id, "StatementID": statement_id}, UpdateExpression=update_expr, ExpressionAttributeNames=attr_names, ExpressionAttributeValues=attr_values
    )
