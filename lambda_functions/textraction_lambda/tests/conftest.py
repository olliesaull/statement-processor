"""
Shared pytest setup for unit tests.

We stub the global ``config`` module so imports do not trigger AWS client setup.
"""

import sys
import types

fake_config = types.ModuleType("config")
# NOTE: These attributes are required by modules that import config at import time.
fake_config.tenant_contacts_config_table = None
fake_config.textract_client = None
fake_config.tenant_statements_table = None
fake_config.tenant_data_table = None
fake_config.s3_client = None
sys.modules["config"] = fake_config
