from typing import Any, Dict

from botocore.exceptions import ClientError

from config import tenant_contacts_config_table


def get_contact_config(tenant_id: str, contact_id: str) -> Dict[str, Any]:
    """
    Fetch contact-specific statement mapping config from DynamoDB.

    :param tenant_id: TenantID partition key value
    :param contact_id: ContactID sort key value
    :return: Config dict
    """
    attr_name = "config"
    try:
        resp = tenant_contacts_config_table.get_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            ProjectionExpression="#cfg",
            ExpressionAttributeNames={"#cfg": attr_name},
        )
    except ClientError as e:
        raise RuntimeError(f"DynamoDB error fetching config: {e}")

    item = resp.get("Item")
    if not item or attr_name not in item:
        raise KeyError(f"Config not found for TenantID={tenant_id}, ContactID={contact_id}")

    cfg = item[attr_name]
    if not isinstance(cfg, dict):
        raise TypeError(f"Config attribute '{attr_name}' is not a dict: {type(cfg)}")

    return cfg


def set_contact_config(tenant_id: str, contact_id: str, config: Dict[str, Any]) -> None:
    """Update contact-specific statement mapping config in DynamoDB."""
    if not isinstance(config, dict):
        raise TypeError("config must be a dict")
    try:
        tenant_contacts_config_table.update_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            UpdateExpression="SET #cfg = :cfg",
            ExpressionAttributeNames={"#cfg": "config"},
            ExpressionAttributeValues={":cfg": config},
        )
    except ClientError as e:
        raise RuntimeError(f"DynamoDB error updating config: {e}")
