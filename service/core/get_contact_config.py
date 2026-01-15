"""
DynamoDB accessors for contact-specific statement mapping config.

These helpers read/write the JSON config stored on the tenant contact row.
"""

from typing import Any

from botocore.exceptions import ClientError

from config import tenant_contacts_config_table

CONFIG_ATTR = "config"


def get_contact_config(tenant_id: str, contact_id: str) -> dict[str, Any]:
    """
    Fetch contact-specific statement mapping config from DynamoDB.

    Raises:
        RuntimeError: When DynamoDB read fails.
        KeyError: When the contact has no config attribute.
        TypeError: When the config attribute is not a dict.
    """
    try:
        resp = tenant_contacts_config_table.get_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            ProjectionExpression="#cfg",
            ExpressionAttributeNames={"#cfg": CONFIG_ATTR},
        )
    except ClientError as exc:
        raise RuntimeError(f"DynamoDB error fetching config for TenantID={tenant_id}, ContactID={contact_id}") from exc

    item = resp.get("Item") if isinstance(resp, dict) else None
    if not isinstance(item, dict) or CONFIG_ATTR not in item:
        raise KeyError(f"Config not found for TenantID={tenant_id}, ContactID={contact_id}")

    cfg = item[CONFIG_ATTR]
    if not isinstance(cfg, dict):
        raise TypeError(f"Config attribute '{CONFIG_ATTR}' is not a dict: {type(cfg)}")

    return cfg


def set_contact_config(tenant_id: str, contact_id: str, config: dict[str, Any]) -> None:
    """Update contact-specific statement mapping config in DynamoDB."""
    if not isinstance(config, dict):
        raise TypeError("config must be a dict")
    try:
        tenant_contacts_config_table.update_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            UpdateExpression="SET #cfg = :cfg",
            ExpressionAttributeNames={"#cfg": CONFIG_ATTR},
            ExpressionAttributeValues={":cfg": config},
        )
    except ClientError as exc:
        raise RuntimeError(f"DynamoDB error updating config for TenantID={tenant_id}, ContactID={contact_id}") from exc
