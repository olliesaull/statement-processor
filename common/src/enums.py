"""Shared enums for processing stages and token reservation statuses.

These string constants are used across the service and extraction lambda
to track statement processing lifecycle and billing reservation state.
Using enums prevents typo-driven bugs and centralizes the valid values.
"""

from enum import StrEnum


class ProcessingStage(StrEnum):
    """Lifecycle stage of a statement extraction pipeline run.

    Written to DynamoDB as the ProcessingStage attribute so the Flask UI
    can display granular progress during extraction.
    """

    QUEUED = "queued"
    CHUNKING = "chunking"
    EXTRACTING = "extracting"
    POST_PROCESSING = "post_processing"
    COMPLETE = "complete"
    FAILED = "failed"


class TokenReservationStatus(StrEnum):
    """Settlement status of a token reservation for a statement upload.

    Reservations start as RESERVED when the user uploads, move to
    CONSUMED on extraction success, or RELEASED on extraction failure.
    """

    RESERVED = "reserved"
    CONSUMED = "consumed"
    RELEASED = "released"
