from __future__ import annotations

import importlib
import logging
import platform
import sys
from typing import Any, Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config
from brain.llm import get_llm_client
from .message import context_from_update, has_bot_mention, strip_bot_mention

logger = logging.getLogger(__name__)


class TelegramCommandsMixin:
    def _command_should_process(self, update: Update) -> bool:
        """Private commands always run; group commands require an explicit @bot mention."""
        if not update.effective_chat:
            return False
        if update.effective_chat.type == "private":
            return True
        if update.effective_chat.type not in ("group", "supergroup"):
            return False
        if not self.bot_username or not update.message:
            return False
        text = update.message.text or update.message.caption or ""
        return has_bot_mention(text, self.bot_username)

    def _command_text(self, update: Update, fallback: str) -> str:
        text = update.message.text if update.message and update.message.text else fallback
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return strip_bot_mention(text, self.bot_username) or fallback
        return text

    async def _command_context(self, update: Update, text: str) -> Dict[str, Any]:
        context = context_from_update(
            update,
            text,
            bot_user_id=str(getattr(self, "bot_user_id", "") or ""),
        )
        return await self._enrich_group_context(context, update.effective_chat, update=update)

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._command_should_process(update):
            return
        if self._message_handler:
            event_context = await self._command_context(update, self._command_text(update, "/start"))
            await self._message_handler(event_context)

    async def _clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        # 优先交由 controller 统一清理（包含内存上下文）
        if self._message_handler:
            event_context = await self._command_context(update, self._command_text(update, "/clear"))
            await self._message_handler(event_context)
            return

        # fallback: 直接清数据库（清理所有，不保留 pinned）
        try:
            storage_id = chat_id if update.effective_chat.type != "private" else (str(update.effective_user.id) if update.effective_user else chat_id)
            self.message_history.clear_chat_history("telegram", storage_id, keep_pinned=False)
            if update.message:
                await update.message.reply_text("✅ 当前聊天的历史记录已清空。")
            logger.info(f"[{chat_id}] 聊天历史已通过 /clear 命令清空。")
        except Exception as e:
            logger.error(f"[{chat_id}] 清空历史记录时出错: {e}")
            if update.message:
                await update.message.reply_text("❌ 清空历史记录时发生错误。")

    async def _stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        # 交由 controller 统一停止前/后脑任务
        if self._message_handler:
            event_context = await self._command_context(update, self._command_text(update, "/stop"))
            await self._message_handler(event_context)
            return
        # fallback: 没有 controller 时提示
        if update.message:
            await update.message.reply_text("⚠️ 当前无法停止任务。")

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        
        try:
            # N.O.R.A. Core and System Info
            app_config = config.get_config()
            core_version = app_config.get("version", "N/A") if app_config else "N/A"
            python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            os_info = f"{platform.system()} {platform.release()}"
            
            # LLM Info
            llm_client = get_llm_client()
            llm_provider = llm_client.__class__.__name__.replace("LLM", "")
            llm_model = getattr(llm_client, 'model', 'N/A')

            # Memory/DB Info
            db_path = self.message_history.db_path
            db_exists = "✅ Connected" if db_path.exists() else "❌ Not Found"
            
            # RAG/VectorDB Info
            from memory.rag import RAGEngine
            rag_engine = RAGEngine()
            rag_status = "✅ Online" if rag_engine.enabled else "❌ Offline"

            status_text = (
                f"<b>N.O.R.A. Core Status</b>\n\n"
                f"<b><u>System</u></b>\n"
                f"  <b>Version:</b> {core_version}\n"
                f"  <b>Python:</b> {python_version}\n"
                f"  <b>OS:</b> {os_info}\n\n"
                f"<b><u>Language Model</u></b>\n"
                f"  <b>Provider:</b> {llm_provider}\n"
                f"  <b>Model:</b> {llm_model}\n\n"
                f"<b><u>Memory</u></b>\n"
                f"  <b>History DB:</b> {db_exists}\n"
                f"  <b>RAG Engine:</b> {rag_status}\n"
            )

            if update.message:
                await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)
            logger.info(f"[{chat_id}] 已发送状态信息。")
        except Exception as e:
            logger.error(f"[{chat_id}] 获取状态时出错: {e}", exc_info=True)
            if update.message:
                await update.message.reply_text("❌ 获取系统状态时发生错误。")

    async def _context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """显示上下文窗口使用情况，透传给 controller。"""
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = await self._command_context(update, self._command_text(update, "/context"))
        await self._message_handler(event_context)

    async def _undo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """撤销上一条 AI 消息，透传给 controller。"""
        if not self._command_should_process(update):
            return
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = await self._command_context(update, self._command_text(update, "/undo"))
        await self._message_handler(event_context)

    async def _override_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """一次性放行：撤掉上一条回复并带放行声明重新生成，透传给 controller。"""
        if not self._command_should_process(update):
            return
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = await self._command_context(update, self._command_text(update, "/override"))
        await self._message_handler(event_context)

    async def _debug_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """切换 debug 模式，透传给 controller。"""
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = await self._command_context(update, self._command_text(update, "/debug"))
        await self._message_handler(event_context)

    async def _debug_cleanup_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """调试清空入口：弹出危险操作按钮（需二次确认）。"""
        if not self._command_should_process(update) or not update.message:
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 清空 Qdrant 记忆", callback_data="debug_cleanup:qdrant")],
            [InlineKeyboardButton("🧹 清空 Mongo 图片库", callback_data="debug_cleanup:mongo")],
            [InlineKeyboardButton("🧹 清空消息镜像库", callback_data="debug_cleanup:mirror")],
            [InlineKeyboardButton("🧹 清空上下文压缩库", callback_data="debug_cleanup:context")],
            [InlineKeyboardButton("🧹 清空聊天记录", callback_data="debug_cleanup:chat")],
            [InlineKeyboardButton("💥 全部清空", callback_data="debug_cleanup:all")],
            [InlineKeyboardButton("取消", callback_data="debug_cleanup:cancel")],
        ])

        warning_text = (
            "⚠️ <b>危险操作</b>\n"
            "以下清空操作均为不可恢复的永久删除。\n"
            "请选择要执行的清理范围。"
        )
        await update.message.reply_text(warning_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    async def _model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """展示模型切换按钮并允许保存配置。"""
        if not self._command_should_process(update) or not update.message:
            return

        text, markup = self._build_model_alias_menu()
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    def _build_model_alias_menu(self):
        """构造 /model 顶层 alias 选择菜单。"""
        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}

        buttons = []
        for alias in ["smart", "fast", "coder", "image", "video", "security", "fast-image", "draw", "draw_desc", "summary"]:
            current = models_cfg.get(alias, "")
            label = f"{alias}: {current or '未设置'}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model_pick:{alias}")])
        buttons.append([InlineKeyboardButton("关闭", callback_data="model_pick:cancel")])

        text = (
            "🧠 <b>模型切换</b>\n"
            "点击要修改的模型别名，然后输入新的模型名称。\n"
            "将立即保存到 config.yml。"
        )
        return text, InlineKeyboardMarkup(buttons)

    def _get_provider_for_alias(self, cfg: Dict[str, Any], alias: str) -> str:
        llm_cfg = cfg.get("llm", {}) or {}
        model_providers = llm_cfg.get("model_providers", {}) or {}
        return model_providers.get(alias) or llm_cfg.get("provider", "gemini")

    def _get_provider_config(self, cfg: Dict[str, Any], provider_name: str) -> Dict[str, Any]:
        llm_cfg = cfg.get("llm", {}) or {}
        providers_cfg = llm_cfg.get("providers", {}) or {}
        if provider_name in providers_cfg:
            return providers_cfg.get(provider_name) or {}

        api_keys = llm_cfg.get("api_keys", {}) or {}
        legacy = {
            "type": provider_name,
            "api_key": api_keys.get(provider_name, ""),
        }
        if provider_name == "openai":
            if llm_cfg.get("base_url"):
                legacy["base_url"] = llm_cfg.get("base_url")
            if llm_cfg.get("user_agent"):
                legacy["user_agent"] = llm_cfg.get("user_agent")
        return legacy

    def _fetch_provider_models(self, provider_name: str, provider_cfg: Dict[str, Any]) -> List[str]:
        provider_type = provider_cfg.get("type", provider_name)
        api_key = provider_cfg.get("api_key", "")
        if not api_key:
            return []

        try:
            if provider_type == "gemini":
                genai = importlib.import_module("google.generativeai")
                if hasattr(genai, "configure"):
                    genai.configure(api_key=api_key)
                if hasattr(genai, "list_models"):
                    models = [
                        m.name.replace("models/", "")
                        for m in genai.list_models()
                        if "generateContent" in getattr(m, "supported_generation_methods", [])
                    ]
                    return sorted(models)
                return []
            if provider_type == "openai":
                openai_module = importlib.import_module("openai")
                base_url = provider_cfg.get("base_url") or None
                client = openai_module.OpenAI(api_key=api_key, base_url=base_url)
                return sorted([model.id for model in client.models.list()])
        except Exception:
            return []

        return []

    async def _regenerate_proactive_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        手动触发“重新生成主动消息计划”。

        用法:
        - /regenerate_proactive
        - /regenerate_proactive append   (追加)
        - /regenerate_proactive replace  (覆盖，默认)
        """
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        mode = (context.args[0].strip().lower() if context and context.args else "replace")
        if mode not in ("replace", "append"):
            mode = "replace"

        event_context = await self._command_context(update, f"/regenerate_proactive {mode}")
        await self._message_handler(event_context)

    async def _schedule_today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看今日主动消息计划。"""
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = await self._command_context(update, self._command_text(update, "/schedule_today"))
        await self._message_handler(event_context)

    async def _custom_scope_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """设置 CUSTOM.md 注入范围。"""
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        try:
            scopes = self._load_custom_scopes()
            if update.message:
                await update.message.reply_text(
                    self._render_custom_scope_text(scopes),
                    reply_markup=self._render_custom_scope_keyboard(scopes),
                )
        except Exception:
            logger.error(f"[{chat_id}] /custom_scope 按钮初始化失败", exc_info=True)
            if update.message:
                await update.message.reply_text("❌ 无法加载按钮。")

    async def _nora_prefs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """展示 Nora 偏好设置按钮。"""
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)

        try:
            prefs = self._load_nora_preferences()
            if update.message:
                await update.message.reply_text(
                    self._render_nora_prefs_text(prefs),
                    reply_markup=self._render_nora_prefs_keyboard(prefs),
                )
        except Exception:
            logger.error(f"[{chat_id}] /nora_prefs 按钮初始化失败", exc_info=True)
            if update.message:
                await update.message.reply_text("❌ 无法加载 Nora 偏好设置。")

    async def _set_stream_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """切换当前会话的输出模式。用法：/set_stream on|off，或点击按钮选择。"""
        if not self._command_should_process(update):
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        arg = (context.args[0].strip().lower() if context and context.args else "").replace("\n", " ")
        if arg in ("on", "off"):
            event_context = await self._command_context(update, f"/set_stream {arg}")
            await self._message_handler(event_context)
            return

        if arg:
            if update.message:
                await update.message.reply_text("用法：/set_stream on|off（on=一次性输出，off=流式输出）")
            return

        event_context = await self._command_context(update, self._command_text(update, "/set_stream"))
        await self._message_handler(event_context)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 启用一次性输出", callback_data="set_stream:on")],
            [InlineKeyboardButton("🔵 恢复流式输出", callback_data="set_stream:off")],
        ])
        prompt_text = (
            "请选择输出模式：\n"
            "- 🟢 启用一次性输出（非流式，完整内容一次发出）\n"
            "- 🔵 恢复流式输出（逐步流式发送）"
        )
        if update.message:
            await update.message.reply_text(prompt_text, reply_markup=keyboard)
