'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-15 22:28:26
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import json
import logging
import re
import sqlite3
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any, Tuple

from workspace_config import get_workspace_manager
from brain.prompts import render_template, append_custom_scope_block
from brain.prompts import load_identity_context
from memory.summary_quality import summarize_error_reason
from memory.summary_retry import call_with_retry
from memory.summary_chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_CHAR_CAP,
    MERGE_MODE_SECTIONS,
    chunked_summary,
    has_timeline,
)

logger = logging.getLogger(__name__)

# 与 message_history.DEFAULT_MEMORY_SCOPE_ID / conversation_identity 保持一致。
DEFAULT_MEMORY_SCOPE_ID = "relationship:owner:default"

# 送给模型的单段锚点长度：开头 + 结尾各这么多字。
# ⚠️ **不能整段送**。实测 12000 字整段必被输入侧拦截（`empty_choices`，
# prompt_tokens 13850 / completion_tokens 0），且是确定性的——叠多少重试都没用。
# 首尾各留一段，既保住"这段在讲什么"的锚点，又把密度压到能过。
SNIPPET_HEAD = 160

# 槽位标签的模板。按 **slot** 现拼，不落库——历史上是写库的，
# 命中缓存回填时就变成 `[最近段#1] [最近段#1] …` 层层嵌套。
_SLOT_LABEL = {
    "raw_recent_segment": "[最近段#{slot}]",
    "compressed_recent_segment": "[最近长段摘要#{slot}]",
    "compressed_single_segment": "[压缩段#{slot}]",
    "compressed_group_segments": "[合并摘要段7-10]",
}
# `[标签] 正文` 与 `[标签]\n正文` 两种写法都要能还原。
_LABEL_PREFIX = re.compile(r"^\[(?:最近段|最近长段摘要|压缩段|合并摘要段)[^\]]*\][ \t]*\n?")


def _load_source_keys(row: Optional[Dict]) -> List[str]:
    """从库行里取出 message_ids（JSON 数组），解析失败按空处理。"""
    if not row:
        return []
    try:
        keys = json.loads(row.get("message_ids") or "[]")
    except Exception:
        return []
    return [str(k) for k in keys] if isinstance(keys, list) else []


@dataclass
class _SegmentText:
    """一段对话展开后的结果。"""

    blocks: List[str]        # 按消息顺序，每条的 `role: 内容`
    message_count: int       # 消息条数（决定最小字数要求）
    latest_ts: Optional[float]  # 段内消息的 max(timestamp)，缓存键就靠它


def _unwrap_label(content: str) -> str:
    """去掉槽位标签，还原成纯摘要正文（读/回填都走这里，避免前缀嵌套）。"""
    text = (content or "").strip()
    m = _LABEL_PREFIX.match(text)
    return text[m.end():].strip() if m else text


