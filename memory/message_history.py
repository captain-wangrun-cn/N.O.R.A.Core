import sqlite3
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path
import logging
from contextlib import suppress

from workspace_config import get_workspace_manager
from brain.prompts import render_template
from memory.context_store import ContextCompressor, MessageLog

logger = logging.getLogger(__name__)


def get_default_message_history_db() -> Path:
    """返回 workspace 下的默认聊天记录数据库路径，并确保目录存在。"""
    workspace = get_workspace_manager()
    path = Path(workspace.data_dir) / "memory" / "message_history.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class MessageHistory:
    """
    消息历史管理器
    
    存储策略:
    - Level 1: 原始消息 (最近 N 条) - 完整保留
    - Level 2: 压缩总结 (N ~ M 条) - 分段总结
    - Level 3: 归档总结 (M+ 条) - 全局总结
    - Pinned: 永久标记消息 - 跨会话保留
    """
    
    def __init__(
        self,
        db_path: Optional[str] = None,
    raw_window: int = 80,      # 原始消息窗口大小（放宽，减少过早截断）
    compress_window: int = 120, # 开始压缩的阈值（按消息数量，推迟压缩）
    compress_ratio: int = 12,   # 压缩比例 (12:1)，兼顾上下文保留
    archive_threshold: int = 600, # 归档阈值
        timezone: str = "Asia/Shanghai",  # 时间戳显示时区
        timestamp_format: str = "%Y-%m-%d %H:%M:%S %A",  # 时间戳格式
        mirror_db_path: Optional[str] = None,  # 原文镜像库（独立）
        context_db_path: Optional[str] = None,  # 压缩上下文库（独立）
    long_message_threshold: int = 1600,  # 判定“过长”后提前压缩的阈值，放宽减少过早截断
    retry_base_delay_seconds: float = 60.0,
    retry_max_attempts: int = 8,
    retry_scan_interval_seconds: float = 300.0,
    ):
        self.db_path = Path(db_path) if db_path else get_default_message_history_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.raw_window = raw_window
        self.compress_window = compress_window
        self.compress_ratio = compress_ratio
        self.archive_threshold = archive_threshold
        
        # 设置时区
        try:
            self.timezone = ZoneInfo(timezone)
        except Exception as e:
            logger.warning(f"无效的时区 '{timezone}'，回退到 UTC: {e}")
            self.timezone = ZoneInfo("UTC")

        self.timestamp_format = timestamp_format or "%Y-%m-%d %H:%M:%S %A"
        
        self._init_db()
        self._summarizer = None  # 延迟加载
        self._last_compress_log_count: Dict[Tuple[str, str], int] = {}
        self.retry_base_delay_seconds = max(5.0, float(retry_base_delay_seconds))
        self.retry_max_attempts = max(1, int(retry_max_attempts))
        self.retry_scan_interval_seconds = max(10.0, float(retry_scan_interval_seconds))
        self._retry_worker_task: Optional[asyncio.Task] = None
        self._retry_worker_started = False

        # 独立的原文镜像库 & 压缩上下文库
        self.message_log = MessageLog(db_path=mirror_db_path)
        self.context_compressor = ContextCompressor(
            message_log=self.message_log,
            db_path=context_db_path,
            long_message_threshold=long_message_threshold,
            history_db_path=str(self.db_path),
            retry_callback=self._enqueue_retry,
            success_callback=self._mark_retry_success,
        )

        self.start_retry_worker()
    
    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # 原始消息表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT,
                role TEXT NOT NULL,  -- 'user', 'assistant', 'system'
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                metadata TEXT,  -- JSON格式的额外信息
                is_pinned INTEGER DEFAULT 0,  -- 是否永久标记
                is_archived INTEGER DEFAULT 0,  -- 是否已归档
                session_id INTEGER,  -- 所属对话段落ID (NULL=尚未分配)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 对话段落表 (Conversation Sessions)
        # 当 AI 从 ONLINE 进入 SEMI_ONLINE 时，将这次在线状态的所有对话封装为一个段落
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                started_at REAL NOT NULL,      -- 本段对话起始时间戳
                ended_at REAL NOT NULL,         -- 本段对话结束时间戳
                message_count INTEGER DEFAULT 0,-- 本段消息总数
                summary TEXT,                   -- 本段对话摘要 (异步生成)
                trigger_type TEXT DEFAULT 'user', -- 触发来源: 'user'=用户发起, 'proactive'=AI主动, 'alarm'=闹钟
                metadata TEXT,                  -- JSON格式额外信息
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 压缩总结表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                level INTEGER NOT NULL,  -- 1=一级总结, 2=二级总结, 3=归档总结
                start_message_id INTEGER,  -- 总结的起始消息ID
                end_message_id INTEGER,    -- 总结的结束消息ID
                summary_text TEXT NOT NULL,
                message_count INTEGER,     -- 总结了多少条消息
                timestamp REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (start_message_id) REFERENCES messages(id),
                FOREIGN KEY (end_message_id) REFERENCES messages(id)
            )
        """)
        
        # 创建索引加速查询
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_chat 
            ON messages(platform, chat_id, timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_pinned 
            ON messages(platform, chat_id, is_pinned)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session 
            ON messages(platform, chat_id, session_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_summaries_chat 
            ON summaries(platform, chat_id, level, timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_chat 
            ON conversation_sessions(platform, chat_id, started_at)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS compression_retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                payload TEXT,
                attempts INTEGER DEFAULT 0,
                next_retry_at REAL NOT NULL,
                last_error TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, chat_id, task_type, payload)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_retry_queue_due
            ON compression_retry_queue(status, next_retry_at)
        """)
        
        # --- 数据库迁移：为旧数据库添加新字段 ---
        self._migrate_db(conn)
        
        conn.commit()
        conn.close()
        logger.info(f"消息历史数据库初始化完成: {self.db_path}")

    def _migrate_db(self, conn: sqlite3.Connection):
        """执行数据库迁移，安全地添加新列到已有表。"""
        cursor = conn.cursor()
        
        # 检查 messages 表是否有 session_id 列
        cursor.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in cursor.fetchall()}
        
        if "session_id" not in columns:
            logger.info("数据库迁移: 为 messages 表添加 session_id 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER")
            conn.commit()
    
    def add_message(
        self,
        platform: str,
        chat_id: str,
        role: str,
        content: str,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        is_pinned: bool = False
    ) -> int:
        """
        添加一条消息
        
    自动插入时间戳（每条消息都会插入）:
    - 时间戳格式: [YYYY-MM-DD HH:MM]
        
        :return: 消息ID
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        timestamp = datetime.now().timestamp()
        
        # 每条消息都插入时间戳
        should_add_timestamp = True
        
        # 如果需要，在内容前添加时间戳
        original_content = content
        if should_add_timestamp:
            # 使用配置的时区格式化时间
            dt = datetime.fromtimestamp(timestamp, tz=self.timezone)
            time_str = dt.strftime(self.timestamp_format)
            content = f"[{time_str}] {content}"
            # 在元数据中标记此消息包含时间戳
            if metadata is None:
                metadata = {}
            metadata["has_timestamp"] = True
            logger.debug(f"[{platform}/{chat_id}] 插入时间戳: {time_str} ({self.timezone})")
        
        metadata_json = json.dumps(metadata) if metadata else None
        
        cursor.execute("""
            INSERT INTO messages (platform, chat_id, user_id, role, content, timestamp, metadata, is_pinned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (platform, chat_id, user_id, role, content, timestamp, metadata_json, int(is_pinned)))

        message_id = int(cursor.lastrowid or 0)
        conn.commit()
        conn.close()

        # 镜像写入独立原文库
        try:
            self.message_log.add_message(
                message_id=message_id,
                platform=platform,
                chat_id=chat_id,
                role=role,
                content_with_timestamp=content,
                raw_content=original_content,
                timestamp=timestamp,
                metadata=metadata,
            )
        except Exception as e:
            logger.warning(f"[{platform}/{chat_id}] 写入消息镜像失败: {e}")

        # 触发压缩上下文刷新（独立上下文数据库）
        self._schedule_context_refresh(platform, chat_id)

        logger.debug(f"[{platform}/{chat_id}] 添加消息 #{message_id}: {role}")

        # 异步触发压缩检查（不阻塞）
        self._launch_background(self._check_and_compress(platform, chat_id))

        return message_id

    def delete_last_assistant_message(
        self,
        platform: str,
        chat_id: str,
        source: Optional[str] = None,
        nth: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        删除最近一条 assistant 消息（未归档且未置顶）。

        如果提供了 source，则仅匹配 metadata.source == source，否则删除最新的一条 assistant 消息。
        nth=1 表示最近一条，nth=2 表示倒数第二条，以此类推。
        返回包含 message_id 与 metadata 的字典；若未找到则返回 None。
        """
        nth = max(1, int(nth or 1))
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, metadata FROM messages
                WHERE platform = ? AND chat_id = ? AND role = 'assistant'
                      AND is_archived = 0 AND is_pinned = 0
                ORDER BY id DESC
                LIMIT 50
                """,
                (platform, chat_id),
            )
            rows = cursor.fetchall()
            target_id: Optional[int] = None
            target_md: Dict[str, Any] = {}
            matched_count = 0
            for row in rows:
                md_raw = row["metadata"]
                md = json.loads(md_raw) if md_raw else {}
                if source is None or md.get("source") == source:
                    matched_count += 1
                    if matched_count == nth:
                        target_id = int(row["id"])
                        target_md = md
                        break
            if target_id is None:
                return None
            cursor.execute("DELETE FROM messages WHERE id = ?", (target_id,))
            conn.commit()
            # 同步删除镜像库
            try:
                self.message_log.delete_by_message_id(target_id)
            except Exception:
                logger.warning("删除镜像库记录失败 (id=%s)", target_id, exc_info=True)
            logger.info(f"[{platform}/{chat_id}] 撤销 assistant 消息 #{target_id} (source={source})")
            return {"message_id": target_id, "metadata": target_md}
        except Exception:
            logger.error(f"[{platform}/{chat_id}] 撤销 assistant 消息失败", exc_info=True)
            conn.rollback()
            return None
        finally:
            conn.close()

    def _schedule_context_refresh(self, platform: str, chat_id: str):
        """异步刷新滑动压缩上下文，失败时记录日志并不中断主流程。"""
        if not self.context_compressor:
            return
        self._launch_background(self.context_compressor.refresh_context(platform, chat_id))

    def _launch_background(self, coro):
        """安全地启动后台任务，如无事件循环则直接运行。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # 无运行中的事件循环时，阻塞运行一次
            try:
                asyncio.run(coro)
            except Exception as e:  # pragma: no cover
                logger.error(f"后台任务执行失败: {e}")

    def start_retry_worker(self):
        """启动压缩失败补偿 worker；若当前无事件循环则在首次可用时再启动。"""
        if self._retry_worker_started:
            return
        self._retry_worker_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("当前无事件循环，压缩重试 worker 将在后续异步调用时启动")
            self._retry_worker_started = False
            return
        self._retry_worker_task = loop.create_task(self._retry_worker_loop())

    async def stop_retry_worker(self):
        """停止后台重试 worker。"""
        task = self._retry_worker_task
        self._retry_worker_task = None
        self._retry_worker_started = False
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _retry_worker_loop(self):
        """周期性扫描并执行到期的压缩重试任务。"""
        logger.info("压缩重试 worker 已启动")
        try:
            while True:
                try:
                    await self.process_pending_retries()
                except Exception:
                    logger.error("处理压缩重试任务失败", exc_info=True)
                await asyncio.sleep(self.retry_scan_interval_seconds)
        except asyncio.CancelledError:
            logger.debug("压缩重试 worker 已停止")
            raise

    def _serialize_retry_payload(self, payload: Optional[Dict[str, Any]]) -> str:
        normalized = payload or {}
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    def _deserialize_retry_payload(self, payload: Optional[str]) -> Dict[str, Any]:
        if not payload:
            return {}
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _compute_next_retry_at(self, attempts: int) -> float:
        delay = self.retry_base_delay_seconds * (2 ** max(0, attempts - 1))
        capped_delay = min(delay, 6 * 60 * 60)
        return datetime.now().timestamp() + capped_delay

    def _enqueue_retry(
        self,
        task_type: str,
        platform: str,
        chat_id: str,
        payload: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        payload_json = self._serialize_retry_payload(payload)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, attempts FROM compression_retry_queue
                WHERE platform = ? AND chat_id = ? AND task_type = ? AND payload = ?
                """,
                (platform, chat_id, task_type, payload_json),
            )
            row = cursor.fetchone()
            now_ts = datetime.now().timestamp()
            if row:
                retry_id, attempts = int(row[0]), int(row[1] or 0) + 1
                status = "dead" if attempts >= self.retry_max_attempts else "pending"
                next_retry_at = self._compute_next_retry_at(attempts)
                cursor.execute(
                    """
                    UPDATE compression_retry_queue
                    SET attempts = ?, next_retry_at = ?, last_error = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (attempts, next_retry_at, error, status, retry_id),
                )
            else:
                attempts = 1
                status = "dead" if attempts >= self.retry_max_attempts else "pending"
                next_retry_at = now_ts if status == "dead" else self._compute_next_retry_at(attempts)
                cursor.execute(
                    """
                    INSERT INTO compression_retry_queue
                        (platform, chat_id, task_type, payload, attempts, next_retry_at, last_error, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (platform, chat_id, task_type, payload_json, attempts, next_retry_at, error, status),
                )
            conn.commit()
        finally:
            conn.close()

    def _mark_retry_success(
        self,
        task_type: str,
        platform: str,
        chat_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload_json = self._serialize_retry_payload(payload)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                DELETE FROM compression_retry_queue
                WHERE platform = ? AND chat_id = ? AND task_type = ? AND payload = ?
                """,
                (platform, chat_id, task_type, payload_json),
            )
            conn.commit()
        finally:
            conn.close()

    def get_retry_queue_status(self) -> List[Dict[str, Any]]:
        """返回当前压缩重试队列状态，便于调试或测试。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM compression_retry_queue
            ORDER BY next_retry_at ASC, id ASC
            """
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    async def process_pending_retries(self) -> int:
        """执行所有已到期的重试任务，返回本轮处理数量。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM compression_retry_queue
            WHERE status = 'pending' AND next_retry_at <= ?
            ORDER BY next_retry_at ASC, id ASC
            """,
            (datetime.now().timestamp(),),
        )
        due_rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        processed = 0
        for row in due_rows:
            payload = self._deserialize_retry_payload(row.get("payload"))
            await self._execute_retry_task(
                task_type=row["task_type"],
                platform=row["platform"],
                chat_id=row["chat_id"],
                payload=payload,
            )
            processed += 1
        return processed

    async def _execute_retry_task(
        self,
        task_type: str,
        platform: str,
        chat_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = payload or {}
        if task_type == "compress":
            await self._perform_compression(platform, chat_id)
        elif task_type == "archive":
            await self._create_archive_summary(platform, chat_id)
        elif task_type == "session_summary":
            session_id = int(payload.get("session_id", 0) or 0)
            if session_id > 0:
                await self._generate_session_summary(platform, chat_id, session_id)
        elif task_type == "context_refresh":
            if self.context_compressor:
                await self.context_compressor.refresh_context(platform, chat_id)
        else:
            logger.warning("未知压缩重试任务类型: %s", task_type)
    
    def get_context_messages(
        self,
        platform: str,
        chat_id: str,
        limit: Optional[int] = None,
        include_summaries: bool = True
    ) -> List[Dict]:
        """
        获取对话上下文消息
        
        返回格式:
        [
            {"role": "system", "content": "[归档总结] ..."},
            {"role": "system", "content": "[压缩总结] ..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."},
            ...
        ]
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        messages = []
        
        # 1. 获取永久标记的消息
        cursor.execute("""
            SELECT * FROM messages
            WHERE platform = ? AND chat_id = ? AND is_pinned = 1
            ORDER BY timestamp ASC
        """, (platform, chat_id))
        
        pinned_messages = [dict(row) for row in cursor.fetchall()]
        if pinned_messages:
            for msg in pinned_messages:
                messages.append({
                    "role": msg["role"],
                    "content": f"[📌 重要] {msg['content']}",
                    "timestamp": msg["timestamp"]
                })
        
        # 2. 获取归档总结 (Level 3)
        if include_summaries:
            cursor.execute("""
                SELECT summary_text, message_count FROM summaries
                WHERE platform = ? AND chat_id = ? AND level = 3
                ORDER BY timestamp DESC
                LIMIT 1
            """, (platform, chat_id))
            
            archive_summary = cursor.fetchone()
            if archive_summary:
                messages.append({
                    "role": "system",
                    "content": f"[📚 早期对话总结，共{archive_summary['message_count']}条] {archive_summary['summary_text']}",
                    "timestamp": 0
                })

        # 优先使用独立上下文数据库的滑动压缩结果
        if include_summaries and self.context_compressor:
            compressed_segments = self.context_compressor.get_context_messages(platform, chat_id)
            if compressed_segments:
                messages.extend(compressed_segments)
                logger.debug(f"[{platform}/{chat_id}] 使用上下文压缩库: {len(compressed_segments)} 段")
        
        # 3. 获取压缩总结 (Level 1-2)
        if include_summaries:
            cursor.execute("""
                SELECT summary_text, level, message_count, timestamp FROM summaries
                WHERE platform = ? AND chat_id = ? AND level < 3
                ORDER BY timestamp ASC
            """, (platform, chat_id))
            
            for row in cursor.fetchall():
                messages.append({
                    "role": "system",
                    "content": f"[💬 对话摘要，共{row['message_count']}条] {row['summary_text']}",
                    "timestamp": row["timestamp"]
                })
        
        # 4. 获取原始消息（最近的）
        actual_limit = limit or self.raw_window
        cursor.execute("""
            SELECT * FROM messages
            WHERE platform = ? AND chat_id = ? AND is_archived = 0 AND is_pinned = 0
            ORDER BY timestamp DESC
            LIMIT ?
        """, (platform, chat_id, actual_limit))
        
        raw_messages = [dict(row) for row in cursor.fetchall()]
        raw_messages.reverse()  # 按时间正序
        
        # 在不同对话段落之间插入分隔标记
        prev_session_id = -1  # 用 -1 表示"尚未见到任何 session"
        for msg in raw_messages:
            current_session_id = msg.get("session_id")  # 可能为 None（当前活跃对话）
            
            # 当 session_id 发生变化时，插入段落分隔标记
            if prev_session_id != -1 and current_session_id != prev_session_id:
                # 查找对应 session 的摘要
                session_summary = ""
                if prev_session_id is not None:
                    cursor.execute("""
                        SELECT summary FROM conversation_sessions WHERE id = ?
                    """, (prev_session_id,))
                    row = cursor.fetchone()
                    if row and row[0]:
                        session_summary = f" 上段对话摘要: {row[0]}"
                
                messages.append({
                    "role": "system",
                    "content": f"[📋 对话段落分隔 — 以上为之前的对话段落{session_summary}，以下为新的对话段落]",
                    "timestamp": msg["timestamp"] - 0.001,  # 略早于新段落的第一条消息
                })
            
            prev_session_id = current_session_id
            md_raw = msg.get("metadata")
            try:
                md = json.loads(md_raw) if md_raw else {}
            except Exception:
                md = {}

            platform_ids = md.get("platform_message_ids") or md.get("platform_message_id") or []
            if isinstance(platform_ids, str):
                platform_ids = [platform_ids]
            platform_ids = [str(pid) for pid in platform_ids if pid is not None]

            messages.append({
                "role": msg["role"],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
                "message_id": msg["id"],
                "metadata": md,
                "platform_message_ids": platform_ids,
            })
        
        conn.close()
        
        # 按时间排序（归档总结在最前，然后是压缩总结，最后是原始消息）
        messages.sort(key=lambda x: x.get("timestamp", 0))
        
        logger.info(f"[{platform}/{chat_id}] 获取上下文: {len(messages)} 条消息")
        return messages

    # ------------------------------------------------------------------
    # 日志查询辅助
    # ------------------------------------------------------------------
    def get_messages_between(
        self,
        platform: str,
        chat_id: str,
        start_ts: float,
        end_ts: float,
    ) -> List[Dict[str, Any]]:
        """获取指定时间戳区间内的原始消息（来自镜像库）。"""
        if not self.message_log:
            return []

        conn = sqlite3.connect(str(self.message_log.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT role, raw_content, timestamp, metadata
            FROM raw_messages
            WHERE platform = ? AND chat_id = ? AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp ASC
            """,
            (platform, chat_id, start_ts, end_ts),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows
    
    async def _check_and_compress(self, platform: str, chat_id: str):
        """检查是否需要压缩，并执行压缩"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            # 统计未归档的消息数量
            cursor.execute("""
                SELECT COUNT(*) as count FROM messages
                WHERE platform = ? AND chat_id = ? AND is_archived = 0 AND is_pinned = 0
            """, (platform, chat_id))
            
            count = cursor.fetchone()[0]
            conn.close()
            
            if count > self.compress_window:
                key = (platform, chat_id)
                last_count = self._last_compress_log_count.get(key, 0)
                # 仅首次超过阈值，或每新增50条再提示一次，避免每条消息刷屏
                if last_count == 0 or count - last_count >= 50:
                    logger.info(f"[{platform}/{chat_id}] 消息数 {count} 超过阈值 {self.compress_window}，开始压缩")
                    self._last_compress_log_count[key] = count
                await self._perform_compression(platform, chat_id)
            
            if count > self.archive_threshold:
                logger.info(f"[{platform}/{chat_id}] 消息数 {count} 超过归档阈值 {self.archive_threshold}，创建归档总结")
                await self._create_archive_summary(platform, chat_id)
        
        except Exception as e:
            logger.error(f"压缩检查失败: {e}", exc_info=True)
    
    async def _perform_compression(self, platform: str, chat_id: str):
        """执行消息压缩"""
        self.start_retry_worker()
        if self._summarizer is None:
            from brain.llm import get_llm_client
            self._summarizer = get_llm_client(model_alias="summary")
        
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 获取最早的 N 条未归档消息
        cursor.execute("""
            SELECT * FROM messages
            WHERE platform = ? AND chat_id = ? AND is_archived = 0 AND is_pinned = 0
            ORDER BY timestamp ASC
            LIMIT ?
        """, (platform, chat_id, self.compress_ratio))
        
        messages_to_compress = [dict(row) for row in cursor.fetchall()]
        
        if len(messages_to_compress) < self.compress_ratio:
            conn.close()
            return  # 消息不足，不压缩
        
        # 构建总结提示
        conversation_text = "\n".join([
            f"{msg['role']}: {msg['content']}" for msg in messages_to_compress
        ])
        
        prompt = render_template('compression.jinja', 'compress_user',
                                 conversation_text=conversation_text)
        
        try:
            # 调用LLM总结
            summary = await self._summarizer.chat(
                system_prompt=render_template('compression.jinja', 'compress_system'),
                user_prompt=prompt,
                history=[]
            )
            
            # 保存总结
            start_id = messages_to_compress[0]["id"]
            end_id = messages_to_compress[-1]["id"]
            timestamp = datetime.now().timestamp()
            
            cursor.execute("""
                INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id, 
                                     summary_text, message_count, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (platform, chat_id, 1, start_id, end_id, summary, len(messages_to_compress), timestamp))
            
            # 标记消息为已归档
            cursor.execute("""
                UPDATE messages SET is_archived = 1
                WHERE id >= ? AND id <= ? AND platform = ? AND chat_id = ?
            """, (start_id, end_id, platform, chat_id))
            
            conn.commit()
            self._mark_retry_success("compress", platform, chat_id)
            logger.info(f"[{platform}/{chat_id}] 压缩完成: {len(messages_to_compress)} 条消息 -> 1 条总结")
        
        except Exception as e:
            logger.error(f"消息压缩失败: {e}", exc_info=True)
            conn.rollback()
            self._enqueue_retry("compress", platform, chat_id, error=str(e))
        
        finally:
            conn.close()
    
    async def _create_archive_summary(self, platform: str, chat_id: str):
        """创建归档总结（二级压缩）"""
        self.start_retry_worker()
        if self._summarizer is None:
            from brain.llm import get_llm_client
            self._summarizer = get_llm_client(model_alias="summary")
        
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 获取所有一级总结
        cursor.execute("""
            SELECT * FROM summaries
            WHERE platform = ? AND chat_id = ? AND level = 1
            ORDER BY timestamp ASC
        """, (platform, chat_id))
        
        level1_summaries = [dict(row) for row in cursor.fetchall()]
        
        if not level1_summaries:
            conn.close()
            return
        
        # 构建归档总结提示
        summaries_text = "\n\n".join([
            f"[时间段 {i+1}] {s['summary_text']}" 
            for i, s in enumerate(level1_summaries)
        ])
        
        prompt = render_template('compression.jinja', 'archive_user',
                                 summaries_text=summaries_text)
        
        try:
            archive_summary = await self._summarizer.chat(
                system_prompt=render_template('compression.jinja', 'archive_system'),
                user_prompt=prompt,
                history=[]
            )
            
            # 保存归档总结
            start_id = level1_summaries[0]["start_message_id"]
            end_id = level1_summaries[-1]["end_message_id"]
            total_count = sum(s["message_count"] for s in level1_summaries)
            timestamp = datetime.now().timestamp()
            
            cursor.execute("""
                INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                                     summary_text, message_count, timestamp)
                VALUES (?, ?, 3, ?, ?, ?, ?, ?)
            """, (platform, chat_id, 3, start_id, end_id, archive_summary, total_count, timestamp))
            
            # 删除已归档的一级总结
            cursor.execute("""
                DELETE FROM summaries
                WHERE platform = ? AND chat_id = ? AND level = 1 AND id IN ({})
            """.format(','.join('?' * len(level1_summaries))), 
            [platform, chat_id] + [s["id"] for s in level1_summaries])
            
            conn.commit()
            self._mark_retry_success("archive", platform, chat_id)
            logger.info(f"[{platform}/{chat_id}] 创建归档总结: {len(level1_summaries)} 段总结 -> 1 条归档")
        
        except Exception as e:
            logger.error(f"归档总结失败: {e}", exc_info=True)
            conn.rollback()
            self._enqueue_retry("archive", platform, chat_id, error=str(e))
        
        finally:
            conn.close()
    
    def pin_message(self, message_id: int):
        """标记消息为永久保留"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("UPDATE messages SET is_pinned = 1 WHERE id = ?", (message_id,))
        conn.commit()
        conn.close()
        logger.info(f"消息 #{message_id} 已标记为永久保留")
    
    def unpin_message(self, message_id: int):
        """取消永久保留标记"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("UPDATE messages SET is_pinned = 0 WHERE id = ?", (message_id,))
        conn.commit()
        conn.close()
        logger.info(f"消息 #{message_id} 已取消永久保留")

    # ------------------------------------------------------------------
    # 对话分段系统 (Conversation Session Segmentation)
    # ------------------------------------------------------------------

    def close_session(
        self,
        platform: str,
        chat_id: str,
        trigger_type: str = "user",
        metadata: Optional[Dict] = None,
    ) -> Optional[int]:
        """
        关闭当前对话段落：将所有未分配 session 的消息归入一个新的 session。

        当 AI 从 ONLINE 进入 SEMI_ONLINE 时由 controller 调用。

        :param platform: 平台标识
        :param chat_id: 聊天 ID
        :param trigger_type: 触发来源 ('user', 'proactive', 'alarm')
        :param metadata: 额外元数据
        :return: 新创建的 session_id，如果没有未分配消息则返回 None
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        try:
            # 查找所有未分配 session 的消息（session_id IS NULL）
            cursor.execute("""
                SELECT id, timestamp FROM messages
                WHERE platform = ? AND chat_id = ? AND session_id IS NULL
                ORDER BY timestamp ASC
            """, (platform, chat_id))

            unassigned = cursor.fetchall()
            if not unassigned:
                logger.debug(f"[{platform}/{chat_id}] 没有未分配的消息，跳过 session 创建")
                conn.close()
                return None

            msg_ids = [row[0] for row in unassigned]
            started_at = unassigned[0][1]      # 第一条消息的时间戳
            ended_at = unassigned[-1][1]        # 最后一条消息的时间戳
            message_count = len(msg_ids)

            metadata_json = json.dumps(metadata) if metadata else None

            # 创建 session 记录
            cursor.execute("""
                INSERT INTO conversation_sessions
                    (platform, chat_id, started_at, ended_at, message_count, trigger_type, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (platform, chat_id, started_at, ended_at, message_count, trigger_type, metadata_json))

            session_id = cursor.lastrowid
            if session_id is None:
                conn.commit()
                conn.close()
                return None

            # 批量更新消息的 session_id
            cursor.execute("""
                UPDATE messages SET session_id = ?
                WHERE platform = ? AND chat_id = ? AND session_id IS NULL
            """, (session_id, platform, chat_id))

            conn.commit()
            logger.info(
                f"[{platform}/{chat_id}] 对话段落已创建: session_id={session_id}, "
                f"消息数={message_count}, 时间跨度={ended_at - started_at:.0f}s"
            )

            # 异步生成段落摘要（不阻塞）
            try:
                asyncio.create_task(self._generate_session_summary(platform, chat_id, int(session_id)))
            except RuntimeError:
                pass  # 没有事件循环时跳过

            return session_id

        except Exception as e:
            logger.error(f"[{platform}/{chat_id}] 创建对话段落失败: {e}", exc_info=True)
            conn.rollback()
            return None
        finally:
            conn.close()

    async def _generate_session_summary(self, platform: str, chat_id: str, session_id: int):
        """异步为已关闭的对话段落生成摘要。"""
        self.start_retry_worker()
        try:
            if self._summarizer is None:
                from brain.llm import get_llm_client
                self._summarizer = get_llm_client(model_alias="summary")

            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT role, content FROM messages
                WHERE platform = ? AND chat_id = ? AND session_id = ?
                ORDER BY timestamp ASC
            """, (platform, chat_id, session_id))

            messages = cursor.fetchall()
            conn.close()

            if not messages:
                return

            conversation_text = "\n".join([
                f"{msg['role']}: {msg['content']}" for msg in messages
            ])

            # 使用压缩模板生成摘要
            prompt = render_template('compression.jinja', 'compress_user',
                                     conversation_text=conversation_text)

            summary = await self._summarizer.chat(
                system_prompt=render_template('compression.jinja', 'compress_system'),
                user_prompt=prompt,
                history=[]
            )

            # 更新 session 摘要
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE conversation_sessions SET summary = ? WHERE id = ?
            """, (summary, session_id))
            conn.commit()
            conn.close()

            self._mark_retry_success(
                "session_summary",
                platform,
                chat_id,
                payload={"session_id": int(session_id)},
            )

            logger.info(f"[{platform}/{chat_id}] 段落 #{session_id} 摘要已生成 ({len(summary)} chars)")

        except Exception as e:
            logger.error(f"[{platform}/{chat_id}] 生成段落摘要失败: {e}", exc_info=True)
            self._enqueue_retry(
                "session_summary",
                platform,
                chat_id,
                payload={"session_id": int(session_id)},
                error=str(e),
            )

    def get_session_messages(
        self,
        platform: str,
        chat_id: str,
        session_id: int,
    ) -> List[Dict]:
        """获取指定段落的所有消息。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM messages
            WHERE platform = ? AND chat_id = ? AND session_id = ?
            ORDER BY timestamp ASC
        """, (platform, chat_id, session_id))

        messages = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return messages

    def get_current_segment_messages(
        self,
        platform: str,
        chat_id: str,
    ) -> List[Dict]:
        """
        获取当前活跃对话段落的所有消息（session_id IS NULL）。
        
        这些是尚未被 close_session 封闭的消息，代表正在进行中的对话段落。
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM messages
            WHERE platform = ? AND chat_id = ? AND session_id IS NULL
            ORDER BY timestamp ASC
        """, (platform, chat_id))

        messages = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return messages

    def get_messages_since(
        self,
        platform: str,
        chat_id: str,
        since_timestamp: float,
        role: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取指定时间戳之后的消息。
        
        Args:
            platform: 平台标识
            chat_id: 聊天 ID
            since_timestamp: 时间戳（获取此时间之后的消息）
            role: 可选，只获取指定角色的消息（如 'user'）
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if role:
            cursor.execute("""
                SELECT * FROM messages
                WHERE platform = ? AND chat_id = ? AND timestamp > ? AND role = ?
                ORDER BY timestamp ASC
            """, (platform, chat_id, since_timestamp, role))
        else:
            cursor.execute("""
                SELECT * FROM messages
                WHERE platform = ? AND chat_id = ? AND timestamp > ?
                ORDER BY timestamp ASC
            """, (platform, chat_id, since_timestamp))

        messages = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return messages

    def get_recent_sessions(
        self,
        platform: str,
        chat_id: str,
        limit: int = 10,
    ) -> List[Dict]:
        """获取最近的对话段落列表。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM conversation_sessions
            WHERE platform = ? AND chat_id = ?
            ORDER BY ended_at DESC
            LIMIT ?
        """, (platform, chat_id, limit))

        sessions = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return sessions

    def get_current_session_id(self, platform: str, chat_id: str) -> Optional[int]:
        """获取当前活跃对话段落的 ID（如果有未分配消息，说明正在进行中）。"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COUNT(*) FROM messages
            WHERE platform = ? AND chat_id = ? AND session_id IS NULL
        """, (platform, chat_id))

        count = cursor.fetchone()[0]
        conn.close()

        if count > 0:
            return None  # 有未分配消息 → 对话进行中，尚未封闭 session
        
        # 返回最近一个 session_id
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM conversation_sessions
            WHERE platform = ? AND chat_id = ?
            ORDER BY ended_at DESC
            LIMIT 1
        """, (platform, chat_id))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    
    def get_statistics(self, platform: str, chat_id: str) -> Dict:
        """获取消息统计信息"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # 原始消息数
        cursor.execute("""
            SELECT COUNT(*) FROM messages
            WHERE platform = ? AND chat_id = ? AND is_archived = 0 AND is_pinned = 0
        """, (platform, chat_id))
        raw_count = cursor.fetchone()[0]
        
        # 已归档消息数
        cursor.execute("""
            SELECT COUNT(*) FROM messages
            WHERE platform = ? AND chat_id = ? AND is_archived = 1
        """, (platform, chat_id))
        archived_count = cursor.fetchone()[0]
        
        # 永久标记消息数
        cursor.execute("""
            SELECT COUNT(*) FROM messages
            WHERE platform = ? AND chat_id = ? AND is_pinned = 1
        """, (platform, chat_id))
        pinned_count = cursor.fetchone()[0]
        
        # 总结数量
        cursor.execute("""
            SELECT level, COUNT(*) as count FROM summaries
            WHERE platform = ? AND chat_id = ?
            GROUP BY level
        """, (platform, chat_id))
        
        summaries = {row[0]: row[1] for row in cursor.fetchall()}
        
        # 对话段落数
        cursor.execute("""
            SELECT COUNT(*) FROM conversation_sessions
            WHERE platform = ? AND chat_id = ?
        """, (platform, chat_id))
        session_count = cursor.fetchone()[0]
        
        # 当前未分配 session 的消息数（活跃对话中）
        cursor.execute("""
            SELECT COUNT(*) FROM messages
            WHERE platform = ? AND chat_id = ? AND session_id IS NULL
        """, (platform, chat_id))
        active_message_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "raw_messages": raw_count,
            "archived_messages": archived_count,
            "pinned_messages": pinned_count,
            "level1_summaries": summaries.get(1, 0),
            "level2_summaries": summaries.get(2, 0),
            "archive_summaries": summaries.get(3, 0),
            "total_sessions": session_count,
            "active_messages": active_message_count,
        }
    
    def clear_chat_history(self, platform: str, chat_id: str, keep_pinned: bool = True):
        """清除聊天历史（包括对话段落记录）"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        if keep_pinned:
            cursor.execute("""
                DELETE FROM messages WHERE platform = ? AND chat_id = ? AND is_pinned = 0
            """, (platform, chat_id))
        else:
            cursor.execute("""
                DELETE FROM messages WHERE platform = ? AND chat_id = ?
            """, (platform, chat_id))
        
        cursor.execute("""
            DELETE FROM summaries WHERE platform = ? AND chat_id = ?
        """, (platform, chat_id))
        
        cursor.execute("""
            DELETE FROM conversation_sessions WHERE platform = ? AND chat_id = ?
        """, (platform, chat_id))
        
        conn.commit()
        conn.close()
        logger.info(f"[{platform}/{chat_id}] 聊天历史已清除 (保留标记: {keep_pinned})")

    def clear_all_history(self, include_pinned: bool = False):
        """清除所有聊天历史（包括对话段落记录）"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        if include_pinned:
            cursor.execute("DELETE FROM messages")
        else:
            cursor.execute("DELETE FROM messages WHERE is_pinned = 0")

        cursor.execute("DELETE FROM summaries")
        cursor.execute("DELETE FROM conversation_sessions")

        conn.commit()
        conn.close()
        logger.info("已清除全部聊天历史 (包含标记: %s)", include_pinned)
