"""RTC 测试共用：anyio backend 固定 asyncio（不跑 trio）。"""

import pytest

pytest_plugins = ["anyio"]


@pytest.fixture
def anyio_backend():
    return "asyncio"
