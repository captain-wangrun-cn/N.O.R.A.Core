"""纯文本紧随图片/视频轮时折进同一轮的判定。

背景：用户先发图片（直接走后脑 image 轮），紧接着补一句文字说明。
用户视角这是一次表达，但图片轮已经启动，后续文本只能另开一轮，
结果图片和文本各回一次。`_media_turn_fold_target` 负责识别可折叠的场景。
"""

import time

from core.conversation_identity import build_identity_from_parts
from core.controller import NoraController
from core.message_handler import MessageHandlerMixin
from core.worker_status import WorkerStatus


class _FoldController(MessageHandlerMixin):
    def __init__(self):
        self.worker_status = {}
        self.generation_tasks = {}
        self.front_brain_tasks = {}
        self.task_queues = {}
        self.back_brain_input_context = {}
        self.conversation_identities = {}
        self.identity = build_identity_from_parts(
            platform="telegram",
            chat_id="chat-1",
            user_id="user-1",
            chat_type="private",
        )
        self.conversation_identities[self.identity.runtime_key] = self.identity

    def _identity_for_runtime_key(self, key):
        return self.conversation_identities[key]

    _scope_key_for_identity = NoraController._scope_key_for_identity
    _scope_key_for_runtime_key = NoraController._scope_key_for_runtime_key
    _get_scope_queue_for_runtime = NoraController._get_scope_queue_for_runtime
    _runtime_keys_for_scope = NoraController._runtime_keys_for_scope
    _has_pending_task = NoraController._has_pending_task
    _has_pending_front_task = NoraController._has_pending_front_task
    get_busy_backend_runtime = NoraController.get_busy_backend_runtime


def _busy_media_controller(**snapshot_overrides):
    controller = _FoldController()
    runtime_key = controller.identity.runtime_key
    status = WorkerStatus()
    status.start("图片输入")
    controller.worker_status[runtime_key] = status
    snapshot = {
        "text": "",
        "multimodal_images": [{"image_id": "img-1"}],
        "multimodal_videos": [],
        "persisted_user_message_ids": [1],
        "in_polling": False,
        "user_id": "user-1",
        "started_at": time.time(),
        "sent_user_message": False,
    }
    snapshot.update(snapshot_overrides)
    controller.back_brain_input_context[runtime_key] = snapshot
    return controller, runtime_key


def _fold(controller, *, user_id="user-1", chat_type="private"):
    return controller._media_turn_fold_target(
        memory_scope_id=controller.identity.memory_scope_id,
        user_id=user_id,
        chat_type=chat_type,
    )


def test_text_folds_into_running_image_turn():
    controller, runtime_key = _busy_media_controller()
    assert _fold(controller) == runtime_key


def test_video_turn_also_folds():
    controller, runtime_key = _busy_media_controller(
        multimodal_images=[], multimodal_videos=[{"video_id": "vid-1"}]
    )
    assert _fold(controller) == runtime_key


def test_no_fold_when_backend_idle():
    controller, runtime_key = _busy_media_controller()
    controller.worker_status[runtime_key].finish()
    assert _fold(controller) is None


def test_no_fold_for_text_only_turn():
    """正在跑的那一轮本来就没有媒体输入时，普通文本走原有前脑路径。"""
    controller, _ = _busy_media_controller(multimodal_images=[], multimodal_videos=[])
    assert _fold(controller) is None


def test_no_fold_after_user_visible_reply():
    """已经对用户说过话再折就会先看到半截、再看到重写版。"""
    controller, _ = _busy_media_controller(sent_user_message=True)
    assert _fold(controller) is None


def test_no_fold_in_polling_mode():
    """轮询模式由前脑审查驱动自己的重启节奏，不参与折叠。"""
    controller, _ = _busy_media_controller(in_polling=True)
    assert _fold(controller) is None


def test_no_fold_after_tool_execution():
    """折叠 = cancel + 重启，已执行过的工具副作用会被重跑一遍。"""
    controller, runtime_key = _busy_media_controller()
    controller.worker_status[runtime_key].log_tool("write_file", {"path": "a.txt"})
    assert _fold(controller) is None


def test_no_fold_outside_time_window():
    controller, _ = _busy_media_controller(
        started_at=time.time() - (MessageHandlerMixin.MEDIA_TURN_TEXT_FOLD_WINDOW + 5)
    )
    assert _fold(controller) is None


def test_group_fold_requires_same_sender():
    """群聊里别人的话不能折进这一轮。"""
    controller, runtime_key = _busy_media_controller()
    assert _fold(controller, user_id="user-2", chat_type="group") is None
    assert _fold(controller, user_id="user-1", chat_type="group") == runtime_key
