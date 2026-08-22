from __future__ import annotations

from pathlib import Path


class TelegramCleanupMixin:
    def _purge_qdrant(self) -> str:
        """清空所有 Qdrant 相关集合。"""
        messages = []
        try:
            from memory.vector import VectorStore
            vs = VectorStore()
            if not vs.client:
                return "⚠️ Qdrant 未连接，未执行。"
            vs.client.delete_collection(collection_name=vs.collection_name)
            vs._ensure_collection()
            messages.append(f"- Qdrant 记忆集合已清空: {vs.collection_name}")
        except Exception as e:
            messages.append(f"- Qdrant 记忆集合清空失败: {e}")

        try:
            from memory.image_store import ImageStore
            img_store = ImageStore()
            if not img_store.qdrant:
                return "⚠️ Qdrant 未连接（图片库），未执行。"
            img_store.qdrant.delete_collection(collection_name=img_store.qdrant_collection)
            img_store._ensure_qdrant_collection()
            messages.append(f"- Qdrant 图片集合已清空: {img_store.qdrant_collection}")
        except Exception as e:
            messages.append(f"- Qdrant 图片集合清空失败: {e}")

        return "\n".join(messages) if messages else "⚠️ Qdrant 未执行。"

    def _purge_mongo(self) -> str:
        """清空 MongoDB 图片库集合。"""
        try:
            from memory.image_store import ImageStore, MONGO_COLLECTION
            img_store = ImageStore()
            if img_store.mongo_col is None:
                return "⚠️ MongoDB 未连接，未执行。"
            img_store.mongo_col.delete_many({})
            return f"- MongoDB 图片集合已清空: {MONGO_COLLECTION}"
        except Exception as e:
            return f"- MongoDB 清空失败: {e}"

    def _purge_chat_history(self) -> str:
        """清空全部聊天记录。"""
        try:
            self.message_history.clear_all_history(include_pinned=True)
            return "- 聊天记录已全部清空（含 pinned）"
        except Exception as e:
            return f"- 聊天记录清空失败: {e}"

    def _purge_message_log(self) -> str:
        """清空原文镜像库（message_log.db）。"""
        try:
            path = Path(self.message_history.message_log.db_path)
            if path.exists():
                path.unlink()
            # 重新初始化以确保表结构存在
            self.message_history.message_log._init_db()
            return f"- 消息镜像库已清空: {path.name}"
        except Exception as e:
            return f"- 消息镜像库清空失败: {e}"

    def _purge_context_db(self) -> str:
        """清空上下文压缩库（context_compression.db）。"""
        try:
            path = Path(self.message_history.context_compressor.db_path)
            if path.exists():
                path.unlink()
            # 重新初始化以确保表结构存在
            self.message_history.context_compressor._init_db()
            return f"- 上下文压缩库已清空: {path.name}"
        except Exception as e:
            return f"- 上下文压缩库清空失败: {e}"
