'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-02 22:09:36
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from email.header import decode_header
from email.message import Message
from typing import Any, Dict, List

from brain.prompts import render_template
from triggers.base import BaseTrigger, TriggerEvent, TriggerFeatures

logger = logging.getLogger(__name__)


class EmailTrigger(BaseTrigger):
    """IMAP 邮件触发器：轮询新邮件并用 fast 模型判断是否通知 Nora。"""

    def __init__(self, cfg: Dict[str, Any], default_chat_id: str = ""):
        super().__init__()
        self.cfg = cfg
        self.default_chat_id = default_chat_id
        self._poll_task: asyncio.Task | None = None
        self._seen_uids: set[str] = set()
        self._warned_missing_config = False

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
        logger.info(f"[trigger:email] 轮询已启动，interval={max(10, poll_interval)}s")
        while self.is_running:
            try:
                emails = await asyncio.to_thread(self._fetch_recent_emails)
                logger.debug(f"[trigger:email] 本轮拉取邮件数量: {len(emails)}")
                for item in emails:
                    await self._handle_email(item)
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
                if uid and uid in self._seen_uids:
                    continue

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

                parsed.append(
                    {
                        "uid": uid,
                        "from": sender,
                        "subject": subject,
                        "date": date_str,
                        "body_preview": body_preview,
                    }
                )
                if uid:
                    self._seen_uids.add(uid)

        return parsed

    async def _handle_email(self, email_data: Dict[str, str]) -> None:
        system_prompt = render_template("trigger_email.jinja", "email_filter_system")
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
            history=[],
        )

        decision = self._parse_filter_decision(decision_text)
        logger.debug(
            f"[trigger:email] 过滤判定: decision={decision.get('decision')}, subject={email_data.get('subject', '')[:60]}"
        )
        if decision["decision"] != "NOTIFY":
            return

        sender = email_data.get("from", "(未知发件人)")
        subject = email_data.get("subject", "(无主题)")
        reason = decision.get("reason") or "邮件命中通知规则"
        event_reason = f"📧 新邮件提醒\n发件人: {sender}\n主题: {subject}\n判定: {reason}"

        event = TriggerEvent(
            trigger_name=self.trigger_name,
            source="email",
            event_type="trigger_email",
            reason=event_reason,
            payload=email_data,
            chat_id=(self.cfg.get("chat_id") or self.default_chat_id or None),
        )
        logger.info(f"[trigger:email] 命中通知规则，准备分发事件: subject={subject[:80]}")
        await self.dispatch_event(event)

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
                out.append(text.decode(enc, errors="ignore"))
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
