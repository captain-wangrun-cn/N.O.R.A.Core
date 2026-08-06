r'''
形象生成图的独立记录库（MongoDB collection: appearance_images）。

不复用 ImageStore：那套 schema 围绕"用户发的媒体 + LLM 打标签"设计
（tag_status / tags / Qdrant 向量 / 一堆 scope 字段），而这里只需要
"她想画什么 / 实际送进模型的提示词 / 文件在哪 / 什么时候"四件事。
不写 Qdrant，不做向量化，不建文本索引。
'''
import logging
from typing import Any, Dict, List, Optional

import pymongo

import config
from memory.image_store import resolve_mongo_db_name

logger = logging.getLogger(__name__)

APPEARANCE_COLLECTION = "appearance_images"


class AppearanceStore:
    """形象生成图的元数据落库入口。MongoDB 不可用时整体降级为 no-op。"""

    def __init__(self):
        self.mongo_client: Optional[pymongo.MongoClient] = None
        self.mongo_col = None
        try:
            mongo_cfg = (config.get_config().get("memory", {}) or {}).get("mongo", {}) or {}
            mongo_uri = mongo_cfg.get("uri", "mongodb://localhost:27017/")
            self.mongo_client = pymongo.MongoClient(mongo_uri)
            db_name = resolve_mongo_db_name(mongo_cfg, self.mongo_client)
            self.mongo_col = self.mongo_client[db_name][APPEARANCE_COLLECTION]
            logger.info(f"AppearanceStore: MongoDB 已连接（db={db_name}.{APPEARANCE_COLLECTION}）。")
        except Exception as e:
            logger.error(f"AppearanceStore: MongoDB 连接失败，形象图仅落盘不入库: {e}")
            self.mongo_client = None
            self.mongo_col = None

    @property
    def enabled(self) -> bool:
        return self.mongo_col is not None

    def save_generated(
        self,
        image_id: str,
        file_path: str,
        request: str,
        draw_prompt: str,
        created_at: str = "",
    ) -> bool:
        """
        记录一张生成图。失败只记 warning 并返回 False——绝不阻断追发。

        :param request: 前脑 [DRAW:...] 里的原文（人类可读，"她想画什么"）
        :param draw_prompt: draw_desc 产出的实际提示词（排查生图效果时要看）
        :param created_at: ISO 时间串，时区跟 memory.message_history.timezone 走
        """
        if self.mongo_col is None:
            logger.warning("AppearanceStore 未就绪，跳过入库: %s", file_path)
            return False

        if not created_at:
            from brain.appearance import now_local

            created_at = now_local().isoformat(timespec="seconds")

        doc = {
            "image_id": image_id,
            "file_path": file_path,
            "request": request or "",
            "draw_prompt": draw_prompt or "",
            "created_at": created_at,
        }
        try:
            self.mongo_col.update_one({"image_id": image_id}, {"$set": doc}, upsert=True)
            return True
        except Exception as e:
            logger.warning(f"AppearanceStore: 写入失败 {image_id}: {e}")
            return False

    def list_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """按时间倒序列出最近的生成图记录（暂无检索需求，仅便于排查）。"""
        if self.mongo_col is None:
            return []
        try:
            cursor = (
                self.mongo_col.find({}, {"_id": 0})
                .sort("created_at", pymongo.DESCENDING)
                .limit(max(1, int(limit)))
            )
            return list(cursor)
        except Exception as e:
            logger.warning(f"AppearanceStore: 查询失败: {e}")
            return []
