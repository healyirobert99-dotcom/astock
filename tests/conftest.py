from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.option.markexpr:
        return
    skip_realdata = pytest.mark.skip(reason="realdata smoke test requires explicit -m realdata")
    for item in items:
        if "realdata" in item.keywords:
            item.add_marker(skip_realdata)
