"""Validation helpers for statement upload and preflight flows.

These helpers keep route handlers in ``app.py`` focused on request/response
wiring while centralizing the business rules that decide whether an uploaded
statement batch is valid, affordable, and ready to process.
"""

from dataclasses import dataclass
from typing import Any

from werkzeug.datastructures import FileStorage

from core.get_contact_config import get_contact_config
from logger import logger
from tenant_data_repository import TenantDataRepository
from utils.pdf_page_count import PDFPageCountError, count_pdf_pages
from utils.storage import is_allowed_pdf


@dataclass(frozen=True)
class UploadPageCountResult:
    """Authoritative page-count outcome for one uploaded PDF."""

    filename: str
    page_count: int | None = None
    error: str | None = None

    def to_response_payload(self) -> dict[str, Any]:
        """Serialize the page-count result for JSON responses."""
        payload: dict[str, Any] = {"filename": self.filename, "page_count": self.page_count}
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class StatementUploadPreflightResult:
    """Aggregated upload validation result for the current batch."""

    files: list[UploadPageCountResult]
    total_pages: int
    available_tokens: int
    is_sufficient: bool
    can_submit: bool
    shortfall: int

    def to_response_payload(self) -> dict[str, Any]:
        """Serialize the preflight result for the upload page."""
        return {
            "files": [file_result.to_response_payload() for file_result in self.files],
            "total_pages": self.total_pages,
            "available_tokens": self.available_tokens,
            "is_sufficient": self.is_sufficient,
            "can_submit": self.can_submit,
            "shortfall": self.shortfall,
            "has_errors": any(file_result.error for file_result in self.files),
        }


@dataclass(frozen=True)
class PreparedStatementUpload:
    """Validated upload row ready for persistence and workflow start."""

    uploaded_file: FileStorage
    contact_id: str
    contact_name: str
    page_count: int


@dataclass(frozen=True)
class UploadTokenSufficiencyResult:
    """Token sufficiency summary for a validated upload batch."""

    total_pages: int
    available_tokens: int
    shortfall: int

    @property
    def is_sufficient(self) -> bool:
        """Return whether the current tenant balance covers the batch pages."""
        return self.shortfall == 0


def validate_upload_payload(files: list[FileStorage], names: list[str]) -> bool:
    """Validate the number of uploaded files and selected contacts."""
    if not files:
        logger.info("Upload rejected; no statement files provided.")
        return False
    if len(files) != len(names):
        logger.info("Upload rejected; file count does not match contact selections.")
        return False
    return True


def count_uploaded_pdf_pages(tenant_id: str | None, uploaded_file: FileStorage) -> UploadPageCountResult:
    """Count pages for one uploaded PDF and return a user-facing result object."""
    filename = uploaded_file.filename or "Unnamed PDF"
    if not is_allowed_pdf(filename, uploaded_file.mimetype):
        logger.info("Upload validation rejected non-PDF", tenant_id=tenant_id, statement_filename=filename, mimetype=uploaded_file.mimetype)
        return UploadPageCountResult(filename=filename, error="Only PDF statements are supported.")

    try:
        page_count = count_pdf_pages(uploaded_file)
        return UploadPageCountResult(filename=filename, page_count=page_count)
    except PDFPageCountError as exc:
        logger.warning("Upload validation could not count PDF pages", tenant_id=tenant_id, statement_filename=filename, error=str(exc))
        return UploadPageCountResult(filename=filename, error="Unable to determine page count for this PDF.")


def build_statement_upload_preflight(tenant_id: str | None, files: list[FileStorage]) -> StatementUploadPreflightResult:
    """Count pages for the current batch and compare it with the tenant balance."""
    file_results = [count_uploaded_pdf_pages(tenant_id, uploaded_file) for uploaded_file in files]
    total_pages = sum(result.page_count or 0 for result in file_results)
    available_tokens = TenantDataRepository.get_tenant_token_balance(tenant_id)
    shortfall = max(total_pages - available_tokens, 0)
    is_sufficient = total_pages <= available_tokens
    has_errors = any(result.error for result in file_results)

    return StatementUploadPreflightResult(
        files=file_results,
        total_pages=total_pages,
        available_tokens=available_tokens,
        is_sufficient=is_sufficient,
        can_submit=bool(file_results) and not has_errors and is_sufficient,
        shortfall=shortfall,
    )


def build_upload_token_sufficiency(tenant_id: str | None, prepared_uploads: list[PreparedStatementUpload]) -> UploadTokenSufficiencyResult:
    """Calculate whether a validated upload batch fits within the tenant balance."""
    total_pages = sum(upload.page_count for upload in prepared_uploads)
    available_tokens = TenantDataRepository.get_tenant_token_balance(tenant_id)
    shortfall = max(total_pages - available_tokens, 0)
    return UploadTokenSufficiencyResult(total_pages=total_pages, available_tokens=available_tokens, shortfall=shortfall)


def _ensure_contact_config(tenant_id: str | None, contact_id: str, contact_name: str, filename: str, error_messages: list[str]) -> bool:
    """Ensure the contact has a config; on failure, log and append a user-facing error."""
    try:
        get_contact_config(tenant_id, contact_id)
    except KeyError:
        logger.warning("Upload blocked; contact config missing", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_filename=filename)
        error_messages.append(f"Contact '{contact_name}' does not have a statement config yet. Please configure it before uploading.")
        return False
    except Exception as exc:
        logger.exception("Upload blocked; config lookup failed", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_filename=filename, error=exc)
        error_messages.append(f"Could not load the config for '{contact_name}'. Please try again later.")
        return False
    return True


def prepare_statement_uploads(tenant_id: str | None, files: list[FileStorage], names: list[str], contact_lookup: dict[str, str], error_messages: list[str]) -> list[PreparedStatementUpload]:
    """Validate submitted rows and return the subset that can proceed."""
    prepared_uploads: list[PreparedStatementUpload] = []

    for uploaded_file, contact in zip(files, names, strict=False):
        filename = uploaded_file.filename or "Unnamed PDF"
        contact_name = contact.strip()

        if not contact_name:
            logger.info("Upload blocked; contact missing", tenant_id=tenant_id, statement_filename=filename)
            error_messages.append(f"Please select a contact for '{filename}'.")
            continue

        page_count_result = count_uploaded_pdf_pages(tenant_id, uploaded_file)
        if page_count_result.error:
            error_messages.append(f"{filename}: {page_count_result.error}")
            continue

        contact_id: str | None = contact_lookup.get(contact_name)
        if not contact_id:
            logger.warning("Upload blocked; contact not found", tenant_id=tenant_id, contact_name=contact_name, statement_filename=filename)
            error_messages.append(f"Contact '{contact_name}' was not recognised. Please select a contact from the list.")  # nosec B608 - user-facing message only, no SQL execution
            continue

        if not _ensure_contact_config(tenant_id, contact_id, contact_name, filename, error_messages):
            continue

        prepared_uploads.append(PreparedStatementUpload(uploaded_file=uploaded_file, contact_id=contact_id, contact_name=contact_name, page_count=page_count_result.page_count or 0))

    return prepared_uploads
