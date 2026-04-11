"""Re-export the shared logger from sp_common for the extraction lambda."""

from sp_common.logger import logger

__all__ = ["logger"]
