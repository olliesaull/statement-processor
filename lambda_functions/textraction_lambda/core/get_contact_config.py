from typing import Any, Dict

from botocore.exceptions import ClientError

from config import tenant_contacts_config_table


def get_contact_config(tenant_id: str, contact_id: str) -> Dict[str, Any]:
    attr_name = "config"
    if tenant_contacts_config_table is None:
        raise RuntimeError("Contact config table not configured")
    try:
        resp = tenant_contacts_config_table.get_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            ProjectionExpression="#cfg",
            ExpressionAttributeNames={"#cfg": attr_name},
        )
    except ClientError as exc:
        raise RuntimeError(f"DynamoDB error fetching config: {exc}")

    item = resp.get("Item") if isinstance(resp, dict) else None
    if not item or attr_name not in item:
        raise KeyError(f"Config not found for TenantID={tenant_id}, ContactID={contact_id}")

    cfg = item[attr_name]
    if not isinstance(cfg, dict):
        raise TypeError(f"Config attribute '{attr_name}' is not a dict: {type(cfg)}")

    return cfg


def set_contact_config(tenant_id: str, contact_id: str, config: Dict[str, Any]) -> None:
    if tenant_contacts_config_table is None:
        raise RuntimeError("Contact config table not configured")
    if not isinstance(config, dict):
        raise TypeError("config must be a dict")
    try:
        tenant_contacts_config_table.update_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            UpdateExpression="SET #cfg = :cfg",
            ExpressionAttributeNames={"#cfg": "config"},
            ExpressionAttributeValues={":cfg": config},
        )
    except ClientError as exc:
        raise RuntimeError(f"DynamoDB error updating config: {exc}")
