import asyncio
import importlib.util
from pathlib import Path


_MAIN_SPEC = importlib.util.spec_from_file_location(
    "nora_project_main",
    Path(__file__).resolve().parents[1] / "main.py",
)
_MAIN_MODULE = importlib.util.module_from_spec(_MAIN_SPEC)
assert _MAIN_SPEC.loader is not None
_MAIN_SPEC.loader.exec_module(_MAIN_MODULE)
_run_adapters_async = _MAIN_MODULE._run_adapters_async


class _Controller:
    def __init__(self):
        self.shutdown_calls = 0
        self.scheduler_calls = 0
        self.trigger_calls = 0

    async def handle_new_message(self, context):
        return None

    def start_scheduler(self):
        self.scheduler_calls += 1

    async def start_triggers(self):
        self.trigger_calls += 1

    async def shutdown(self):
        self.shutdown_calls += 1


class _Adapter:
    def __init__(self):
        self.on_ready = self._on_ready

    async def _on_ready(self):
        return None

    async def run_async(self, message_handler):
        await self.on_ready()


def test_run_adapters_shuts_down_controller_once():
    async def run():
        controller = _Controller()
        await _run_adapters_async(controller, [_Adapter(), _Adapter()])

        assert controller.scheduler_calls == 1
        assert controller.trigger_calls == 1
        assert controller.shutdown_calls == 1

    asyncio.run(run())
