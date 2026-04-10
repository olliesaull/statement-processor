"""Tests for typed models introduced in B1b.

Verifies ExtractionOutput and PersistItemsRequest construction,
field access, and immutability.
"""

import pytest

from core.statement_processor import ExtractionOutput, PersistItemsRequest


class TestExtractionOutput:
    """ExtractionOutput: frozen dataclass replacing dict return from run_extraction."""

    def test_construction_and_field_access(self) -> None:
        output = ExtractionOutput(filename="stmt.json", statement={"statement_items": []})
        assert output.filename == "stmt.json"
        assert output.statement == {"statement_items": []}

    def test_frozen(self) -> None:
        output = ExtractionOutput(filename="stmt.json", statement={})
        with pytest.raises(AttributeError):
            output.filename = "other.json"  # type: ignore[misc]


class TestPersistItemsRequest:
    """PersistItemsRequest: parameter object for _persist_statement_items."""

    def test_construction_with_all_fields(self) -> None:
        req = PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=[{"date": "2024-01-01"}], earliest_item_date="2024-01-01", latest_item_date="2024-12-31")
        assert req.tenant_id == "t1"
        assert req.contact_id == "c1"
        assert req.statement_id == "stmt-1"
        assert len(req.items) == 1
        assert req.earliest_item_date == "2024-01-01"
        assert req.latest_item_date == "2024-12-31"

    def test_optional_fields_default_none(self) -> None:
        req = PersistItemsRequest(tenant_id="t1", contact_id=None, statement_id="stmt-1", items=[])
        assert req.contact_id is None
        assert req.earliest_item_date is None
        assert req.latest_item_date is None

    def test_frozen(self) -> None:
        req = PersistItemsRequest(tenant_id="t1", contact_id=None, statement_id="s1", items=[])
        with pytest.raises(AttributeError):
            req.tenant_id = "t2"  # type: ignore[misc]
