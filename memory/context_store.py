'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-15 22:28:26
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from workspace_config import get_workspace_manager
from brain.prompts import render_template

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认路径
# ---------------------------------------------------------------------------

def get_default_message_log_db() -> Path:
    """工作区下的消息镜像库，存储所有原始用户/AI 消息的副本。"""
    workspace = get_workspace_manager()
    path = Path(workspace.data_dir) / "memory" / "message_log.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_default_context_db() -> Path:
    """工作区下的上下文压缩库，存储滑动窗口压缩结果。"""
    workspace = get_workspace_manager()
    path = Path(workspace.data_dir) / "memory" / "context_compression.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 消息镜像表：完整保存所有用户/AI 消息原文
# ---------------------------------------------------------------------------


class MessageLog:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else get_default_message_log_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content_with_timestamp TEXT NOT NULL,
                raw_content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_raw_messages_chat
            ON raw_messages(platform, chat_id, timestamp DESC)
            """
        )
        conn.commit()
        conn.close()

    def add_message(
        self,
        message_id: int,
        platform: str,
        chat_id: str,
        role: str,
        content_with_timestamp: str,
        raw_content: str,
        timestamp: float,
        metadata: Optional[Dict] = None,
    ) -> None:
        metadata_json = json.dumps(metadata) if metadata else None
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO raw_messages (message_id, platform, chat_id, role, content_with_timestamp, raw_content, timestamp, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                platform,
                chat_id,
                role,
                content_with_timestamp,
                raw_content,
                timestamp,
                metadata_json,
            ),
        )
        conn.commit()
        conn.close()

    def get_recent_messages(self, platform: str, chat_id: str, limit: int = 20) -> List[Dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM raw_messages
            WHERE platform = ? AND chat_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (platform, chat_id, limit),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows


# ---------------------------------------------------------------------------
# 上下文压缩存储：滑动窗口策略
# ---------------------------------------------------------------------------


class ContextCompressor:
    """
    负责按照规则生成上下文压缩片段：
      - 最新 3 段完整保留（若过长则单独压缩）
      - 第 4~6 段各自单独压缩
      - 第 7~10 段合并为一个压缩摘要
    生成的压缩内容仅存放在独立的 context DB，方便重启加载。
    """

    def __init__(
        self,
        message_log: MessageLog,
        db_path: Optional[str] = None,
        long_message_threshold: int = 1200,
    ):
        self.message_log = message_log
        self.db_path = Path(db_path) if db_path else get_default_context_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.long_message_threshold = long_message_threshold
        self._summarizer = None
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS context_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                slot INTEGER NOT NULL,
                segment_type TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                message_ids TEXT NOT NULL,  -- JSON array
                source_timestamp REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(platform, chat_id, slot)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_context_segments_chat
            ON context_segments(platform, chat_id, slot)
            """
        )
        conn.commit()
        conn.close()

    async def refresh_context(self, platform: str, chat_id: str) -> None:
        """重新生成指定会话的压缩上下文。"""
        messages = self.message_log.get_recent_messages(platform, chat_id, limit=20)
        if not messages:
            return

        existing = self._load_existing(platform, chat_id)
        slots: List[Dict] = []

        # 最新在前
        for idx, msg in enumerate(messages):
            slot = idx + 1
            if slot > 10:
                break

            raw_text = msg.get("raw_content", "")
            role = msg.get("role") or "user"
            timestamp = msg.get("timestamp", datetime.now().timestamp())
            message_id = msg.get("message_id")
            message_ids = [message_id] if message_id is not None else []

            if slot <= 3:
                if raw_text and len(raw_text) > self.long_message_threshold:
                    content = await self._summarize_single(raw_text, role, existing.get(slot), message_ids)
                    slots.append(
                        {
                            "slot": slot,
                            "segment_type": "compressed_recent",
                            "role": "system",
                            "content": f"[最近长消息摘要] {content}",
                            "message_ids": message_ids,
                            "source_timestamp": timestamp,
                        }
                    )
                else:
                    slots.append(
                        {
                            "slot": slot,
                            "segment_type": "raw_recent",
                            "role": role,
                            "content": msg.get("content_with_timestamp") or raw_text,
                            "message_ids": message_ids,
                            "source_timestamp": timestamp,
                        }
                    )
            elif slot <= 6:
                content = await self._summarize_single(raw_text, role, existing.get(slot), message_ids)
                slots.append(
                    {
                        "slot": slot,
                        "segment_type": "compressed_single",
                        "role": "system",
                        "content": f"[压缩段 #{slot}] {content}",
                        "message_ids": message_ids,
                        "source_timestamp": timestamp,
                    }
                )
            elif slot == 7:
                group_msgs = messages[6:10]  # slots 7-10
                if not group_msgs:
                    break
                group_ids = [m.get("message_id") for m in group_msgs if m.get("message_id") is not None]
                last_ts = max(m.get("timestamp", timestamp) for m in group_msgs)
                content = await self._summarize_group(group_msgs, existing.get(slot))
                slots.append(
                    {
                        "slot": slot,
                        "segment_type": "compressed_group",
                        "role": "system",
                        "content": f"[合并摘要 7-10] {content}",
                        "message_ids": group_ids,
                        "source_timestamp": last_ts,
                    }
                )
                break

        self._persist_segments(platform, chat_id, slots)

    def _load_existing(self, platform: str, chat_id: str) -> Dict[int, Dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM context_segments
            WHERE platform = ? AND chat_id = ?
            """,
            (platform, chat_id),
        )
        rows = {int(row["slot"]): dict(row) for row in cursor.fetchall()}
        conn.close()
        return rows

    async def _summarize_single(
        self,
        raw_text: str,
        role: str,
        existing_row: Optional[Dict],
        message_ids: List[int],
    ) -> str:
        if existing_row:
            try:
                cached_ids = json.loads(existing_row.get("message_ids", "[]"))
            except Exception:
                cached_ids = []
            if cached_ids == message_ids:
                return existing_row.get("content", "")

        prompt = render_template(
            "compression.jinja",
            "compress_user",
            conversation_text=f"{role}: {raw_text}",
        )
        return await self._call_summary(
            system_prompt=render_template("compression.jinja", "compress_system"),
            user_prompt=prompt,
            fallback_text=raw_text,
        )

    async def _summarize_group(self, messages: List[Dict], existing_row: Optional[Dict]) -> str:
        message_ids = [m.get("message_id") for m in messages if m.get("message_id") is not None]
        if existing_row:
            try:
                cached_ids = json.loads(existing_row.get("message_ids", "[]"))
            except Exception:
                cached_ids = []
            if cached_ids == message_ids:
                return existing_row.get("content", "")

        conversation_text = "\n".join([
            f"{m.get('role')}: {m.get('raw_content', '')}" for m in messages
        ])
        prompt = render_template("compression.jinja", "compress_user", conversation_text=conversation_text)
        return await self._call_summary(
            system_prompt=render_template("compression.jinja", "compress_system"),
            user_prompt=prompt,
            fallback_text=conversation_text,
        )

    async def _call_summary(self, system_prompt: str, user_prompt: str, fallback_text: str) -> str:
        if self._summarizer is None:
            try:
                from brain.llm import get_llm_client

                self._summarizer = get_llm_client(model_alias="summary")
            except Exception as e:
                logger.warning("summary 模型不可用，使用回退压缩: %s", e)
                self._summarizer = None

        if self._summarizer is None:
            return self._fallback_summary(fallback_text)

        try:
            return await self._summarizer.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
            )
        except Exception as e:
            logger.error("调用 summary 模型失败，使用回退: %s", e)
            return self._fallback_summary(fallback_text)

    def _fallback_summary(self, text: str, max_length: int = 200) -> str:
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."

    def _persist_segments(self, platform: str, chat_id: str, slots: List[Dict]) -> None:
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        now = datetime.now().timestamp()
        for seg in slots:
            cursor.execute(
                """
                INSERT INTO context_segments (platform, chat_id, slot, segment_type, role, content, message_ids, source_timestamp, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, chat_id, slot) DO UPDATE SET
                    segment_type=excluded.segment_type,
                    role=excluded.role,
                    content=excluded.content,
                    message_ids=excluded.message_ids,
                    source_timestamp=excluded.source_timestamp,
                    updated_at=excluded.updated_at
                """,
                (
                    platform,
                    chat_id,
                    seg["slot"],
                    seg["segment_type"],
                    seg["role"],
                    seg["content"],
                    json.dumps(seg.get("message_ids", [])),
                    seg.get("source_timestamp", now),
                    now,
                ),
            )
        conn.commit()
        conn.close()

    def get_context_messages(self, platform: str, chat_id: str) -> List[Dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM context_segments
            WHERE platform = ? AND chat_id = ?
            ORDER BY slot DESC
            """,
            (platform, chat_id),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        messages = []
        for row in rows:
            messages.append(
                {
                    "role": row.get("role", "system"),
                    "content": row.get("content", ""),
                    "timestamp": row.get("source_timestamp", 0),
                    "segment_slot": row.get("slot"),
                    "segment_type": row.get("segment_type"),
                }
            )
        return messages


__all__ = [
    "MessageLog",
    "ContextCompressor",
    "get_default_message_log_db",
    "get_default_context_db",
]
