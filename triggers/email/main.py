'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-02 22:09:36
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import json
import logging
import os
import re
from email.header import decode_header
from email.message import Message
from typing import Any, Dict, List, Optional

from brain.prompts import render_template, get_soul_prompt, _read_file_safe, WORKSPACE_USER_FILE
from core.conversation_identity import build_identity_from_target, conversation_target_dict
from triggers.base import BaseTrigger, TriggerEvent, TriggerFeatures
from workspace_config import get_workspace_manager
from memory.message_history import MessageHistory

logger = logging.getLogger(__name__)


class EmailTrigger(BaseTrigger):
    """IMAP 邮件触发器：轮询新邮件并用 fast 模型判断是否通知 Nora。"""

    def __init__(
        self,
        cfg: Dict[str, Any],
        default_chat_id: str = "",
        default_chat_target: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self.cfg = cfg
        self.default_chat_id = default_chat_id
        self.default_chat_target = dict(default_chat_target or {})
        history_cfg = __import__("config").get_message_history_config()
        self.message_history = MessageHistory(
            db_path=history_cfg.get("db_path"),
            raw_window=history_cfg.get("raw_window", 50),
            compress_window=history_cfg.get("compress_window", 50),
            compress_ratio=history_cfg.get("compress_ratio", 10),
            archive_threshold=history_cfg.get("archive_threshold", 500),
            timezone=history_cfg.get("timezone", "Asia/Shanghai"),
            timestamp_format=history_cfg.get("timestamp_format", "%Y-%m-%d %H:%M:%S %A"),
            mirror_db_path=history_cfg.get("mirror_db_path"),
            context_db_path=history_cfg.get("context_db_path"),
            long_message_threshold=history_cfg.get("long_message_threshold", 1200),
        )
        self._poll_task: asyncio.Task | None = None
        self._reviewed_keys: set[str] = set()
        self._reviewed_order: List[str] = []
        self._reviewed_store_path = self._resolve_reviewed_store_path()
        self._reviewed_store_max = int(self.cfg.get("reviewed_store_max", 5000) or 5000)
        self._warned_missing_config = False
        self._poll_round = 0

    def _resolve_target(self) -> Dict[str, str]:
        configured_chat_id = str(self.cfg.get("chat_id") or "").strip()
        if configured_chat_id:
            identity = build_identity_from_target(
                {"runtime_key": configured_chat_id, "chat_type": "private"}
            )
            return conversation_target_dict(identity)
        if self.default_chat_target:
            identity = build_identity_from_target(self.default_chat_target)
            return conversation_target_dict(identity)
        if self.default_chat_id:
            identity = build_identity_from_target(
                {"runtime_key": self.default_chat_id, "chat_type": "private"}
            )
            return conversation_target_dict(identity)
        return {}

    @property
    def trigger_name(self) -> str:
        return "email"

    @property
    def trigger_features(self) -> TriggerFeatures:
        return TriggerFeatures(
            supports_background_polling=True,
            supports_model_call=True,
            supports_custom_hooks=True,
        )

    def run(self, event_handler):
        self._event_handler = event_handler
        self._load_reviewed_keys()
        logger.info("[trigger:email] 启动轮询任务")
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def on_shutdown(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        poll_interval = int(self.cfg.get("poll_interval_sec", 60) or 60)
        log_every = int(self.cfg.get("log_every_n_polls", 1) or 1)
        log_every = max(1, log_every)
        logger.info(f"[trigger:email] 轮询已启动，interval={max(10, poll_interval)}s")
        while self.is_running:
            try:
                self._poll_round += 1
                emails = await asyncio.to_thread(self._fetch_recent_emails)
                notified_count = 0
                for item in emails:
                    should_notify = await self._handle_email(item)
                    if should_notify:
                        notified_count += 1
                    review_key = item.get("review_key") or ""
                    if review_key:
                        self._mark_reviewed(review_key)

                if self._poll_round % log_every == 0:
                    logger.debug(
                        f"[trigger:email] 心跳 round={self._poll_round}, fetched={len(emails)}, notified={notified_count}, reviewed={len(self._reviewed_keys)}"
                    )
            except Exception as exc:
                logger.error(f"[trigger:email] 轮询失败: {exc}", exc_info=True)

            await asyncio.sleep(max(10, poll_interval))

    def _fetch_recent_emails(self) -> List[Dict[str, str]]:
        host = (self.cfg.get("host") or "").strip()
        username = (self.cfg.get("username") or "").strip()
        password = self.cfg.get("password") or ""
        port = int(self.cfg.get("port", 993) or 993)
        mailbox = (self.cfg.get("mailbox") or "INBOX").strip() or "INBOX"
        unseen_only = bool(self.cfg.get("unseen_only", True))
        max_fetch_per_poll = int(self.cfg.get("max_fetch_per_poll", 20) or 20)
        max_body_chars = int(self.cfg.get("max_body_chars", 1200) or 1200)

        if not host or not username or not password:
            if not self._warned_missing_config:
                logger.warning("[trigger:email] 配置不完整（host/username/password），轮询将跳过")
                self._warned_missing_config = True
            return []

        self._warned_missing_config = False

        criterion = "UNSEEN" if unseen_only else "ALL"
        parsed: List[Dict[str, str]] = []

        with imaplib.IMAP4_SSL(host, port) as mail:
            mail.login(username, password)
            status, _ = mail.select(mailbox)
            if status != "OK":
                return []

            status, data = mail.search(None, criterion)
            if status != "OK" or not data or not data[0]:
                return []

            ids = data[0].split()[-max_fetch_per_poll:]
            for msg_id in ids:
                status, fetched = mail.fetch(msg_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                uid = self._extract_uid(fetched)

                raw_bytes = None
                for part in fetched:
                    if isinstance(part, tuple) and len(part) == 2:
                        raw_bytes = part[1]
                        break
                if not raw_bytes:
                    continue

                msg = email.message_from_bytes(raw_bytes)
                sender = self._decode_header_value(msg.get("From", ""))
                subject = self._decode_header_value(msg.get("Subject", "(无主题)"))
                date_str = self._decode_header_value(msg.get("Date", ""))
                body = self._extract_text_body(msg)
                body_preview = (body or "").strip().replace("\r", " ").replace("\n", " ")
                if len(body_preview) > max_body_chars:
                    body_preview = body_preview[:max_body_chars] + "..."

                message_id = self._decode_header_value(msg.get("Message-ID", ""))
                review_key = self._build_review_key(
                    uid=uid,
                    message_id=message_id,
                    sender=sender,
                    subject=subject,
                    date_str=date_str,
                    body_preview=body_preview,
                )
                if review_key in self._reviewed_keys:
                    continue

                parsed.append(
                    {
                        "review_key": review_key,
                        "uid": uid,
                        "message_id": message_id,
                        "from": sender,
                        "subject": subject,
                        "date": date_str,
                        "body_preview": body_preview,
                    }
                )

        return parsed

    async def _handle_email(self, email_data: Dict[str, str]) -> bool:
        system_prompt = render_template("trigger_email.jinja", "email_filter_system")
        history: List[Dict[str, str]] = []

        try:
            target = self._resolve_target()
            if target:
                platform = str(target.get("platform") or "telegram")
                storage_id = str(target.get("storage_id") or target.get("chat_id") or "")
                db_context = self.message_history.get_context_messages(platform, storage_id)
                history = [
                    {
                        "role": str(msg.get("role", "")).strip(),
                        "content": str(msg.get("content", "")).strip(),
                    }
                    for msg in db_context
                    if str(msg.get("role", "")).strip() in ("user", "assistant")
                    and str(msg.get("content", "")).strip()
                ]
        except Exception as exc:
            logger.warning(f"[trigger:email] 加载前脑完整上下文失败，将使用空 history: {exc}")

        try:
            soul_prompt = get_soul_prompt()
            user_profile = _read_file_safe(WORKSPACE_USER_FILE, max_chars=2000)

            context_blocks: List[str] = []
            if soul_prompt:
                context_blocks.append(f"<soul>\n{soul_prompt}\n</soul>")
            if user_profile:
                context_blocks.append(f"<user_profile>\n{user_profile}\n</user_profile>")

            if context_blocks:
                system_prompt = (
                    system_prompt
                    + "\n\n【审查上下文】以下为 Nora 的人设与用户信息，请据此判断邮件提醒优先级。\n"
                    + "\n\n".join(context_blocks)
                )
        except Exception as exc:
            logger.warning(f"[trigger:email] 注入 SOUL/USER 上下文失败，将使用基础规则审查: {exc}")

        user_prompt = render_template(
            "trigger_email.jinja",
            "email_filter_user",
            sender=email_data.get("from", ""),
            subject=email_data.get("subject", ""),
            received_at=email_data.get("date", ""),
            body_preview=email_data.get("body_preview", ""),
        )

        decision_text = await self.call_model(
            model_alias="fast",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history=history,
        )

        decision = self._parse_filter_decision(decision_text)
        decision_value = str(decision.get("decision", "SKIP")).upper()
        decision_reason = (decision.get("reason") or "").strip()
        subject_preview = email_data.get("subject", "")[:80]
        logger.info(
            f"[trigger:email] 审查结果: decision={decision_value}, reason={decision_reason[:120]}, subject={subject_preview}"
        )

        if decision_value != "NOTIFY":
            return False

        sender = email_data.get("from", "(未知发件人)")
        subject = email_data.get("subject", "(无主题)")
        reason = decision_reason or "邮件命中通知规则"
        event_reason = f"📧 新邮件提醒\n发件人: {sender}\n主题: {subject}\n判定: {reason}"

        event = TriggerEvent(
            trigger_name=self.trigger_name,
            source="email",
            event_type="trigger_email",
            reason=event_reason,
            payload=email_data,
            chat_id=(self._resolve_target().get("runtime_key") or None),
        )
        logger.info(f"[trigger:email] 命中通知规则，准备分发事件: subject={subject[:80]}")
        await self.dispatch_event(event)
        return True

    def _resolve_reviewed_store_path(self) -> str:
        configured = (self.cfg.get("reviewed_store_path") or "").strip()
        if configured:
            return os.path.abspath(os.path.expanduser(configured))

        ws = get_workspace_manager()
        return os.path.join(ws.cache_dir, "email_trigger_reviewed.json")

    def _load_reviewed_keys(self) -> None:
        path = self._reviewed_store_path
        try:
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self._reviewed_keys = set()
                self._reviewed_order = []
                logger.info(f"[trigger:email] 已审查名单不存在，将新建: {path}")
                return

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            keys = data.get("keys", []) if isinstance(data, dict) else []
            if not isinstance(keys, list):
                keys = []

            normalized = [str(k).strip() for k in keys if str(k).strip()]
            self._reviewed_order = normalized[-self._reviewed_store_max:]
            self._reviewed_keys = set(self._reviewed_order)
            logger.info(f"[trigger:email] 已加载已审查名单: {len(self._reviewed_keys)} 条")
        except Exception as e:
            logger.warning(f"[trigger:email] 加载已审查名单失败，将使用空名单: {e}")
            self._reviewed_keys = set()
            self._reviewed_order = []

    def _save_reviewed_keys(self) -> None:
        path = self._reviewed_store_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"keys": self._reviewed_order}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[trigger:email] 保存已审查名单失败: {e}")

    def _mark_reviewed(self, review_key: str) -> None:
        key = (review_key or "").strip()
        if not key or key in self._reviewed_keys:
            return

        self._reviewed_keys.add(key)
        self._reviewed_order.append(key)

        if len(self._reviewed_order) > self._reviewed_store_max:
            overflow = len(self._reviewed_order) - self._reviewed_store_max
            removed = self._reviewed_order[:overflow]
            self._reviewed_order = self._reviewed_order[overflow:]
            for item in removed:
                self._reviewed_keys.discard(item)

        self._save_reviewed_keys()

    @staticmethod
    def _build_review_key(
        *,
        uid: str,
        message_id: str,
        sender: str,
        subject: str,
        date_str: str,
        body_preview: str,
    ) -> str:
        if uid:
            return f"uid:{uid}"
        if message_id:
            return f"mid:{message_id.strip().lower()}"

        fallback_src = "|".join([
            (sender or "").strip().lower(),
            (subject or "").strip().lower(),
            (date_str or "").strip().lower(),
            (body_preview or "").strip().lower()[:200],
        ])
        digest = hashlib.sha1(fallback_src.encode("utf-8", errors="ignore")).hexdigest()
        return f"fallback:{digest}"

    @staticmethod
    def _extract_uid(fetch_data: List[Any]) -> str:
        for item in fetch_data:
            if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                match = re.search(rb"UID\s+(\d+)", item[0])
                if match:
                    return match.group(1).decode("utf-8", errors="ignore")
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        parts = decode_header(value)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                enc = charset or "utf-8"
                try:
                    out.append(text.decode(enc, errors="ignore"))
                except (LookupError, UnicodeDecodeError):
                    out.append(text.decode("utf-8", errors="ignore"))
            else:
                out.append(text)
        return "".join(out).strip()

    @staticmethod
    def _extract_text_body(msg: Message) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition", "")).lower()
                if ctype == "text/plain" and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if payload is None or not isinstance(payload, (bytes, bytearray)):
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
            return ""

        payload = msg.get_payload(decode=True)
        if payload is None or not isinstance(payload, (bytes, bytearray)):
            return ""
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")

    @staticmethod
    def _parse_filter_decision(text: str) -> Dict[str, str]:
        raw = (text or "").strip()
        cleaned = raw.removeprefix("```").removesuffix("```").strip()

        # 尝试 JSON 解析
        try:
            import json

            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                decision = str(obj.get("decision", "SKIP")).upper().strip()
                reason = str(obj.get("reason", "")).strip()
                if decision not in {"NOTIFY", "SKIP"}:
                    decision = "SKIP"
                return {"decision": decision, "reason": reason}
        except Exception:
            pass

        # 兜底解析关键字
        upper = cleaned.upper()
        if "NOTIFY" in upper:
            return {"decision": "NOTIFY", "reason": cleaned[:120]}
        return {"decision": "SKIP", "reason": cleaned[:120]}
