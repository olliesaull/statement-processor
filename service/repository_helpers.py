"""Shared helpers for small repository classes."""

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed


def fetch_items_by_tenant_id(get_item: Callable[[str], dict[str, object] | None], tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, dict[str, object] | None]:
    """Fetch multiple tenant-scoped items concurrently.

    Args:
        get_item: Repository-specific function that fetches one tenant item.
        tenant_ids: Iterable of tenant IDs to fetch.
        max_workers: Maximum number of concurrent lookups.

    Returns:
        Mapping of tenant IDs to the fetched item or ``None``.
    """
    unique_ids = {tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)}

    if not unique_ids:
        return {}

    items: dict[str, dict[str, object] | None] = dict.fromkeys(unique_ids)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(get_item, tenant_id): tenant_id for tenant_id in unique_ids}
        for future in as_completed(futures):
            tenant_id = futures[future]
            try:
                items[tenant_id] = future.result()
            except Exception:
                # Swallow individual lookup failures; caller can retry if needed.
                continue

    return items
