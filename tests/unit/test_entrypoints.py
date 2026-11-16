"""Each process entry point must work when imported alone, as in production.

The test suite imports every module, which can hide a missing import: the
worker once failed in its container because it never loaded the tables its
foreign keys point at.
"""

import subprocess
import sys

import pytest

CHECK = """
import {module}
from sqlalchemy.orm import configure_mappers
from backend.database.base import Base
configure_mappers()
for table in Base.metadata.tables.values():
    for key in table.foreign_keys:
        key.column  # raises NoReferencedTableError if the target is not loaded
"""


@pytest.mark.parametrize("module", ["backend.worker", "backend.main", "backend.ingestion.pipeline"])
def test_entry_point_resolves_all_foreign_keys_on_its_own(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHECK.format(module=module)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
