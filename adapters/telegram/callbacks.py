from __future__ import annotations

import logging
import time
import uuid
from typing import Any, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

import config
from .message import build_telegram_event

logger = logging.getLogger(__name__)


class TelegramCallbacksMixin:
    def _callback_user_id(self, query) -> str:
        if query and query.from_user and getattr(query.from_user, "id", None) is not None:
            return str(query.from_user.id)
        if query and query.message and query.message.chat:
            return str(query.message.chat.id)
        return ""

    def _callback_user_name(self, query) -> str:
        user_id = self._callback_user_id(query)
        user = query.from_user if query else None
        if not user:
            return user_id or "Unknown"
        name_parts = [
            part.strip()
            for part in (getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "")
            if part and part.strip()
        ]
        if name_parts:
            return " ".join(name_parts)
        username = getattr(user, "username", "") or ""
        if username:
            return f"@{username}"
        return user_id or "Unknown"

    def _callback_base_context(self, query, text: str) -> dict[str, Any]:
        chat = query.message.chat if query and query.message else None
        callback_event_id = getattr(query, "id", None) if query else None
        event = build_telegram_event(
            chat_id=str(chat.id) if chat else "",
            user_id=self._callback_user_id(query),
            text=text,
            chat_type=chat.type if chat else "private",
            user_name=self._callback_user_name(query),
            platform_message_id=str(callback_event_id) if callback_event_id is not None else None,
            raw=query,
        )
        return event.to_context()

    async def _callback_context(self, query, text: str) -> dict[str, Any]:
        context = self._callback_base_context(query, text)
        chat = query.message.chat if query and query.message else None
        return await self._enrich_group_context(context, chat)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 inline keyboard 回调"""
        query = update.callback_query
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
        callback_data = query.data
        
        # 确认回调
        await query.answer()

        if callback_data and callback_data.startswith("debug_cleanup_confirm:"):
            action = callback_data.split(":", 1)[1]
            await self._handle_debug_cleanup_confirm(query, action)
            return

        if callback_data and callback_data.startswith("model_pick:"):
            await self._handle_model_pick_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_clear:"):
            await self._handle_model_clear_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_set:"):
            await self._handle_model_set_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_page:"):
            await self._handle_model_page_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_provider:"):
            await self._handle_model_provider_callback(query, callback_data)
            return

        # effort_set 必须排在 effort_pick 之前吗？不必——两个前缀互不包含。
        # 但顺序仍要保持「更具体的前缀在前」的习惯，避免以后加 effort_pick_xxx 时踩坑。
        if callback_data and callback_data.startswith("effort_set:"):
            await self._handle_effort_set_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("effort_pick:"):
            await self._handle_effort_pick_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("debug_cleanup:"):
            await self._handle_debug_cleanup_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("custom_scope:"):
            await self._handle_custom_scope_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("nora_prefs:"):
            await self._handle_nora_prefs_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("set_stream:"):
            await self._handle_set_stream_callback(query, callback_data)
            return
        
        # 构造消息
        text = f"[按钮点击: {callback_data}]"
        
        if self._aggregator:
            full_context = await self._callback_context(query, text)
            await self._aggregator.add_message(chat_id, text, full_context)
        
        logger.info(f"[{chat_id}] 收到回调: {callback_data}")

    async def _handle_debug_cleanup_callback(self, query, callback_data: str):
        """处理调试清空按钮回调（带二次确认）。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)

        action = callback_data.split(":", 1)[1]
        if action == "cancel":
            await query.edit_message_text("已取消清空操作。")
            return

        token_key = f"{chat_id}:{action}"
        now_ts = time.time()

        async with self._cleanup_lock:
            if token_key not in self._cleanup_confirm_tokens:
                self._cleanup_confirm_tokens[token_key] = now_ts
                confirm_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❗确认删除", callback_data=f"debug_cleanup_confirm:{action}")],
                    [InlineKeyboardButton("取消", callback_data="debug_cleanup:cancel")],
                ])
                await query.edit_message_text(
                    "⚠️ <b>二次确认</b>\n"
                    "该操作将永久删除数据，且不可恢复。\n"
                    "若确认，请点击下方按钮。",
                    reply_markup=confirm_keyboard,
                    parse_mode=ParseMode.HTML,
                )
                return

        await query.edit_message_text("⚠️ 确认信息已过期，请重新发起 /debug_cleanup。")

    async def _handle_debug_cleanup_confirm(self, query, action: str):
        """执行清空操作。"""
        chat_id = str(query.message.chat.id)
        token_key = f"{chat_id}:{action}"
        now_ts = time.time()

        async with self._cleanup_lock:
            created_at = self._cleanup_confirm_tokens.get(token_key)
            if not created_at or now_ts - created_at > 60:
                if token_key in self._cleanup_confirm_tokens:
                    self._cleanup_confirm_tokens.pop(token_key, None)
                await query.edit_message_text("⚠️ 确认已超时，请重新发起 /debug_cleanup。")
                return
            self._cleanup_confirm_tokens.pop(token_key, None)

        result_lines = ["✅ 清空完成"]

        if action in ("qdrant", "all"):
            result_lines.append(self._purge_qdrant())
        if action in ("mongo", "all"):
            result_lines.append(self._purge_mongo())
        if action in ("mirror", "all"):
            result_lines.append(self._purge_message_log())
        if action in ("context", "all"):
            result_lines.append(self._purge_context_db())
        if action in ("chat", "all"):
            result_lines.append(self._purge_chat_history())

        await query.edit_message_text("\n".join(result_lines))

    async def _handle_set_stream_callback(self, query, callback_data: str):
        """处理 /set_stream 选择按钮回调。"""
        if not query or not query.message:
            return
        if not self._message_handler:
            await query.answer("指令处理器未就绪", show_alert=True)
            return

        chat_id = str(query.message.chat.id)
        action = callback_data.split(":", 1)[1] if ":" in callback_data else ""
        if action not in ("on", "off"):
            await query.answer("未知选项", show_alert=True)
            return

        event_context = await self._callback_context(query, f"/set_stream {action}")

        await self._message_handler(event_context)

        status_text = "✅ 已启用一次性输出" if action == "on" else "✅ 已恢复流式输出"
        try:
            await query.edit_message_text(status_text)
        except Exception:
            logger.debug(f"[{chat_id}] set_stream edit_message_text 失败", exc_info=True)
        try:
            await query.answer(status_text, show_alert=False)
        except Exception:
            pass

    async def _handle_custom_scope_callback(self, query, callback_data: str):
        """处理 CUSTOM scope 按钮回调。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
        action = callback_data.split(":", 1)[1]

        if action == "close":
            scopes = self._load_custom_scopes()
            await query.edit_message_text(self._render_custom_scope_text(scopes))
            return

        if action not in {"fast", "smart", "image", "coder", "none"}:
            await query.edit_message_text("❌ 无效范围。")
            return

        try:
            scopes = self._load_custom_scopes()
            # Toggle logic
            if action == "none":
                # 切换 none：如果当前是 none 则恢复为 all（空列表），否则设为 none
                if any(s in ("none", "off") for s in scopes):
                    scopes = []
                else:
                    scopes = ["none"]
            else:
                scopes = [s for s in scopes if s not in ("none", "off")]
                if action in scopes:
                    scopes = [s for s in scopes if s != action]
                else:
                    scopes.append(action)

            self._save_custom_scopes(scopes)

            # Refresh keyboard & text
            scopes = self._load_custom_scopes()
            await query.edit_message_text(
                self._render_custom_scope_text(scopes),
                reply_markup=self._render_custom_scope_keyboard(scopes),
            )
        except Exception as e:
            logger.error(f"[{chat_id}] custom_scope 回调失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 失败: {e}")

    def _load_custom_scopes(self) -> list:
        cfg = config.get_config() or {}
        scopes = (cfg.get("custom_injection", {}) or {}).get("scopes") or []
        # normalize
        scopes = [str(s).strip().lower() for s in scopes if str(s).strip()]
        return scopes

    def _save_custom_scopes(self, scopes: list):
        config.set_custom_injection_scopes(scopes)

    def _render_custom_scope_text(self, scopes: list) -> str:
        if any(s in ("none", "off") for s in scopes):
            desc = "none"
        elif not scopes:
            desc = "all"
        else:
            desc = ", ".join(scopes)
        return (
            "🧭 CUSTOM 注入范围\n"
            f"当前: {desc}\n"
            "点击勾选/取消需要的 scope，实时保存。\n"
            "空列表=全量注入，none=关闭全部。"
        )

    def _render_custom_scope_keyboard(self, scopes: list) -> InlineKeyboardMarkup:
        def mark(name: str) -> str:
            return "✅ " if name in scopes else "☑ "

        none_mark = "✅ " if any(s in ("none", "off") for s in scopes) else "☑ "
        rows = [
            [
                InlineKeyboardButton(f"{mark('fast')}fast", callback_data="custom_scope:fast"),
                InlineKeyboardButton(f"{mark('smart')}smart", callback_data="custom_scope:smart"),
            ],
            [
                InlineKeyboardButton(f"{mark('image')}image", callback_data="custom_scope:image"),
                InlineKeyboardButton(f"{mark('coder')}coder", callback_data="custom_scope:coder"),
            ],
            [InlineKeyboardButton(f"{none_mark}关闭全部", callback_data="custom_scope:none")],
            [InlineKeyboardButton("取消", callback_data="custom_scope:close")],
        ]
        return InlineKeyboardMarkup(rows)

    async def _handle_nora_prefs_callback(self, query, callback_data: str):
        """处理 Nora 偏好按钮回调。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
        action = callback_data.split(":", 1)[1]

        if action == "close":
            prefs = self._load_nora_preferences()
            await query.edit_message_text(self._render_nora_prefs_text(prefs))
            return

        editable = {
            "nora_followup_probability",
            "proactive_message_probability",
            "split_reply_probability",
            "short_reply_preference",
            "verbosity_preference",
            "pause_between_splits_seconds",
            "warmth_level",
            "playfulness_level",
            "emotional_expressiveness",
            "assertiveness_level",
        }
        if action not in editable:
            await query.edit_message_text("❌ 无效偏好项。")
            return

        try:
            prefs = self._load_nora_preferences()
            prefs[action] = self._next_nora_pref_value(action, prefs.get(action))
            self._save_nora_preferences(prefs)
            prefs = self._load_nora_preferences()
            await query.edit_message_text(
                self._render_nora_prefs_text(prefs),
                reply_markup=self._render_nora_prefs_keyboard(prefs),
            )
        except Exception as e:
            logger.error(f"[{chat_id}] nora_prefs 回调失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 失败: {e}")

    def _load_nora_preferences(self) -> dict:
        return dict(config.get_nora_preferences())

    def _save_nora_preferences(self, prefs: dict):
        config.set_nora_preferences(prefs)

    def _format_nora_pref_value(self, key: str, value: Any) -> str:
        if key == "pause_between_splits_seconds":
            return f"{float(value):.1f}s"
        return f"{float(value):.2f}"

    def _next_nora_pref_value(self, key: str, current: Any) -> float:
        defaults = config.get_nora_preferences()
        try:
            value = float(current)
        except (TypeError, ValueError):
            value = float(defaults.get(key, 0.0))

        if key == "pause_between_splits_seconds":
            choices = [0.3, 0.6, 1.0, 1.4, 1.8, 2.2]
        else:
            choices = [0.0, 0.25, 0.5, 0.75, 1.0]

        rounded = round(value, 1 if key == "pause_between_splits_seconds" else 2)
        for idx, candidate in enumerate(choices):
            if abs(candidate - rounded) < 1e-6:
                return choices[(idx + 1) % len(choices)]
        higher = [candidate for candidate in choices if candidate > rounded]
        if higher:
            return higher[0]
        return choices[0]

    def _render_nora_prefs_text(self, prefs: dict) -> str:
        lines = [
            "🎛️ Nora 偏好设置",
            "点击按钮循环切换数值，修改会立即保存到 `config.yml`。",
            "",
            f"- followup 概率: {self._format_nora_pref_value('nora_followup_probability', prefs.get('nora_followup_probability', 1.0))}",
            f"- 自主主动消息概率: {self._format_nora_pref_value('proactive_message_probability', prefs.get('proactive_message_probability', 1.0))}",
            f"- 分段聊天倾向: {self._format_nora_pref_value('split_reply_probability', prefs.get('split_reply_probability', 0.7))}",
            f"- 短回复偏好: {self._format_nora_pref_value('short_reply_preference', prefs.get('short_reply_preference', 0.5))}",
            f"- 详细解释偏好: {self._format_nora_pref_value('verbosity_preference', prefs.get('verbosity_preference', 0.5))}",
            f"- 默认分段停顿: {self._format_nora_pref_value('pause_between_splits_seconds', prefs.get('pause_between_splits_seconds', 1.0))}",
            f"- 温柔度: {self._format_nora_pref_value('warmth_level', prefs.get('warmth_level', 0.7))}",
            f"- 俏皮度: {self._format_nora_pref_value('playfulness_level', prefs.get('playfulness_level', 0.4))}",
            f"- 情绪表达: {self._format_nora_pref_value('emotional_expressiveness', prefs.get('emotional_expressiveness', 0.6))}",
            f"- 主张程度: {self._format_nora_pref_value('assertiveness_level', prefs.get('assertiveness_level', 0.5))}",
        ]
        return "\n".join(lines)

    def _render_nora_prefs_keyboard(self, prefs: dict) -> InlineKeyboardMarkup:
        def button(label: str, key: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                f"{label}: {self._format_nora_pref_value(key, prefs.get(key))}",
                callback_data=f"nora_prefs:{key}",
            )

        rows = [
            [button("Followup", "nora_followup_probability")],
            [button("Proactive", "proactive_message_probability")],
            [button("Split", "split_reply_probability"), button("Pause", "pause_between_splits_seconds")],
            [button("Short", "short_reply_preference"), button("Verbose", "verbosity_preference")],
            [button("Warmth", "warmth_level"), button("Playful", "playfulness_level")],
            [button("Emotion", "emotional_expressiveness"), button("Assertive", "assertiveness_level")],
            [InlineKeyboardButton("关闭", callback_data="nora_prefs:close")],
        ]
        return InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------------
    # 推理强度（/effort）
    # ------------------------------------------------------------------

    # 档位顺序与 config.EFFORT_LEVELS 一致，外加"跟随全局"（清除该别名的覆盖）。
    _EFFORT_CHOICES = ("none", "minimal", "low", "medium", "high")

    def _build_effort_alias_menu(self):
        """构造 /effort 顶层 alias 选择菜单，标出每个别名的当前档位。"""
        from .commands import MODEL_ALIASES

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}
        global_effort = config.normalize_effort(llm_cfg.get("effort"))
        per_alias = llm_cfg.get("effort_by_alias", {}) or {}

        buttons = []
        for alias in MODEL_ALIASES:
            # 未配置模型的别名不显示：给它设 effort 没有意义。
            if not models_cfg.get(alias):
                continue
            own = config.normalize_effort(per_alias.get(alias))
            if own is not None:
                shown = own
            elif global_effort is not None:
                shown = f"{global_effort}(全局)"
            else:
                shown = "默认"
            buttons.append([
                InlineKeyboardButton(f"{alias}: {shown}", callback_data=f"effort_pick:{alias}")
            ])

        if not buttons:
            buttons.append([InlineKeyboardButton("（没有已配置的模型）", callback_data="effort_pick:cancel")])
        buttons.append([InlineKeyboardButton("关闭", callback_data="effort_pick:cancel")])

        global_desc = global_effort or "未设置"
        text = (
            "🧩 <b>推理强度 (reasoning effort)</b>\n"
            f"全局默认: <code>{global_desc}</code>\n"
            "点击别名为它单独设置档位，立即保存到 config.yml。\n"
            "<b>默认</b>=不传该字段（用端点自己的行为），<b>none</b>=显式关闭思考。\n"
            "⚠️ 改动在下次创建该模型客户端时生效；不支持推理的模型会自动回退。"
        )
        return text, InlineKeyboardMarkup(buttons)

    def _build_effort_level_menu(self, alias: str):
        """构造某个 alias 的档位选择菜单。"""
        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        per_alias = llm_cfg.get("effort_by_alias", {}) or {}
        current = config.normalize_effort(per_alias.get(alias))
        global_effort = config.normalize_effort(llm_cfg.get("effort"))

        rows = []
        row = []
        for level in self._EFFORT_CHOICES:
            mark = "✅ " if current == level else "☑ "
            row.append(InlineKeyboardButton(f"{mark}{level}", callback_data=f"effort_set:{alias}:{level}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        follow_mark = "✅ " if current is None else "☑ "
        rows.append([InlineKeyboardButton(
            f"{follow_mark}跟随全局({global_effort or '默认'})",
            callback_data=f"effort_set:{alias}:inherit",
        )])
        rows.append([
            InlineKeyboardButton("⬅ 返回", callback_data="effort_pick:back"),
            InlineKeyboardButton("取消", callback_data="effort_pick:cancel"),
        ])

        text = (
            f"🧩 <b>{alias}</b> 的推理强度\n"
            f"当前: <code>{current or ('跟随全局: ' + (global_effort or '默认'))}</code>\n"
            "档位越高，模型思考越久、越贵，首字延迟也越长。"
        )
        return text, InlineKeyboardMarkup(rows)

    async def _handle_effort_pick_callback(self, query, callback_data: str):
        """处理 /effort 顶层菜单回调（选别名 / 返回 / 取消）。"""
        if not query or not query.message:
            return
        alias = callback_data.split(":", 1)[1]

        if alias == "cancel":
            await query.edit_message_text("已关闭推理强度设置。")
            return

        if alias == "back":
            text, markup = self._build_effort_alias_menu()
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return

        text, markup = self._build_effort_level_menu(alias)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    async def _handle_effort_set_callback(self, query, callback_data: str):
        """写入某个 alias 的推理强度档位。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)

        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("❌ 无效的档位数据。")
            return
        _, alias, level = parts

        # "inherit" 表示清除该别名的覆盖，回落全局 llm.effort。
        value = None if level == "inherit" else level
        if value is not None and value not in self._EFFORT_CHOICES:
            await query.edit_message_text("❌ 无效档位。")
            return

        try:
            config.set_llm_effort_by_alias(alias, value)
            logger.info(f"[{chat_id}] /effort 已设置 {alias} -> {value or 'inherit'}")
            text, markup = self._build_effort_level_menu(alias)
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"[{chat_id}] effort_set 回调失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 失败: {e}")

    async def _handle_model_pick_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        alias = callback_data.split(":", 1)[1]

        if alias == "cancel":
            await query.edit_message_text("已关闭模型切换。")
            return

        if alias == "back":
            text, markup = self._build_model_alias_menu()
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        providers_cfg = llm_cfg.get("providers", {}) or {}
        provider_names = list(providers_cfg.keys())

        if not provider_names:
            default_provider = llm_cfg.get("provider", "gemini")
            provider_names = [default_provider]

        buttons = []
        for provider_name in provider_names:
            provider_type = (providers_cfg.get(provider_name, {}) or {}).get("type", provider_name)
            label = f"{provider_name} ({provider_type})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model_provider:{alias}:{provider_name}")])
        # 可选模型（video, security, fast-image, draw, draw_desc）允许清除配置
        if alias in ("video", "security", "fast-image", "draw", "draw_desc"):
            buttons.append([InlineKeyboardButton("🗑 清除（不使用）", callback_data=f"model_clear:{alias}")])
        buttons.append([
            InlineKeyboardButton("⬅ 返回", callback_data="model_pick:back"),
            InlineKeyboardButton("取消", callback_data="model_pick:cancel"),
        ])

        await query.edit_message_text(
            f"请选择 {alias} 的 Provider：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _handle_model_clear_callback(self, query, callback_data: str):
        """清除可选模型（video/security）的配置。"""
        alias = callback_data.split(":", 1)[1]
        try:
            cfg = config.get_config() or {}
            llm_cfg = cfg.setdefault("llm", {})
            models_cfg = llm_cfg.setdefault("models", {})
            model_providers = llm_cfg.setdefault("model_providers", {})
            models_cfg.pop(alias, None)
            model_providers.pop(alias, None)
            config.save_config(cfg)
            await query.edit_message_text(f"✅ 已清除 {alias} 模型配置。")
        except Exception as e:
            await query.edit_message_text(f"❌ 清除失败: {e}")

    async def _apply_model_update(self, update: Update, alias: str, model_name: str):
        if not update.effective_chat or not update.message:
            return

        try:
            cfg = config.get_config() or {}
            llm_cfg = cfg.setdefault("llm", {})
            models_cfg = llm_cfg.setdefault("models", {})
            models_cfg[alias] = model_name
            config.save_config(cfg)
            await update.message.reply_text(f"✅ 已更新 {alias} 模型为: {model_name}")
        except Exception as e:
            await update.message.reply_text(f"❌ 更新失败: {e}")

    async def _handle_model_set_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ 回调参数错误。")
            return
        _, alias, token = parts
        token_entry = self._model_option_tokens.pop(token, None)
        if not token_entry or token_entry.get("kind") != "model_set":
            await query.edit_message_text("⚠️ 选项已过期，请重新发起 /model。")
            return
        chat_id = token_entry.get("chat_id")
        model_name = token_entry.get("model")
        provider_name = token_entry.get("provider")
        if str(query.message.chat.id) != str(chat_id):
            await query.edit_message_text("⚠️ 当前会话无法使用该选项。")
            return

        try:
            cfg = config.get_config() or {}
            llm_cfg = cfg.setdefault("llm", {})
            models_cfg = llm_cfg.setdefault("models", {})
            models_cfg[alias] = model_name
            if provider_name:
                llm_cfg.setdefault("model_providers", {})[alias] = provider_name
            config.save_config(cfg)
            await query.edit_message_text(f"✅ 已更新 {alias} 模型为: {model_name}\n正在刷新模型...")

            if self._message_handler:
                event_context = await self._callback_context(query, "/reload_models")
                await self._message_handler(event_context)
        except Exception as e:
            await query.edit_message_text(f"❌ 更新失败: {e}")

    async def _handle_model_page_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ 回调参数错误。")
            return
        _, alias, token = parts
        token_entry = self._model_option_tokens.pop(token, None)
        if not token_entry or token_entry.get("kind") != "model_page":
            await query.edit_message_text("⚠️ 选项已过期，请重新发起 /model。")
            return
        chat_id = token_entry.get("chat_id")
        model_options = token_entry.get("models") or []
        offset = int(token_entry.get("offset") or 0)
        provider_name = token_entry.get("provider") or ""
        if str(query.message.chat.id) != str(chat_id):
            await query.edit_message_text("⚠️ 当前会话无法使用该选项。")
            return

        if not model_options:
            await query.edit_message_text("⚠️ 未找到更多模型。")
            return

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}
        current_model = models_cfg.get(alias, "")

        await self._render_model_page(
            query,
            alias=alias,
            provider_name=provider_name,
            model_options=model_options,
            offset=offset,
        )

    async def _handle_model_provider_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ 回调参数错误。")
            return
        _, alias, provider_name = parts

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}
        current_model = models_cfg.get(alias, "")

        provider_cfg = self._get_provider_config(cfg, provider_name)
        provider_models = self._fetch_provider_models(provider_name, provider_cfg)
        model_options = list(dict.fromkeys(m for m in provider_models if m))
        if current_model and current_model not in model_options:
            model_options.insert(0, current_model)

        if not model_options:
            await query.edit_message_text("⚠️ 未找到可选模型，请检查 provider API Key 或网络。")
            return

        await self._render_model_page(
            query,
            alias=alias,
            provider_name=provider_name,
            model_options=model_options,
            offset=0,
        )

    async def _render_model_page(
        self,
        query,
        alias: str,
        provider_name: str,
        model_options: List[str],
        offset: int,
        page_size: int = 12,
    ):
        if not query or not query.message:
            return

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        current_model = (llm_cfg.get("models", {}) or {}).get(alias, "")

        start = max(offset, 0)
        end = min(start + page_size, len(model_options))
        page_models = model_options[start:end]
        buttons = []
        for model_name in page_models:
            token = uuid.uuid4().hex[:10]
            self._model_option_tokens[token] = {
                "kind": "model_set",
                "chat_id": str(query.message.chat.id),
                "model": model_name,
                "provider": provider_name,
            }
            label = f"{model_name}"
            if model_name == current_model:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model_set:{alias}:{token}")])

        nav_buttons = []
        if start > 0:
            prev_token = uuid.uuid4().hex[:10]
            self._model_option_tokens[prev_token] = {
                "kind": "model_page",
                "chat_id": str(query.message.chat.id),
                "models": model_options,
                "offset": max(start - page_size, 0),
                "provider": provider_name,
            }
            nav_buttons.append(InlineKeyboardButton("◀ 上一页", callback_data=f"model_page:{alias}:{prev_token}"))
        if end < len(model_options):
            next_token = uuid.uuid4().hex[:10]
            self._model_option_tokens[next_token] = {
                "kind": "model_page",
                "chat_id": str(query.message.chat.id),
                "models": model_options,
                "offset": end,
                "provider": provider_name,
            }
            nav_buttons.append(InlineKeyboardButton("下一页 ▶", callback_data=f"model_page:{alias}:{next_token}"))

        if nav_buttons:
            buttons.append(nav_buttons)
        buttons.append([
            InlineKeyboardButton("⬅ 返回", callback_data=f"model_pick:{alias}"),
            InlineKeyboardButton("取消", callback_data="model_pick:cancel"),
        ])

        await query.edit_message_text(
            f"选择 {alias} 的模型（来源: {provider_name}）：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
