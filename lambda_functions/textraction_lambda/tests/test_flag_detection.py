"""
Unit tests for statement flag attachment.
"""

import core.transform as transform


# region Flag detection
def test_table_to_json_attaches_invalid_date_flags(monkeypatch) -> None:
    """Attach invalid-date flags to items and statement-level metadata.

    Args:
        None.

    Returns:
        None.
    """
    contact_cfg = {"date_format": "DD/MM/YYYY", "statement_items": {"date": "Date", "number": "Number"}}

    monkeypatch.setattr(transform, "get_contact_config", lambda tenant_id, contact_id: contact_cfg)
    monkeypatch.setattr(transform, "set_contact_config", lambda tenant_id, contact_id, config: None)

    tables = [{"page": 1, "grid": [["Date", "Number"], ["not-a-date", "INV-1"]]}]

    output = transform.table_to_json(tables, tenant_id="tenant-1", contact_id="contact-1", statement_id="stmt-1")

    items = output["statement_items"]
    assert items[0]["_flags"] == ["invalid-date"]

    flags = output["_flags"]
    assert flags[0]["flags"] == ["invalid-date"]
    assert flags[0]["page"] == 1
    assert flags[0]["row"] == 1


# endregion
