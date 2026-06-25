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

# 必须与 core/conversation_identity.DEFAULT_RELATIONSHIP_ID 保持一致：
# 单主人实例下所有平台共享的默认记忆作用域。旧数据迁移、未显式传入作用域的新写入
# 都归入这个作用域，从而让 Nora 在任何平台都是同一个连续主体。
DEFAULT_MEMORY_SCOPE_ID = "relationship:owner:default"


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
                memory_scope_id TEXT,  -- 共享上下文主键（跨平台接力）
                place_scope_id TEXT,   -- 来源地点 {platform}:{platform_chat_id}
                actor_display_name TEXT,  -- 说话人昵称
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
                memory_scope_id TEXT,           -- 共享上下文主键（跨平台接力）
                place_scope_id TEXT,            -- 来源地点 {platform}:{platform_chat_id}
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
                memory_scope_id TEXT,      -- 共享上下文主键（跨平台接力）
                place_scope_id TEXT,       -- 来源地点 {platform}:{platform_chat_id}
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
        # 注意：作用域列（memory_scope_id 等）相关索引在 _migrate_db 中、确保列已存在后再创建，
        # 否则对旧库执行时列尚未 ALTER 出来会报 "no such column"。
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
        """执行数据库迁移，安全地添加新列到已有表（非破坏性，自动检测）。"""
        cursor = conn.cursor()

        def _columns(table: str) -> set:
            cursor.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cursor.fetchall()}

        # --- messages.session_id（历史遗留迁移）---
        msg_cols = _columns("messages")
        if "session_id" not in msg_cols:
            logger.info("数据库迁移: 为 messages 表添加 session_id 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER")
            conn.commit()
            msg_cols.add("session_id")

        # --- 跨平台接力：为三张表补充作用域列 ---
        # messages: memory_scope_id / place_scope_id / actor_display_name
        added_scope_to_messages = False
        if "memory_scope_id" not in msg_cols:
            logger.info("数据库迁移: 为 messages 表添加 memory_scope_id 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN memory_scope_id TEXT")
            added_scope_to_messages = True
        if "place_scope_id" not in msg_cols:
            logger.info("数据库迁移: 为 messages 表添加 place_scope_id 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN place_scope_id TEXT")
        if "actor_display_name" not in msg_cols:
            logger.info("数据库迁移: 为 messages 表添加 actor_display_name 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN actor_display_name TEXT")

        # summaries: memory_scope_id / place_scope_id
        sum_cols = _columns("summaries")
        if "memory_scope_id" not in sum_cols:
            logger.info("数据库迁移: 为 summaries 表添加 memory_scope_id 列")
            cursor.execute("ALTER TABLE summaries ADD COLUMN memory_scope_id TEXT")
        if "place_scope_id" not in sum_cols:
            cursor.execute("ALTER TABLE summaries ADD COLUMN place_scope_id TEXT")

        # conversation_sessions: memory_scope_id / place_scope_id
        ses_cols = _columns("conversation_sessions")
        if "memory_scope_id" not in ses_cols:
            logger.info("数据库迁移: 为 conversation_sessions 表添加 memory_scope_id 列")
            cursor.execute("ALTER TABLE conversation_sessions ADD COLUMN memory_scope_id TEXT")
        if "place_scope_id" not in ses_cols:
            cursor.execute("ALTER TABLE conversation_sessions ADD COLUMN place_scope_id TEXT")

        conn.commit()

        # --- 回填旧数据：缺失 memory_scope_id 的行统一归入默认共享作用域，
        #     place_scope_id 由旧 platform/chat_id 生成（保留来源地点）。---
        for table in ("messages", "summaries", "conversation_sessions"):
            try:
                cursor.execute(
                    f"""
                    UPDATE {table}
                    SET memory_scope_id = ?
                    WHERE memory_scope_id IS NULL OR memory_scope_id = ''
                    """,
                    (DEFAULT_MEMORY_SCOPE_ID,),
                )
                cursor.execute(
                    f"""
                    UPDATE {table}
                    SET place_scope_id = platform || ':' || chat_id
                    WHERE place_scope_id IS NULL OR place_scope_id = ''
                    """
                )
            except sqlite3.OperationalError as e:
                logger.warning("回填作用域列失败 (%s): %s", table, e)
        conn.commit()
        if added_scope_to_messages:
            logger.info("数据库迁移: 旧消息已归入默认共享作用域 %s", DEFAULT_MEMORY_SCOPE_ID)

        # 作用域列已确保存在，现在创建相关索引（对新库/旧库都安全）。
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_scope
            ON messages(memory_scope_id, timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_summaries_scope
            ON summaries(memory_scope_id, level, timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_scope
            ON conversation_sessions(memory_scope_id, started_at)
        """)
        conn.commit()

    
    def add_message(
        self,
        platform: str,
        chat_id: str,
        role: str,
        content: str,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        is_pinned: bool = False,
        memory_scope_id: Optional[str] = None,
        place_scope_id: Optional[str] = None,
        actor_display_name: Optional[str] = None,
    ) -> int:
        """
        添加一条消息

    自动插入时间戳（每条消息都会插入）:
    - 时间戳格式: [YYYY-MM-DD HH:MM]

    跨平台接力字段（默认值保证旧调用方兼容）:
    - memory_scope_id: 共享上下文主键；缺省归入默认共享作用域。
    - place_scope_id: 来源地点；缺省由 platform/chat_id 生成。
    - actor_display_name: 说话人昵称。

        :return: 消息ID
        """
        # 作用域兜底：未显式传入时归入单主人默认共享作用域，并由 platform/chat_id 生成来源地点。
        resolved_scope = memory_scope_id or DEFAULT_MEMORY_SCOPE_ID
        resolved_place = place_scope_id or f"{platform}:{chat_id}"

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
            INSERT INTO messages (platform, chat_id, user_id, role, content, timestamp, metadata, is_pinned,
                                  memory_scope_id, place_scope_id, actor_display_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (platform, chat_id, user_id, role, content, timestamp, metadata_json, int(is_pinned),
              resolved_scope, resolved_place, actor_display_name))

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
                memory_scope_id=resolved_scope,
                place_scope_id=resolved_place,
            )
        except Exception as e:
            logger.warning(f"[{platform}/{chat_id}] 写入消息镜像失败: {e}")

        # 触发压缩上下文刷新（独立上下文数据库）
        # 注意：这里传“原始” memory_scope_id（旧调用方为 None），而非 resolved_scope，
        # 以保证旧调用方的压缩段仍按物理 (platform, chat_id) 分区读写/清理，行为不变；
        # 显式传作用域的调用方（接力改造后所有生产调用方）才使用共享 __scope__ 分区。
        self._schedule_context_refresh(platform, chat_id, memory_scope_id)

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

    def _schedule_context_refresh(self, platform: str, chat_id: str, memory_scope_id: Optional[str] = None):
        """异步刷新滑动压缩上下文，失败时记录日志并不中断主流程。"""
        if not self.context_compressor:
            return
        self._launch_background(self.context_compressor.refresh_context(platform, chat_id, memory_scope_id=memory_scope_id))

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
                await self.context_compressor.refresh_context(
                    platform, chat_id, memory_scope_id=self._lookup_scope_for_place(platform, chat_id)
                )
        else:
            logger.warning("未知压缩重试任务类型: %s", task_type)

    def _lookup_scope_for_place(self, platform: str, chat_id: str) -> Optional[str]:
        """根据 (platform, chat_id) 反查其记忆作用域（取最近一条消息的 scope）。

        用于无显式 scope 的内部重试路径，让重试的上下文刷新仍写入正确的共享分区。
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT memory_scope_id FROM messages
                WHERE platform = ? AND chat_id = ? AND memory_scope_id IS NOT NULL
                ORDER BY timestamp DESC LIMIT 1
                """,
                (platform, chat_id),
            )
            row = cursor.fetchone()
            conn.close()
            return row[0] if row and row[0] else None
        except Exception:
            return None
    
    def _scope_filter(
        self,
        platform: str,
        chat_id: str,
        memory_scope_id: Optional[str],
    ) -> Tuple[str, tuple]:
        """返回 (where 片段, 参数) ：传入 memory_scope_id 则按共享作用域过滤，
        否则退回旧的 (platform, chat_id) 分区（保证旧调用方行为不变）。

        memory_scope_id 为内部受控值（来自身份层常量/绑定），不来自用户输入。
        """
        if memory_scope_id:
            return "memory_scope_id = ?", (memory_scope_id,)
        return "platform = ? AND chat_id = ?", (platform, chat_id)

    @staticmethod
    def _platform_message_ids_from_metadata(metadata: Dict[str, Any]) -> List[str]:
        """从消息 metadata 中规范化平台消息 ID 列表。"""
        platform_ids = metadata.get("platform_message_ids") or metadata.get("platform_message_id") or []
        if isinstance(platform_ids, str):
            platform_ids = [platform_ids]
        return [str(pid) for pid in platform_ids if pid is not None]

    @staticmethod
    def _parse_message_metadata(msg: Dict[str, Any]) -> Dict[str, Any]:
        """解析 messages.metadata，失败时返回空 dict。"""
        md_raw = msg.get("metadata")
        try:
            return json.loads(md_raw) if md_raw else {}
        except Exception:
            return {}

    def _format_raw_context_message(
        self,
        msg: Dict[str, Any],
        *,
        scoped: bool = False,
        current_place: str = "",
    ) -> Dict[str, Any]:
        """把 messages 表原始行转换为模型上下文消息结构。"""
        md = self._parse_message_metadata(msg)
        content = msg.get("content", "")
        if scoped:
            content = self._apply_source_label(msg, current_place, content)

        return {
            "role": msg["role"],
            "content": content,
            "timestamp": msg["timestamp"],
            "message_id": msg["id"],
            "metadata": md,
            "platform_message_ids": self._platform_message_ids_from_metadata(md),
            "place_scope_id": msg.get("place_scope_id"),
            "actor_display_name": msg.get("actor_display_name"),
            "source_platform": msg.get("platform"),
        }

    def get_compressed_context_messages(
        self,
        platform: str,
        chat_id: str,
        include_summaries: bool = True,
        memory_scope_id: Optional[str] = None,
        current_place_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """获取压缩/历史上下文，不包含当前活跃段原文窗口。"""
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        scoped = bool(memory_scope_id)
        current_place = current_place_scope_id or f"{platform}:{chat_id}"

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        messages: List[Dict] = []

        cursor.execute(f"""
            SELECT * FROM messages
            WHERE {scope_where} AND is_pinned = 1
            ORDER BY timestamp ASC
        """, scope_params)
        for row in cursor.fetchall():
            msg = dict(row)
            content = msg["content"]
            if scoped:
                content = self._apply_source_label(msg, current_place, content)
            messages.append({
                "role": msg["role"],
                "content": f"[📌 重要] {content}",
                "timestamp": msg["timestamp"],
            })

        if include_summaries:
            cursor.execute(f"""
                SELECT summary_text, message_count FROM summaries
                WHERE {scope_where} AND level = 3
                ORDER BY timestamp DESC
                LIMIT 1
            """, scope_params)
            archive_summary = cursor.fetchone()
            if archive_summary:
                messages.append({
                    "role": "system",
                    "content": f"[📚 早期对话总结，共{archive_summary['message_count']}条] {archive_summary['summary_text']}",
                    "timestamp": 0,
                })

        active_segment_exists = bool(self.get_current_segment_messages(platform, chat_id, memory_scope_id=memory_scope_id))
        if include_summaries and self.context_compressor:
            compressed_segments = self.context_compressor.get_context_messages(
                platform, chat_id, memory_scope_id=memory_scope_id
            )
            if active_segment_exists:
                # ContextCompressor 会把当前活跃段作为最新 slot；这里的“压缩上下文”
                # 只代表历史部分，当前活跃段由 get_current_segment_context_messages 原文提供。
                compressed_segments = [
                    seg for seg in compressed_segments
                    if not (
                        int(seg.get("segment_slot") or 0) == 1
                        and seg.get("segment_type") in {"raw_recent_segment", "compressed_recent_segment"}
                    )
                ]
            if compressed_segments:
                messages.extend(compressed_segments)
                logger.debug(f"[{platform}/{chat_id}] 使用历史上下文压缩库: {len(compressed_segments)} 段")

        if include_summaries:
            cursor.execute(f"""
                SELECT summary_text, level, message_count, timestamp FROM summaries
                WHERE {scope_where} AND level < 3
                ORDER BY timestamp ASC
            """, scope_params)
            for row in cursor.fetchall():
                messages.append({
                    "role": "system",
                    "content": f"[💬 对话摘要，共{row['message_count']}条] {row['summary_text']}",
                    "timestamp": row["timestamp"],
                })

        conn.close()
        messages.sort(key=lambda x: x.get("timestamp", 0))
        scope_tag = memory_scope_id if scoped else f"{platform}/{chat_id}"
        logger.info(f"[{scope_tag}] 获取压缩上下文: {len(messages)} 条消息")
        return messages

    def get_current_segment_context_messages(
        self,
        platform: str,
        chat_id: str,
        memory_scope_id: Optional[str] = None,
        current_place_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """获取当前活跃消息段（session_id IS NULL）的未压缩上下文消息。"""
        scoped = bool(memory_scope_id)
        current_place = current_place_scope_id or f"{platform}:{chat_id}"
        rows = self.get_current_segment_messages(platform, chat_id, memory_scope_id=memory_scope_id)
        messages = [
            self._format_raw_context_message(dict(row), scoped=scoped, current_place=current_place)
            for row in rows
        ]
        scope_tag = memory_scope_id if scoped else f"{platform}/{chat_id}"
        logger.info(f"[{scope_tag}] 获取当前活跃段上下文: {len(messages)} 条消息")
        return messages

    def get_forebrain_context_messages(
        self,
        platform: str,
        chat_id: str,
        memory_scope_id: Optional[str] = None,
        current_place_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """前脑完整上下文：历史压缩上下文 + 当前活跃段未压缩原文。"""
        compressed = self.get_compressed_context_messages(
            platform,
            chat_id,
            memory_scope_id=memory_scope_id,
            current_place_scope_id=current_place_scope_id,
        )
        active = self.get_current_segment_context_messages(
            platform,
            chat_id,
            memory_scope_id=memory_scope_id,
            current_place_scope_id=current_place_scope_id,
        )
        return compressed + active

    def get_context_messages(
        self,
        platform: str,
        chat_id: str,
        limit: Optional[int] = None,
        include_summaries: bool = True,
        memory_scope_id: Optional[str] = None,
        current_place_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取对话上下文消息

        跨平台接力：
        - 传入 memory_scope_id → 按共享作用域聚合所有平台/窗口的历史（A 平台写、B 平台读）。
        - 不传 → 退回旧的 (platform, chat_id) 分区，行为与改造前完全一致。
        - 共享模式下，对“非当前地点”的消息注入来源标签 [place / 昵称]，当前地点消息保持原样，
          以免破坏调用方对最后一条消息的去重比较。

        返回格式:
        [
            {"role": "system", "content": "[归档总结] ..."},
            {"role": "system", "content": "[压缩总结] ..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."},
            ...
        ]
        """
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        scoped = bool(memory_scope_id)
        # 当前地点：共享模式下用于决定哪些消息需要打来源标签。
        current_place = current_place_scope_id or f"{platform}:{chat_id}"

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        messages = []

        # 1. 获取永久标记的消息
        cursor.execute(f"""
            SELECT * FROM messages
            WHERE {scope_where} AND is_pinned = 1
            ORDER BY timestamp ASC
        """, scope_params)

        pinned_messages = [dict(row) for row in cursor.fetchall()]
        if pinned_messages:
            for msg in pinned_messages:
                content = msg["content"]
                if scoped:
                    content = self._apply_source_label(msg, current_place, content)
                messages.append({
                    "role": msg["role"],
                    "content": f"[📌 重要] {content}",
                    "timestamp": msg["timestamp"]
                })

        # 2. 获取归档总结 (Level 3)
        if include_summaries:
            cursor.execute(f"""
                SELECT summary_text, message_count FROM summaries
                WHERE {scope_where} AND level = 3
                ORDER BY timestamp DESC
                LIMIT 1
            """, scope_params)

            archive_summary = cursor.fetchone()
            if archive_summary:
                messages.append({
                    "role": "system",
                    "content": f"[📚 早期对话总结，共{archive_summary['message_count']}条] {archive_summary['summary_text']}",
                    "timestamp": 0
                })

        # 优先使用独立上下文数据库的滑动压缩结果
        if include_summaries and self.context_compressor:
            compressed_segments = self.context_compressor.get_context_messages(
                platform, chat_id, memory_scope_id=memory_scope_id
            )
            if compressed_segments:
                messages.extend(compressed_segments)
                logger.debug(f"[{platform}/{chat_id}] 使用上下文压缩库: {len(compressed_segments)} 段")

        # 3. 获取压缩总结 (Level 1-2)
        if include_summaries:
            cursor.execute(f"""
                SELECT summary_text, level, message_count, timestamp FROM summaries
                WHERE {scope_where} AND level < 3
                ORDER BY timestamp ASC
            """, scope_params)

            for row in cursor.fetchall():
                messages.append({
                    "role": "system",
                    "content": f"[💬 对话摘要，共{row['message_count']}条] {row['summary_text']}",
                    "timestamp": row["timestamp"]
                })

        # 4. 获取原始消息（最近的）
        actual_limit = limit or self.raw_window
        cursor.execute(f"""
            SELECT * FROM messages
            WHERE {scope_where} AND is_archived = 0 AND is_pinned = 0
            ORDER BY timestamp DESC
            LIMIT ?
        """, scope_params + (actual_limit,))

        
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
            md = self._parse_message_metadata(msg)

            content = msg["content"]
            if scoped:
                content = self._apply_source_label(msg, current_place, content)

            messages.append({
                "role": msg["role"],
                "content": content,
                "timestamp": msg["timestamp"],
                "message_id": msg["id"],
                "metadata": md,
                "platform_message_ids": self._platform_message_ids_from_metadata(md),
                "place_scope_id": msg.get("place_scope_id"),
                "actor_display_name": msg.get("actor_display_name"),
                "source_platform": msg.get("platform"),
            })

        conn.close()

        # 按时间排序（归档总结在最前，然后是压缩总结，最后是原始消息）
        messages.sort(key=lambda x: x.get("timestamp", 0))

        scope_tag = memory_scope_id if scoped else f"{platform}/{chat_id}"
        logger.info(f"[{scope_tag}] 获取上下文: {len(messages)} 条消息")
        return messages

    @staticmethod
    def _apply_source_label(msg: Dict[str, Any], current_place: str, content: str) -> str:
        """共享作用域下，为来自“非当前地点”的消息打来源标签 [place / 昵称]。

        当前地点的消息保持原文，避免破坏调用方对最后一条消息内容的去重比较。
        标签加在时间戳之后、正文之前，不影响时间戳剥离逻辑。
        """
        place = str(msg.get("place_scope_id") or "").strip()
        if not place:
            src_platform = msg.get("platform") or ""
            src_chat = msg.get("chat_id") or ""
            place = f"{src_platform}:{src_chat}" if src_platform else ""
        # 当前地点或无地点信息：不打标签
        if not place or place == current_place:
            return content

        actor = str(msg.get("actor_display_name") or "").strip()
        label = f"[来自 {place} / {actor}]" if actor else f"[来自 {place}]"

        text = content or ""
        # 把标签插到时间戳标记之后，保持 [时间] [来自 ...] 正文 的顺序。
        ts_pattern = "] "
        if text.startswith("[") and ts_pattern in text:
            head, rest = text.split(ts_pattern, 1)
            return f"{head}{ts_pattern}{label} {rest}"
        return f"{label} {text}"

    # ------------------------------------------------------------------
    # 日志查询辅助
    # ------------------------------------------------------------------
    def get_messages_between(
        self,
        platform: str,
        chat_id: str,
        start_ts: float,
        end_ts: float,
        memory_scope_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取指定时间戳区间内的原始消息（来自镜像库）。

        传入 memory_scope_id 时按共享作用域聚合所有平台（跨平台接力，如每日总结）。
        """
        if not self.message_log:
            return []

        conn = sqlite3.connect(str(self.message_log.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if memory_scope_id:
            cursor.execute(
                """
                SELECT role, raw_content, timestamp, metadata
                FROM raw_messages
                WHERE memory_scope_id = ? AND timestamp >= ? AND timestamp < ?
                ORDER BY timestamp ASC
                """,
                (memory_scope_id, start_ts, end_ts),
            )
        else:
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

            # 总结继承源消息的作用域，保证共享模式下能检索到（缺失则归入默认共享作用域）。
            seg_scope = messages_to_compress[0].get("memory_scope_id") or DEFAULT_MEMORY_SCOPE_ID
            seg_place = messages_to_compress[0].get("place_scope_id") or f"{platform}:{chat_id}"

            cursor.execute("""
                INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                                     summary_text, message_count, timestamp, memory_scope_id, place_scope_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (platform, chat_id, 1, start_id, end_id, summary, len(messages_to_compress), timestamp,
                  seg_scope, seg_place))
            
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

            # 归档总结继承一级总结的作用域。
            arc_scope = level1_summaries[0].get("memory_scope_id") or DEFAULT_MEMORY_SCOPE_ID
            arc_place = level1_summaries[0].get("place_scope_id") or f"{platform}:{chat_id}"

            cursor.execute("""
                INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                                     summary_text, message_count, timestamp, memory_scope_id, place_scope_id)
                VALUES (?, ?, 3, ?, ?, ?, ?, ?, ?, ?)
            """, (platform, chat_id, 3, start_id, end_id, archive_summary, total_count, timestamp,
                  arc_scope, arc_place))
            
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
        memory_scope_id: Optional[str] = None,
    ) -> Optional[int]:
        """
        关闭当前对话段落：将所有未分配 session 的消息归入一个新的 session。

        当 AI 从 ONLINE 进入 SEMI_ONLINE 时由 controller 调用。

        跨平台接力：传入 memory_scope_id 时，按共享作用域关闭**整段连续对话**
        （跨所有平台/窗口），而不是只关闭单平台那一段——否则在一个平台收尾会把
        另一个平台正在延续的对话错误切断。不传则退回旧的单平台分区行为。

        :param platform: 平台标识
        :param chat_id: 聊天 ID
        :param trigger_type: 触发来源 ('user', 'proactive', 'alarm')
        :param metadata: 额外元数据
        :param memory_scope_id: 共享记忆作用域（跨平台接力）
        :return: 新创建的 session_id，如果没有未分配消息则返回 None
        """
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        try:
            # 查找所有未分配 session 的消息（session_id IS NULL）
            cursor.execute(f"""
                SELECT id, timestamp, memory_scope_id, place_scope_id FROM messages
                WHERE {scope_where} AND session_id IS NULL
                ORDER BY timestamp ASC
            """, scope_params)

            unassigned = cursor.fetchall()
            if not unassigned:
                logger.debug(f"[{platform}/{chat_id}] 没有未分配的消息，跳过 session 创建")
                conn.close()
                return None

            msg_ids = [row[0] for row in unassigned]
            started_at = unassigned[0][1]      # 第一条消息的时间戳
            ended_at = unassigned[-1][1]        # 最后一条消息的时间戳
            message_count = len(msg_ids)
            # session 记录继承作用域：优先消息携带的作用域，否则用传入值/默认。
            seg_scope = unassigned[0][2] or memory_scope_id or DEFAULT_MEMORY_SCOPE_ID
            seg_place = unassigned[0][3] or f"{platform}:{chat_id}"

            metadata_json = json.dumps(metadata) if metadata else None

            # 创建 session 记录
            cursor.execute("""
                INSERT INTO conversation_sessions
                    (platform, chat_id, started_at, ended_at, message_count, trigger_type, metadata,
                     memory_scope_id, place_scope_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (platform, chat_id, started_at, ended_at, message_count, trigger_type, metadata_json,
                  seg_scope, seg_place))

            session_id = cursor.lastrowid
            if session_id is None:
                conn.commit()
                conn.close()
                return None

            # 批量更新消息的 session_id（按相同作用域过滤）
            cursor.execute(f"""
                UPDATE messages SET session_id = ?
                WHERE {scope_where} AND session_id IS NULL
            """, (session_id,) + scope_params)

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

            # 按 session_id 取（全局唯一主键）：跨平台关闭的段落可能含多个平台的消息，
            # 不能再用 (platform, chat_id) 过滤，否则会漏掉其他平台的消息。
            cursor.execute("""
                SELECT role, content FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC
            """, (session_id,))

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
        memory_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取当前活跃对话段落的所有消息（session_id IS NULL）。

        这些是尚未被 close_session 封闭的消息，代表正在进行中的对话段落。
        传入 memory_scope_id 时按共享作用域聚合所有平台的活跃消息（跨平台接力）。
        """
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT * FROM messages
            WHERE {scope_where} AND session_id IS NULL
            ORDER BY timestamp ASC
        """, scope_params)

        messages = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return messages

    def get_messages_since(
        self,
        platform: str,
        chat_id: str,
        since_timestamp: float,
        role: Optional[str] = None,
        memory_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取指定时间戳之后的消息。

        Args:
            platform: 平台标识
            chat_id: 聊天 ID
            since_timestamp: 时间戳（获取此时间之后的消息）
            role: 可选，只获取指定角色的消息（如 'user'）
            memory_scope_id: 可选，按共享作用域聚合所有平台（跨平台接力）
        """
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if role:
            cursor.execute(f"""
                SELECT * FROM messages
                WHERE {scope_where} AND timestamp > ? AND role = ?
                ORDER BY timestamp ASC
            """, scope_params + (since_timestamp, role))
        else:
            cursor.execute(f"""
                SELECT * FROM messages
                WHERE {scope_where} AND timestamp > ?
                ORDER BY timestamp ASC
            """, scope_params + (since_timestamp,))

        messages = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return messages

    def get_recent_sessions(
        self,
        platform: str,
        chat_id: str,
        limit: int = 10,
        memory_scope_id: Optional[str] = None,
    ) -> List[Dict]:
        """获取最近的对话段落列表。传入 memory_scope_id 时跨平台聚合。"""
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT * FROM conversation_sessions
            WHERE {scope_where}
            ORDER BY ended_at DESC
            LIMIT ?
        """, scope_params + (limit,))

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
    
    def get_statistics(self, platform: str, chat_id: str, memory_scope_id: Optional[str] = None) -> Dict:
        """获取消息统计信息。传入 memory_scope_id 时统计整个共享作用域（跨平台）。"""
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # 原始消息数
        cursor.execute(f"""
            SELECT COUNT(*) FROM messages
            WHERE {scope_where} AND is_archived = 0 AND is_pinned = 0
        """, scope_params)
        raw_count = cursor.fetchone()[0]

        # 已归档消息数
        cursor.execute(f"""
            SELECT COUNT(*) FROM messages
            WHERE {scope_where} AND is_archived = 1
        """, scope_params)
        archived_count = cursor.fetchone()[0]

        # 永久标记消息数
        cursor.execute(f"""
            SELECT COUNT(*) FROM messages
            WHERE {scope_where} AND is_pinned = 1
        """, scope_params)
        pinned_count = cursor.fetchone()[0]

        # 总结数量
        cursor.execute(f"""
            SELECT level, COUNT(*) as count FROM summaries
            WHERE {scope_where}
            GROUP BY level
        """, scope_params)

        summaries = {row[0]: row[1] for row in cursor.fetchall()}

        # 对话段落数
        cursor.execute(f"""
            SELECT COUNT(*) FROM conversation_sessions
            WHERE {scope_where}
        """, scope_params)
        session_count = cursor.fetchone()[0]

        # 当前未分配 session 的消息数（活跃对话中）
        cursor.execute(f"""
            SELECT COUNT(*) FROM messages
            WHERE {scope_where} AND session_id IS NULL
        """, scope_params)
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

    def clear_chat_history(
        self,
        platform: str,
        chat_id: str,
        keep_pinned: bool = True,
        memory_scope_id: Optional[str] = None,
    ):
        """清除聊天历史（包括对话段落记录）。

        跨平台接力：传入 memory_scope_id 时清除整个共享作用域的历史
        （即所有平台的共享聊天记录），而不是只清当前平台的分区。
        镜像库/压缩库目前仍按 (platform, chat_id) 物理分区，会逐一清理本作用域涉及的地点。
        """
        scope_where, scope_params = self._scope_filter(platform, chat_id, memory_scope_id)
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # 共享模式下，先收集本作用域涉及的所有 (platform, chat_id) 地点，
        # 以便同步清理按地点物理分区的镜像库与压缩库。
        place_pairs: List[Tuple[str, str]] = []
        if memory_scope_id:
            cursor.execute(
                "SELECT DISTINCT platform, chat_id FROM messages WHERE memory_scope_id = ?",
                (memory_scope_id,),
            )
            place_pairs = [(row[0], row[1]) for row in cursor.fetchall()]
        if not place_pairs:
            place_pairs = [(platform, chat_id)]

        if keep_pinned:
            cursor.execute(f"""
                DELETE FROM messages WHERE {scope_where} AND is_pinned = 0
            """, scope_params)
        else:
            cursor.execute(f"""
                DELETE FROM messages WHERE {scope_where}
            """, scope_params)

        cursor.execute(f"""
            DELETE FROM summaries WHERE {scope_where}
        """, scope_params)

        cursor.execute(f"""
            DELETE FROM conversation_sessions WHERE {scope_where}
        """, scope_params)

        # 重试队列按 (platform, chat_id) 分区，逐地点清理。
        for p, c in place_pairs:
            cursor.execute("""
                DELETE FROM compression_retry_queue WHERE platform = ? AND chat_id = ?
            """, (p, c))

        conn.commit()
        conn.close()
        for p, c in place_pairs:
            try:
                self.message_log.clear_chat(p, c)
            except Exception:
                logger.warning(f"[{p}/{c}] 清理消息镜像失败", exc_info=True)
            try:
                self.context_compressor.clear_chat(p, c)
            except Exception:
                logger.warning(f"[{p}/{c}] 清理压缩上下文失败", exc_info=True)
        scope_tag = memory_scope_id if memory_scope_id else f"{platform}/{chat_id}"
        logger.info(f"[{scope_tag}] 聊天历史已清除 (保留标记: {keep_pinned}, 涉及地点: {len(place_pairs)})")

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
