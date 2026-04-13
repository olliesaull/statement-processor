"""Shared pytest setup for unit tests.

We stub the global ``config`` and ``logger`` modules so imports do not
trigger AWS client setup or require the ``sp_common`` package.
"""

import sys
import types
from unittest.mock import MagicMock

# Stub sp_common before anything imports logger.
fake_sp_common = types.ModuleType("sp_common")
fake_sp_common_logger = types.ModuleType("sp_common.logger")
fake_sp_common_logger.logger = MagicMock()
fake_sp_common.logger = fake_sp_common_logger
sys.modules["sp_common"] = fake_sp_common
sys.modules["sp_common.logger"] = fake_sp_common_logger

fake_config = types.ModuleType("config")
# NOTE: These attributes are required by modules that import config at import time.
fake_config.S3_BUCKET_NAME = ""
fake_config.TENANT_DATA_TABLE_NAME = ""
fake_config.TENANT_STATEMENTS_TABLE_NAME = ""
fake_config.TENANT_BILLING_TABLE_NAME = ""
fake_config.STRIPE_API_KEY_SSM_PATH = ""
fake_config.tenant_data_table = None
fake_config.tenant_statements_table = None
fake_config.tenant_billing_table = None
fake_config.s3_client = None
sys.modules["config"] = fake_config