def _decorate(slot: int, segment_type: str, content: str) -> str:
    """按 slot 拼上标签**不落库**，只在读的时候加。"""
    template = _SLOT_LABEL.get(segment_type)
    if not template:
        return content
    label = template.format(slot=slot)
    # 段号是拼出来的（`[压缩段#5]`）用换行接正文；合并段是固定文案，接空格。
    return f"{label}\n{content}" if "{slot}" in template else f"{label} {content}"


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
                memory_scope_id TEXT,
                place_scope_id TEXT,
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
        # idx_raw_messages_scope 在 _migrate_db 中、确保 memory_scope_id 列存在后再建。
        self._migrate_db(conn)
        conn.commit()
        conn.close()

    def _migrate_db(self, conn: sqlite3.Connection):
        """为旧镜像库补充作用域列并回填（非破坏性，自动检测）。"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(raw_messages)")
        cols = {row[1] for row in cursor.fetchall()}
        if "memory_scope_id" not in cols:
            logger.info("数据库迁移: 为 raw_messages 添加 memory_scope_id 列")
            cursor.execute("ALTER TABLE raw_messages ADD COLUMN memory_scope_id TEXT")
        if "place_scope_id" not in cols:
            cursor.execute("ALTER TABLE raw_messages ADD COLUMN place_scope_id TEXT")
        conn.commit()
        try:
            cursor.execute(
                """
                UPDATE raw_messages SET memory_scope_id = ?
                WHERE memory_scope_id IS NULL OR memory_scope_id = ''
                """,
                (DEFAULT_MEMORY_SCOPE_ID,),
            )
            cursor.execute(
                """
                UPDATE raw_messages SET place_scope_id = platform || ':' || chat_id
                WHERE place_scope_id IS NULL OR place_scope_id = ''
                """
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            logger.warning("回填 raw_messages 作用域列失败: %s", e)
        # 作用域列已就绪，创建索引。
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_raw_messages_scope
            ON raw_messages(memory_scope_id, timestamp DESC)
            """
        )
        conn.commit()

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
        memory_scope_id: Optional[str] = None,
        place_scope_id: Optional[str] = None,
    ) -> None:
        metadata_json = json.dumps(metadata) if metadata else None
        resolved_scope = memory_scope_id or DEFAULT_MEMORY_SCOPE_ID
        resolved_place = place_scope_id or f"{platform}:{chat_id}"
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO raw_messages (message_id, platform, chat_id, role, content_with_timestamp, raw_content, timestamp, metadata, memory_scope_id, place_scope_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                resolved_scope,
                resolved_place,
            ),
        )
        conn.commit()
        conn.close()

    def get_recent_messages(
        self,
        platform: str,
        chat_id: str,
        limit: int = 20,
        memory_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if memory_scope_id:
            cursor.execute(
                """
                SELECT * FROM raw_messages
                WHERE memory_scope_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (memory_scope_id, limit),
            )
        else:
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

    def clear_chat(self, platform: str, chat_id: str) -> None:
        """删除指定平台会话的所有镜像消息。"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM raw_messages WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
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
                memory_scope_id TEXT,
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
        self._migrate_db(conn)
        conn.commit()
        conn.close()

    def _migrate_db(self, conn: sqlite3.Connection):
        """为旧压缩库补充 memory_scope_id 列（非破坏性，自动检测）。"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(context_segments)")
        cols = {row[1] for row in cursor.fetchall()}
        if "memory_scope_id" not in cols:
            logger.info("数据库迁移: 为 context_segments 添加 memory_scope_id 列")
            cursor.execute("ALTER TABLE context_segments ADD COLUMN memory_scope_id TEXT")
            conn.commit()
            try:
                cursor.execute(
                    """
                    UPDATE context_segments SET memory_scope_id = ?
                    WHERE memory_scope_id IS NULL OR memory_scope_id = ''
                    """,
                    (DEFAULT_MEMORY_SCOPE_ID,),
                )
                conn.commit()
            except sqlite3.OperationalError as e:
                logger.warning("回填 context_segments 作用域列失败: %s", e)

    @staticmethod
    def _scope_partition(platform: str, chat_id: str, memory_scope_id: Optional[str]) -> tuple[str, str]:
        """共享模式下，压缩片段统一存放在规范作用域分区，避免被单平台 (platform, chat_id) 切分。"""
        if memory_scope_id:
            return ("__scope__", memory_scope_id)
        return (platform, chat_id)

    @staticmethod
    def _msg_scope_filter(platform: str, chat_id: str, memory_scope_id: Optional[str]) -> tuple[str, tuple]:
        """构建查询 messages/conversation_sessions 表的作用域过滤条件。"""
        if memory_scope_id:
            return "memory_scope_id = ?", (memory_scope_id,)
        return "platform = ? AND chat_id = ?", (platform, chat_id)

    async def refresh_context(self, platform: str, chat_id: str, memory_scope_id: Optional[str] = None) -> None:
        """按对话段落(session)重新生成压缩上下文。

        跨平台接力：传入 memory_scope_id 时，按共享作用域聚合所有平台的段落生成压缩，
        并存放在规范作用域分区，让任意平台都能读到同一份压缩上下文。
        """
        try:
            if not self.history_db_path:
                logger.warning("ContextCompressor 未配置 history_db_path，跳过段压缩")
                return

            segment_refs = self._get_recent_segment_refs(platform, chat_id, limit=10, memory_scope_id=memory_scope_id)
            if not segment_refs:
                self._mark_refresh_success(platform, chat_id)
                return

            existing = self._load_existing(platform, chat_id, memory_scope_id=memory_scope_id)
            slots: List[Dict] = []
            # _persist_segments 是整表覆盖：这里少一个槽，库里就少一个。
            # 所以总结不出来的槽必须先把旧内容**原样带过来**，否则等于把之前
            # 好不容易总结出来的记忆删掉（不总结远好过丢内容）。
            skipped: List[int] = []

            for idx, seg_ref in enumerate(segment_refs):
                slot = idx + 1
                if slot > 10:
                    break

                seg = self._build_segment_text(platform, chat_id, seg_ref, memory_scope_id=memory_scope_id)
                if not seg.blocks:
                    continue

                seg_role = "system"
                seg_ts = float(seg_ref.get("source_timestamp") or seg.latest_ts or datetime.now().timestamp())
                source_key = str(seg_ref.get("source_key", ""))

                if slot <= 3:
                    if len("\n".join(seg.blocks)) > self.long_message_threshold:
                        content = await self._summarize_single(
                            seg,
                            seg_role,
                            existing.get(slot),
                            source_key,
                            min_chars=self._get_required_min_chars(seg.message_count),
                        )
                        if content is None:
                            self._carry_over(existing, slot, slots, skipped)
                            continue
                        slots.append(
                            self._slot_row(slot, "compressed_recent_segment", content, source_key, seg_ts)
                        )
                    else:
                        # 最近段没过长就整段保留原文——这是**有意的**，不是降级。
                        slots.append(
                            self._slot_row(
                                slot, "raw_recent_segment", "\n".join(seg.blocks), source_key, seg_ts
                            )
                        )
                elif slot <= 6:
                    content = await self._summarize_single(
                        seg,
                        seg_role,
                        existing.get(slot),
                        source_key,
                        min_chars=self._get_required_min_chars(seg.message_count),
                    )
                    if content is None:
                        self._carry_over(existing, slot, slots, skipped)
                        continue
                    slots.append(
                        self._slot_row(slot, "compressed_single_segment", content, source_key, seg_ts)
                    )
                elif slot == 7:
                    # 第 7~10 段合并成一条。源 key 把各段的 key 拼在一起，
                    # 所以任意一段变了都会让这一槽的缓存失效。
                    group_segs: List[_SegmentText] = []
                    group_keys: List[str] = []
                    for r in segment_refs[6:10]:
                        g = self._build_segment_text(platform, chat_id, r, memory_scope_id=memory_scope_id)
                        if not g.blocks:
                            continue
                        group_segs.append(g)
                        group_keys.append(str(r.get("source_key", "")))
                    if not group_segs:
                        break
                    total_group_messages = sum(g.message_count for g in group_segs)
                    last_ts = max(
                        (float(r.get("source_timestamp") or 0) for r in segment_refs[6:10]),
                        default=seg_ts,
                    )
                    group_key = "|".join(group_keys)
                    content = await self._summarize_group(
                        group_segs,
                        existing.get(slot),
                        group_key,
                        min_chars=self._get_required_min_chars(total_group_messages),
                    )
                    if content is None:
                        self._carry_over(existing, slot, slots, skipped)
                        break
                    slots.append(
                        self._slot_row(slot, "compressed_group_segments", content, group_key, last_ts)
                    )
                    break

            if skipped:
                logger.warning(
                    "[%s/%s] 槽位 %s 本次总结未通过，保留原有内容（可能是空槽）："
                    "绝不写截断原文或报错文案当摘要",
                    platform, chat_id, skipped,
                )
            self._persist_segments(platform, chat_id, slots, memory_scope_id=memory_scope_id)
            self._mark_refresh_success(platform, chat_id)
        except Exception as e:
            self._enqueue_refresh_retry(platform, chat_id, e)
            raise

    @staticmethod
    def _slot_row(slot: int, segment_type: str, content: str, source_key: str, ts: float) -> Dict:
        """构造待写槽位。`content` 存**纯正文**，标签由 `_decorate` 在读时拼。"""
        return {
            "slot": slot,
            "segment_type": segment_type,
            "role": "system",
            "content": content,
            "source_keys": [source_key],
            "source_timestamp": ts,
        }

    @staticmethod
    def _carry_over(
        existing: Dict[int, Dict],
        slot: int,
        slots: List[Dict],
        skipped: List[int],
    ) -> None:
        """本次总结失败时，把该槽位的旧内容原样放回待写列表。

        没有旧内容（新槽）就什么都不加——留空好过写降级内容。
        """
        skipped.append(slot)
        row = existing.get(slot)
        if not row:
            return
        old_keys = _load_source_keys(row)
        # 旧库里存的是带标签的版本，先剥掉再当正文放回，保持"库里只有正文"。
        slots.append(
            ContextCompressor._slot_row(
                slot,
                row.get("segment_type") or "compressed_single_segment",
                _unwrap_label(row.get("content") or ""),
                old_keys[0] if old_keys else "",
                float(row.get("source_timestamp") or 0),
            )
        )

    def _enqueue_refresh_retry(self, platform: str, chat_id: str, error: Exception) -> None:
        if self.retry_callback:
            self.retry_callback("context_refresh", platform, chat_id, None, str(error))

    def _mark_refresh_success(self, platform: str, chat_id: str) -> None:
        if self.success_callback:
            self.success_callback("context_refresh", platform, chat_id, None)

    def _get_recent_segment_refs(self, platform: str, chat_id: str, limit: int = 10, memory_scope_id: Optional[str] = None) -> List[Dict]:
        """获取最近对话段（含当前活跃段）引用，按最新在前返回。

        传入 memory_scope_id 时按共享作用域聚合所有平台的段落。

        ⚠️ `source_key` 里必须带**段内消息的 max(timestamp)**（`stamp`）。
        它既是压缩槽位的缓存键，也是排序键——只按 `ended_at` 排的话，
        活跃段（`ended_at` 为空 / 取首条时间）会被排到最后，槽位顺序就乱了。
        """
        if not self.history_db_path:
            return []

        msg_where, msg_params = self._msg_scope_filter(platform, chat_id, memory_scope_id)

        conn = sqlite3.connect(str(self.history_db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        refs: List[Dict] = []

        # 活跃段（session_id IS NULL）：id 列表 + 最新时间，一次查询拿全。
        cursor.execute(
            f"""
            SELECT id, timestamp FROM messages
            WHERE {msg_where} AND session_id IS NULL
            ORDER BY timestamp ASC
            """,
            msg_params,
        )
        active_rows = cursor.fetchall()
        if active_rows:
            active_ids = [int(r["id"]) for r in active_rows]
            refs.append(
                {
                    "kind": "active",
                    "source_key": f"active:{','.join(map(str, active_ids))}",
                    "source_timestamp": float(active_rows[-1]["timestamp"]),
                    "stamp": float(active_rows[-1]["timestamp"]),
                }
            )

        cursor.execute(
            f"""
            SELECT id, ended_at, message_count FROM conversation_sessions
            WHERE {msg_where}
            ORDER BY ended_at DESC
            LIMIT ?
            """,
            msg_params + (limit,),
        )
        session_rows = cursor.fetchall()
        for row in session_rows:
            session_id = int(row["id"])
            # 用段内真实的 max(timestamp) 而不是 ended_at：扫描压缩时
            # ended_at 可能还没回填，而且它和消息时间的口径未必一致。
            cursor.execute(
                "SELECT MAX(timestamp) AS stamp FROM messages WHERE session_id = ?",
                (session_id,),
            )
            stamp_row = cursor.fetchone()
            stamp = float(stamp_row["stamp"]) if stamp_row and stamp_row["stamp"] is not None else float(row["ended_at"] or 0)
            refs.append(
                {
                    "kind": "closed",
                    "session_id": session_id,
                    "source_key": f"session:{session_id}:{int(row['message_count'] or 0)}:{stamp:.3f}",
                    "source_timestamp": float(row["ended_at"] or 0),
                    "stamp": stamp,
                }
            )

        conn.close()
        refs.sort(key=lambda x: float(x.get("stamp", x.get("source_timestamp", 0))), reverse=True)
        return refs[:limit]

    def _build_segment_text(self, platform: str, chat_id: str, seg_ref: Dict, memory_scope_id: Optional[str] = None) -> _SegmentText:
        """将一个段落引用展开为逐条消息（`role: 内容`）与段内最新时间。"""
        empty = _SegmentText(blocks=[], message_count=0, latest_ts=None)
        if not self.history_db_path:
            return empty

        msg_where, msg_params = self._msg_scope_filter(platform, chat_id, memory_scope_id)

        conn = sqlite3.connect(str(self.history_db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        kind = seg_ref.get("kind")
        if kind == "active":
            cursor.execute(
                f"""
                SELECT role, content, timestamp FROM messages
                WHERE {msg_where} AND session_id IS NULL
                ORDER BY timestamp ASC
                """,
                msg_params,
            )
            rows = cursor.fetchall()
        else:
            session_id = seg_ref.get("session_id")
            if session_id is None:
                conn.close()
                return empty
            # 已关闭段落按 session_id（全局唯一主键）取，跨平台合并的段落也能完整取到。
            cursor.execute(
                """
                SELECT role, content, timestamp FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC
                """,
                (int(session_id),),
            )
            rows = cursor.fetchall()
        conn.close()

        if not rows:
            return empty
        stamps = [float(r["timestamp"]) for r in rows if r["timestamp"] is not None]
        return _SegmentText(
            blocks=[f"{r['role']}: {r['content']}" for r in rows],
            message_count=len(rows),
            latest_ts=max(stamps) if stamps else None,
        )

    def _load_existing(self, platform: str, chat_id: str, memory_scope_id: Optional[str] = None) -> Dict[int, Dict]:
        part_platform, part_chat = self._scope_partition(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM context_segments
            WHERE platform = ? AND chat_id = ?
            """,
            (part_platform, part_chat),
        )
        rows = {int(row["slot"]): dict(row) for row in cursor.fetchall()}
        conn.close()
        return rows

    async def _summarize_single(
        self,
        seg: _SegmentText,
        role: str,
        existing_row: Optional[Dict],
        source_key: str,
        min_chars: Optional[int] = None,
    ) -> Optional[str]:
        """返回 None 表示**这段总结不出来**，调用方必须放弃该槽位。"""
        return await self._call_summary(
            segs=[seg],
            existing_row=existing_row,
            source_key=source_key,
            min_chars=min_chars,
        )

    async def _summarize_group(
        self,
        segs: List[_SegmentText],
        existing_row: Optional[Dict],
        source_key: str,
        min_chars: Optional[int] = None,
    ) -> Optional[str]:
        """合并第 7~10 段。返回 None 表示总结不出来，调用方必须放弃该槽位。"""
        return await self._call_summary(
            segs=segs,
            existing_row=existing_row,
            source_key=source_key,
            min_chars=min_chars,
        )

    @staticmethod
    def _build_snippet(seg: _SegmentText) -> str:
        """把一段展开结果压成首尾各 SNIPPET_HEAD 字的锚点。

        ⚠️ 首尾放在**同一个字符串**里：分块层是按 `texts` 的条数劈半的，
        拆成两条会让"一次追问被切成两段"而各自都读不全。
        """
        joined = "\n".join(seg.blocks)
        if len(joined) <= SNIPPET_HEAD * 2:
            return joined
        return f"{joined[:SNIPPET_HEAD]}\n…（中间省略）…\n{joined[-SNIPPET_HEAD:]}"

    @staticmethod
    def _matching_existing(
        existing_row: Optional[Dict],
        source_key: str,
    ) -> Optional[str]:
        """已有行的来源与本次一致则返回复用的摘要正文，否则 None。

        判据只有一个：`message_ids` 是否等于本次的 `source_key`。
        段内消息的 max(timestamp) 已经编进 source_key，所以"会话内容没动"
        就是命中，"多了一条 / 改过一条"就是未命中——不依赖任何别处再传一遍。
        """
        if not existing_row:
            return None
        if _load_source_keys(existing_row) != [source_key]:
            return None
        content = (existing_row.get("content") or "").strip()
        return content or None

    def _get_required_min_chars(self, message_count: Optional[int] = None) -> int:
        """根据消息条数动态确定摘要最小字数要求。"""
        if message_count is not None and message_count <= 15:
            return 20
        return self.summary_min_chars

    def _looks_like_summary(self, text: str) -> bool:
        """结构判据：六字段摘要必须带【时间线】。

        ⚠️ 不能只看字符数。provider 的失败说明、截断原文、模型"入戏"写的散文
        **都长过任何合理的字数阈值**，长度挡不住"内容不是摘要"。
        【时间线】是模型真做了归纳才会出现的标记，拿它当判据才拦得住。
        """
        return has_timeline(text)

    async def _call_summary(
        self,
        segs: List[_SegmentText],
        existing_row: Optional[Dict],
        source_key: str,
        min_chars: Optional[int] = None,
    ) -> Optional[str]:
        """生成一段槽位摘要。返回 None = **这段救不回来**，调用方必须放弃该槽位。

        ⚠️ 这里**没有**"回退摘要"。历史实现会把发不出去的内容截断到 200 字当摘要写库，
        那不是摘要而是**静默降级**：模型答不上来 / 被安全策略拦下时，槽位里会出现
        一段半截的原文，读起来像记忆、实际把语义砍掉一半。项目在分块那层已经定过调子
        ——静默损坏比彻底失败更危险——所以这里改为：整段送 → 被拦就分块 → 还不行就返回
        None，由调用方保留旧槽位（不写降级内容）。
        """
        # 来源没变就直接复用已有摘要，不重复调模型。
        # 段内消息的 max(timestamp) 已经编进 source_key，所以"内容动过"必然未命中。
        cached = self._matching_existing(existing_row, source_key)

        if self._summarizer is None:
            try:
                from brain.llm import get_llm_client

                self._summarizer = get_llm_client(model_alias="summary")
            except Exception as e:
                logger.warning("summary 模型不可用，放弃本段压缩: %s", e)
                self._summarizer = None

        if self._summarizer is None:
            return None

        # 缓存命中要放在"模型不可用"之后：模型临时挂掉时也该继续用旧摘要，
        # 而不是把一个本来好好的槽位判成失败。
        if cached is not None:
            return cached

        system_prompt = append_custom_scope_block(
            render_template("compression.jinja", "compress_system"), "summary"
        )
        identity_context = load_identity_context(include_schedule=True)
        if identity_context:
            system_prompt = f"{system_prompt}\n\n{identity_context}"

        async def _one_call(prompt: str) -> str:
            # ⚠️ 精确重试放在**分块之前**：`content_filter` 是随机的，同一段内容
            # 这次被拦下次能过。只试一次的话，二分树会被随机失败引着把
            # 本来能过的消息一路劈到单条、最后误判"无解"而整条放弃。
            async def _one() -> Tuple[Optional[Any], str]:
                out = await self._summarizer.chat(
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    history=[],
                )
                return self._summarizer, out

            _, out = await call_with_retry(
                _one,
                attempts=self.summary_max_retries,
                base_delay=self.summary_retry_base_delay,
                label="上下文段摘要",
            )
            reason = summarize_error_reason(self._summarizer, out)
            if reason:
                logger.warning("上下文段摘要单次失败（原因=%s）", reason)
                return ""
            return str(out or "").strip()

        def _render(conversation_text: str) -> str:
            return render_template(
                "compression.jinja", "compress_user", conversation_text=conversation_text
            )

        # 降级阶梯：整段原文 → 首尾锚点 → 分块。
        #
        # ⚠️ 顺序不能反。整段原文信息最全，而且**大多数段是过得去的**——
        # 实测同一批数据里，只有最长的一段（2536 字符）会被拦，其余全过。
        # 一上来就送锚点等于白丢一半细节（实测同样几段：631→350、397→216）。
        # 锚点只在整段被拦时才用，那时"少记一点"好过"什么都不记"。
        full_text = "\n\n".join("\n".join(s.blocks) for s in segs)
        anchors = [self._build_snippet(s) for s in segs]
        anchors_text = "\n\n".join(anchors)

        trials = [full_text] if full_text != anchors_text else []
        trials.append(anchors_text)

        for idx, trial in enumerate(trials):
            summary = await _one_call(_render(trial))
            if summary and self._looks_like_summary(summary):
                if idx:
                    logger.warning("上下文段摘要整段被拦，已用首尾锚点降级生成")
                return summary
            if summary:
                logger.warning("上下文段摘要结构不合规（无【时间线】），继续降级")

        logger.warning(
            "上下文段摘要锚点仍不可用，转入分块降级（%s 段，每块 %s 段 / 单段上限 %s 字符）",
            len(segs), DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_CHAR_CAP,
        )
        fallback = await chunked_summary(
            anchors,
            _one_call,
            _render,
            chunk_size=DEFAULT_CHUNK_SIZE,
            char_cap=DEFAULT_CHUNK_CHAR_CAP,
            merge_mode=MERGE_MODE_SECTIONS,
            enforce_timeline=True,
        )
        if not fallback:
            logger.error("上下文段摘要分块后仍失败，放弃该槽位（保留旧内容，不写降级摘要）")
            return None

        # 分块层已按行分类合并（同号槽位的字段只留一份、入戏散文被丢弃），
        # 这里只再兜一道长度：太短说明几乎没归纳出东西。
        if min_chars is not None and len(fallback.strip()) < min_chars:
            logger.error(
                "上下文段摘要分块结果过短（%s < %s），放弃该槽位",
                len(fallback.strip()), min_chars,
            )
            return None
        return fallback

    def _persist_segments(self, platform: str, chat_id: str, slots: List[Dict], memory_scope_id: Optional[str] = None) -> None:
        part_platform, part_chat = self._scope_partition(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        now = datetime.now().timestamp()
        for seg in slots:
            cursor.execute(
                """
                INSERT INTO context_segments (platform, chat_id, slot, segment_type, role, content, message_ids, source_timestamp, updated_at, memory_scope_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, chat_id, slot) DO UPDATE SET
                    segment_type=excluded.segment_type,
                    role=excluded.role,
                    content=excluded.content,
                    message_ids=excluded.message_ids,
                    source_timestamp=excluded.source_timestamp,
                    updated_at=excluded.updated_at,
                    memory_scope_id=excluded.memory_scope_id
                """,
                (
                    part_platform,
                    part_chat,
                    seg["slot"],
                    seg["segment_type"],
                    seg["role"],
                    seg["content"],
                    json.dumps(seg.get("source_keys", [])),
                    seg.get("source_timestamp", now),
                    now,
                    memory_scope_id,
                ),
            )
        conn.commit()
        conn.close()

    def get_context_messages(self, platform: str, chat_id: str, memory_scope_id: Optional[str] = None) -> List[Dict]:
        part_platform, part_chat = self._scope_partition(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM context_segments
            WHERE platform = ? AND chat_id = ?
            ORDER BY slot DESC
            """,
            (part_platform, part_chat),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        messages = []
        for row in rows:
            slot = int(row.get("slot") or 0)
            segment_type = row.get("segment_type") or ""
            messages.append(
                {
                    "role": row.get("role", "system"),
                    # 库里只存正文，标签在这里按 slot 现拼（见 _decorate）。
                    "content": _decorate(slot, segment_type, row.get("content", "")),
                    "timestamp": row.get("source_timestamp", 0),
                    "segment_slot": slot,
                    "segment_type": segment_type,
                }
            )
        return messages

    def clear_chat(self, platform: str, chat_id: str, memory_scope_id: Optional[str] = None) -> None:
        """删除指定会话的压缩上下文片段。传入 memory_scope_id 时清理规范作用域分区。"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        # 物理地点分区
        cursor.execute(
            "DELETE FROM context_segments WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
        )
        # 规范作用域分区（共享模式写入处）
        if memory_scope_id:
            cursor.execute(
                "DELETE FROM context_segments WHERE platform = ? AND chat_id = ?",
                ("__scope__", memory_scope_id),
            )
        conn.commit()
        conn.close()


__all__ = [
    "MessageLog",
    "ContextCompressor",
    "get_default_message_log_db",
    "get_default_context_db",
]
