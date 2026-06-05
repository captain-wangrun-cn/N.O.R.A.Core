from unittest.mock import MagicMock

from adapters.base import BaseAdapter
from brain.tools import ToolManager


class DummyAdapter(BaseAdapter):
    def run(self, message_handler):
        return None

    async def send_message(self, chat_id: str, text: str, **kwargs):
        return [text]

    async def start_typing(self, chat_id: str):
        return None

    async def stop_typing(self, chat_id: str):
        return None


def test_set_alarm_passes_runtime_chat_id_to_scheduler():
    scheduler = MagicMock()
    scheduler.add_alarm.return_value = {"success": True, "alarm_id": "a1"}
    manager = ToolManager(DummyAdapter(), scheduler=scheduler)

    result = manager.set_alarm(
        trigger_time="23:00",
        reason="测试",
        chat_id="telegram:group-1",
    )

    assert "a1" in result
    scheduler.add_alarm.assert_called_once()
    assert scheduler.add_alarm.call_args.kwargs["chat_id"] == "telegram:group-1"
