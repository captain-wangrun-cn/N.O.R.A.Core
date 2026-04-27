'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-15 22:28:26
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import json
import logging
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any

from workspace_config import get_workspace_manager
from brain.prompts import render_template
from brain.prompts import load_identity_context

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

    def delete_by_message_id(self, message_id: int) -> None:
        """删除指定 message_id 的镜像记录。"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM raw_messages WHERE message_id = ?",
            (message_id,),
        )
        conn.commit()
        conn.close()


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
        history_db_path: Optional[str] = None,
        summary_max_retries: int = 3,
        summary_retry_base_delay: float = 0.8,
        summary_min_chars: int = 80,
        retry_callback: Optional[Callable[[str, str, str, Optional[Dict[str, Any]], Optional[str]], None]] = None,
        success_callback: Optional[Callable[[str, str, str, Optional[Dict[str, Any]]], None]] = None,
    ):
        self.message_log = message_log
        self.db_path = Path(db_path) if db_path else get_default_context_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.long_message_threshold = long_message_threshold
        self.history_db_path = Path(history_db_path) if history_db_path else None
        self.summary_max_retries = max(1, int(summary_max_retries))
        self.summary_retry_base_delay = max(0.1, float(summary_retry_base_delay))
        self.summary_min_chars = max(20, int(summary_min_chars))
        self.retry_callback = retry_callback
        self.success_callback = success_callback
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
        """按对话段落(session)重新生成压缩上下文。"""
        try:
            if not self.history_db_path:
                logger.warning("ContextCompressor 未配置 history_db_path，跳过段压缩")
                return

            segment_refs = self._get_recent_segment_refs(platform, chat_id, limit=10)
            if not segment_refs:
                self._mark_refresh_success(platform, chat_id)
                return

            existing = self._load_existing(platform, chat_id)
            slots: List[Dict] = []

            for idx, seg_ref in enumerate(segment_refs):
                slot = idx + 1
                if slot > 10:
                    break

                seg_text, message_count = self._build_segment_text(platform, chat_id, seg_ref)
                if not seg_text:
                    continue

                seg_role = "system"
                seg_ts = float(seg_ref.get("source_timestamp", datetime.now().timestamp()))
                source_keys = [str(seg_ref.get("source_key", ""))]

                if slot <= 3:
                    if len(seg_text) > self.long_message_threshold:
                        content = await self._summarize_single(
                            seg_text,
                            seg_role,
                            existing.get(slot),
                            source_keys,
                            min_chars=self._get_required_min_chars(message_count),
                        )
                        slots.append(
                            {
                                "slot": slot,
                                "segment_type": "compressed_recent_segment",
                                "role": "system",
                                "content": f"[最近长段摘要#{slot}] {content}",
                                "source_keys": source_keys,
                                "source_timestamp": seg_ts,
                            }
                        )
                    else:
                        slots.append(
                            {
                                "slot": slot,
                                "segment_type": "raw_recent_segment",
                                "role": "system",
                                "content": f"[最近段#{slot}]\n{seg_text}",
                                "source_keys": source_keys,
                                "source_timestamp": seg_ts,
                            }
                        )
                elif slot <= 6:
                    content = await self._summarize_single(
                        seg_text,
                        seg_role,
                        existing.get(slot),
                        source_keys,
                        min_chars=self._get_required_min_chars(message_count),
                    )
                    slots.append(
                        {
                            "slot": slot,
                            "segment_type": "compressed_single_segment",
                            "role": "system",
                            "content": f"[压缩段#{slot}] {content}",
                            "source_keys": source_keys,
                            "source_timestamp": seg_ts,
                        }
                    )
                elif slot == 7:
                    group_refs = segment_refs[6:10]
                    if not group_refs:
                        break
                    group_data = [self._build_segment_text(platform, chat_id, r) for r in group_refs]
                    group_texts = [t for t, c in group_data if t]
                    group_counts = [c for t, c in group_data if t]
                    if not group_texts:
                        break
                    total_group_messages = sum(group_counts)
                    group_keys = [str(r.get("source_key", "")) for r in group_refs]
                    last_ts = max(float(r.get("source_timestamp", seg_ts)) for r in group_refs)
                    content = await self._summarize_group(
                        group_texts,
                        existing.get(slot),
                        group_keys,
                        min_chars=self._get_required_min_chars(total_group_messages),
                    )
                    slots.append(
                        {
                            "slot": slot,
                            "segment_type": "compressed_group_segments",
                            "role": "system",
                            "content": f"[合并摘要段7-10] {content}",
                            "source_keys": group_keys,
                            "source_timestamp": last_ts,
                        }
                    )
                    break

            self._persist_segments(platform, chat_id, slots)
            self._mark_refresh_success(platform, chat_id)
        except Exception as e:
            self._enqueue_refresh_retry(platform, chat_id, e)
            raise

    def _enqueue_refresh_retry(self, platform: str, chat_id: str, error: Exception) -> None:
        if self.retry_callback:
            self.retry_callback("context_refresh", platform, chat_id, None, str(error))

    def _mark_refresh_success(self, platform: str, chat_id: str) -> None:
        if self.success_callback:
            self.success_callback("context_refresh", platform, chat_id, None)

    def _get_recent_segment_refs(self, platform: str, chat_id: str, limit: int = 10) -> List[Dict]:
        """获取最近对话段（含当前活跃段）引用，按最新在前返回。"""
        if not self.history_db_path:
            return []

        conn = sqlite3.connect(str(self.history_db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        refs: List[Dict] = []

        cursor.execute(
            """
            SELECT id, timestamp FROM messages
            WHERE platform = ? AND chat_id = ? AND session_id IS NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (platform, chat_id),
        )
        active = cursor.fetchone()
        if active:
            cursor.execute(
                """
                SELECT id FROM messages
                WHERE platform = ? AND chat_id = ? AND session_id IS NULL
                ORDER BY timestamp ASC
                """,
                (platform, chat_id),
            )
            active_ids = [int(r[0]) for r in cursor.fetchall()]
            refs.append(
                {
                    "kind": "active",
                    "source_key": f"active:{','.join(map(str, active_ids))}",
                    "source_timestamp": float(active["timestamp"]),
                }
            )

        cursor.execute(
            """
            SELECT id, ended_at, message_count FROM conversation_sessions
            WHERE platform = ? AND chat_id = ?
            ORDER BY ended_at DESC
            LIMIT ?
            """,
            (platform, chat_id, limit),
        )
        for row in cursor.fetchall():
            refs.append(
                {
                    "kind": "closed",
                    "session_id": int(row["id"]),
                    "source_key": f"session:{int(row['id'])}:{int(row['message_count'] or 0)}",
                    "source_timestamp": float(row["ended_at"]),
                }
            )

        conn.close()
        refs.sort(key=lambda x: float(x.get("source_timestamp", 0)), reverse=True)
        return refs[:limit]

    def _build_segment_text(self, platform: str, chat_id: str, seg_ref: Dict) -> tuple[str, int]:
        """将一个段落引用展开为可供 summary 模型处理的文本，同时返回消息条数。"""
        if not self.history_db_path:
            return "", 0

        conn = sqlite3.connect(str(self.history_db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        kind = seg_ref.get("kind")
        if kind == "active":
            cursor.execute(
                """
                SELECT role, content FROM messages
                WHERE platform = ? AND chat_id = ? AND session_id IS NULL
                ORDER BY timestamp ASC
                """,
                (platform, chat_id),
            )
            rows = cursor.fetchall()
            conn.close()
            if not rows:
                return "", 0
            return "\n".join([f"{r['role']}: {r['content']}" for r in rows]), len(rows)

        session_id = seg_ref.get("session_id")
        if session_id is None:
            conn.close()
            return "", 0

        cursor.execute(
            """
            SELECT role, content FROM messages
            WHERE platform = ? AND chat_id = ? AND session_id = ?
            ORDER BY timestamp ASC
            """,
            (platform, chat_id, int(session_id)),
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return "", 0
        return "\n".join([f"{r['role']}: {r['content']}" for r in rows]), len(rows)

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
        source_keys: List[str],
        min_chars: Optional[int] = None,
    ) -> str:
        if existing_row:
            try:
                cached_ids = json.loads(existing_row.get("message_ids", "[]"))
            except Exception:
                cached_ids = []
            if cached_ids == source_keys:
                return self._strip_label(existing_row.get("content", ""))

        prompt = render_template(
            "compression.jinja",
            "compress_user",
            conversation_text=f"{role}: {raw_text}",
        )
        return await self._call_summary(
            system_prompt=render_template("compression.jinja", "compress_system"),
            user_prompt=prompt,
            fallback_text=raw_text,
            min_chars=min_chars,
        )

    async def _summarize_group(
        self,
        messages: List[str],
        existing_row: Optional[Dict],
        source_keys: List[str],
        min_chars: Optional[int] = None,
    ) -> str:
        if existing_row:
            try:
                cached_ids = json.loads(existing_row.get("message_ids", "[]"))
            except Exception:
                cached_ids = []
            if cached_ids == source_keys:
                return self._strip_label(existing_row.get("content", ""))

        conversation_text = "\n\n".join(messages)
        prompt = render_template("compression.jinja", "compress_user", conversation_text=conversation_text)
        return await self._call_summary(
            system_prompt=render_template("compression.jinja", "compress_system"),
            user_prompt=prompt,
            fallback_text=conversation_text,
            min_chars=min_chars,
        )

    def _strip_label(self, content: str) -> str:
        """去掉系统前缀标签，避免命中缓存时前缀重复嵌套。"""
        if "] " in content:
            return content.split("] ", 1)[1]
        return content

    def _get_required_min_chars(self, message_count: Optional[int] = None) -> int:
        """根据消息条数动态确定摘要最小字数要求。"""
        if message_count is not None and message_count <= 15:
            return 20
        return self.summary_min_chars

    def _is_too_short_summary(self, summary_text: str, min_chars: Optional[int] = None) -> bool:
        """仅判断摘要是否低于最低字数要求。"""
        required_min = self.summary_min_chars if min_chars is None else min_chars
        if not summary_text:
            return True
        return len(summary_text.strip()) < required_min

    def _sanitize_summary(self, text: str, fallback_text: str, min_chars: Optional[int] = None) -> str:
        """清洗摘要；若字数低于最低要求则回退截断摘要，避免污染数据库。"""
        cleaned = (text or "").strip()
        required_min = self.summary_min_chars if min_chars is None else min_chars
        if self._is_too_short_summary(cleaned, required_min):
            logger.warning(
                "摘要字数过短（actual=%s, min_required=%s），改用回退摘要",
                len(cleaned),
                required_min,
            )
            return self._fallback_summary(fallback_text)
        return cleaned

    async def _call_summary(
        self,
        system_prompt: str,
        user_prompt: str,
        fallback_text: str,
        min_chars: Optional[int] = None,
    ) -> str:
        identity_context = load_identity_context(include_schedule=True)
        if identity_context:
            system_prompt = f"{system_prompt}\n\n{identity_context}"

        if self._summarizer is None:
            try:
                from brain.llm import get_llm_client

                self._summarizer = get_llm_client(model_alias="summary")
            except Exception as e:
                logger.warning("summary 模型不可用，使用回退压缩: %s", e)
                self._summarizer = None

        if self._summarizer is None:
            return self._fallback_summary(fallback_text)

        for attempt in range(1, self.summary_max_retries + 1):
            try:
                summary = await self._summarizer.chat(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    history=[],
                )
                sanitized = self._sanitize_summary(summary, fallback_text, min_chars)
                if self._is_too_short_summary(sanitized, min_chars):
                    raise RuntimeError("summary 返回字数过短")
                return sanitized
            except Exception as e:
                if attempt >= self.summary_max_retries:
                    logger.error(
                        "调用 summary 模型失败（已重试 %s 次），使用回退: %s",
                        self.summary_max_retries,
                        e,
                    )
                    return self._fallback_summary(fallback_text)

                delay = self.summary_retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "调用 summary 模型失败，第 %s/%s 次重试，%.1fs 后重试: %s",
                    attempt,
                    self.summary_max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

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
                    json.dumps(seg.get("source_keys", [])),
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
