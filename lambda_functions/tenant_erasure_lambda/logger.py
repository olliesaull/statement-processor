"""Re-export the shared logger from sp_common for the tenant erasure lambda."""

from sp_common.logger import logger

__all__ = ["logger"]
