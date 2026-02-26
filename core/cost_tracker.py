'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 15:27:57
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
Cost Tracker - LLM 调用成本统计模块
支持自动价格获取和手动配置，记录每次 LLM 调用的 token 使用量和成本
"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from pathlib import Path

import config

logger = logging.getLogger(__name__)


class CostTracker:
    """LLM 成本跟踪器"""
    
    def __init__(self, db_path: str = None, custom_prices: Dict[str, Dict[str, float]] = None):
        """
        初始化成本跟踪器
        
        Args:
            db_path: SQLite 数据库路径，默认为工作区的 cost_tracker.db
            custom_prices: 自定义价格配置 {"model_name": {"input": price, "output": price}}
        """
        if db_path is None:
            # 默认存储在工作区
            from workspace_config import get_workspace_manager
            workspace = get_workspace_manager()
            db_path = os.path.join(workspace.root, "cost_tracker.db")
        
        self.db_path = db_path
        # 若未显式传入，则从配置文件加载 cost_tracking.custom_prices
        if custom_prices is None:
            cfg = config.get_config() if hasattr(config, "get_config") else {}
            self.custom_prices = (
                cfg.get("cost_tracking", {}).get("custom_prices", {}) if cfg else {}
            )
        else:
            self.custom_prices = custom_prices
        
        # 对于 :memory: 数据库，需要保持持久连接
        self._persistent_conn = None
        if self.db_path == ":memory:":
            self._persistent_conn = sqlite3.connect(self.db_path)
        
        self._init_db()
    
    def _get_connection(self):
        """获取数据库连接"""
        if self._persistent_conn:
            return self._persistent_conn
        return sqlite3.connect(self.db_path)
    
    def _should_close_connection(self):
        """是否应该关闭连接（文件数据库需要，内存数据库不需要）"""
        return self._persistent_conn is None
        
    def _init_db(self):
        """初始化数据库表"""
        # 对于内存数据库，不需要创建目录
        if self.db_path != ":memory:":
            db_dir = os.path.dirname(self.db_path)
            if db_dir:  # 确保目录路径不为空
                os.makedirs(db_dir, exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    model_alias TEXT,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    input_cost REAL,
                    output_cost REAL,
                    total_cost REAL,
                    context TEXT
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON usage_log(timestamp)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_model ON usage_log(model)
            """)
    
    def _normalize_model_name(self, provider: str, model: str, input_tokens: int) -> str:
        """
        根据上下文长度规范化模型名称
        
        Args:
            provider: 提供商 (gemini/openai)
            model: 原始模型名称
            input_tokens: 输入 token 数
            
        Returns:
            规范化的模型名称 (可能添加 -short 或 -long 后缀)
        """
        # 只有 Gemini 1.5 Pro/Flash 需要区分上下文长度
        if provider == "gemini" and ("1.5-pro" in model or "1.5-flash" in model):
            # 如果已经有后缀了,直接返回
            if model.endswith("-short") or model.endswith("-long"):
                return model
            
            # 根据输入 token 数判断
            if input_tokens > 200_000:
                return f"{model}-long"
            else:
                return f"{model}-short"
        
        return model
    
    def get_model_price(self, provider: str, model: str) -> Optional[Dict[str, float]]:
        """
        获取模型价格（优先自定义配置，其次内置价格）
        
        Returns:
            {"input": price_per_million, "output": price_per_million} or None
        """
        if model in self.custom_prices:
            return self.custom_prices[model]
        logger.warning(f"No price data for {provider}/{model}. Cost will be 0.")
        return None
    
    def calculate_cost(
        self, 
        provider: str, 
        model: str, 
        input_tokens: int, 
        output_tokens: int
    ) -> Tuple[float, float, float]:
        """
        计算成本
        
        Returns:
            (input_cost, output_cost, total_cost) in USD
        """
        # 根据上下文长度规范化模型名称
        normalized_model = self._normalize_model_name(provider, model, input_tokens)
        prices = self.get_model_price(provider, normalized_model)
        
        if not prices:
            return 0.0, 0.0, 0.0
        
        input_cost = (input_tokens / 1_000_000) * prices["input"]
        output_cost = (output_tokens / 1_000_000) * prices["output"]
        total_cost = input_cost + output_cost
        
        return input_cost, output_cost, total_cost
    
    def log_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        model_alias: str = None,
        context: str = None
    ):
        """
        记录一次 LLM 调用
        
        Args:
            provider: 提供商 (gemini/openai)
            model: 模型名称 (会自动根据上下文长度规范化)
            input_tokens: 输入 token 数
            output_tokens: 输出 token 数
            model_alias: 模型别名 (smart/fast/coder/summary)
            context: 上下文说明 (如 "chat", "tool_call", "summary")
        """
        # 根据上下文长度规范化模型名称
        normalized_model = self._normalize_model_name(provider, model, input_tokens)
        
        input_cost, output_cost, total_cost = self.calculate_cost(
            provider, model, input_tokens, output_tokens
        )
        
        timestamp = datetime.now().isoformat()
        
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO usage_log 
                (timestamp, provider, model, model_alias, input_tokens, output_tokens,
                 input_cost, output_cost, total_cost, context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp, provider, normalized_model, model_alias,
                input_tokens, output_tokens,
                input_cost, output_cost, total_cost,
                context
            ))
            conn.commit()
        finally:
            if self._should_close_connection():
                conn.close()
        
        logger.info(
            f"[Cost] {provider}/{model_alias or normalized_model}: "
            f"{input_tokens} in + {output_tokens} out = ${total_cost:.6f}"
        )
    
    def get_stats(
        self, 
        period: str = "total",
        provider: str = None,
        model_alias: str = None
    ) -> Dict:
        """
        获取统计数据
        
        Args:
            period: "today", "week", "month", "total"
            provider: 筛选提供商
            model_alias: 筛选模型别名
            
        Returns:
            {
                "total_calls": int,
                "total_input_tokens": int,
                "total_output_tokens": int,
                "total_cost": float,
                "by_model": {model: {...}},
                "by_alias": {alias: {...}}
            }
        """
        # 计算时间范围
        now = datetime.now()
        if period == "today":
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start_time = now - timedelta(days=7)
        elif period == "month":
            start_time = now - timedelta(days=30)
        else:  # total
            start_time = datetime(2000, 1, 1)
        
        start_str = start_time.isoformat()
        
        # 构建查询
        where_clauses = ["timestamp >= ?"]
        params = [start_str]
        
        if provider:
            where_clauses.append("provider = ?")
            params.append(provider)
        
        if model_alias:
            where_clauses.append("model_alias = ?")
            params.append(model_alias)
        
        where_sql = " AND ".join(where_clauses)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # 总体统计
            row = conn.execute(f"""
                SELECT 
                    COUNT(*) as total_calls,
                    SUM(input_tokens) as total_input_tokens,
                    SUM(output_tokens) as total_output_tokens,
                    SUM(total_cost) as total_cost
                FROM usage_log
                WHERE {where_sql}
            """, params).fetchone()
            
            stats = {
                "total_calls": row["total_calls"] or 0,
                "total_input_tokens": row["total_input_tokens"] or 0,
                "total_output_tokens": row["total_output_tokens"] or 0,
                "total_cost": row["total_cost"] or 0.0
            }
            
            # 按模型统计
            by_model = {}
            for row in conn.execute(f"""
                SELECT 
                    model,
                    COUNT(*) as calls,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_cost) as cost
                FROM usage_log
                WHERE {where_sql}
                GROUP BY model
            """, params):
                by_model[row["model"]] = {
                    "calls": row["calls"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cost": row["cost"]
                }
            
            stats["by_model"] = by_model
            
            # 按别名统计
            by_alias = {}
            for row in conn.execute(f"""
                SELECT 
                    model_alias,
                    COUNT(*) as calls,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_cost) as cost
                FROM usage_log
                WHERE {where_sql} AND model_alias IS NOT NULL
                GROUP BY model_alias
            """, params):
                by_alias[row["model_alias"]] = {
                    "calls": row["calls"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cost": row["cost"]
                }
            
            stats["by_alias"] = by_alias
        
        return stats
    
    def print_stats(self, period: str = "total"):
        """打印统计信息（格式化输出）"""
        stats = self.get_stats(period)
        
        print(f"\n{'='*60}")
        print(f"  LLM Usage Statistics - {period.upper()}")
        print(f"{'='*60}")
        print(f"Total Calls:        {stats['total_calls']}")
        print(f"Total Input Tokens: {stats['total_input_tokens']:,}")
        print(f"Total Output Tokens:{stats['total_output_tokens']:,}")
        print(f"Total Cost:         ${stats['total_cost']:.4f} USD")
        
        if stats['by_alias']:
            print(f"\n{'-'*60}")
            print("By Model Alias:")
            for alias, data in stats['by_alias'].items():
                print(f"  {alias:10s}: {data['calls']:4d} calls, "
                      f"{data['input_tokens']:8,} in, {data['output_tokens']:8,} out, "
                      f"${data['cost']:.4f}")
        
        if stats['by_model']:
            print(f"\n{'-'*60}")
            print("By Model:")
            for model, data in stats['by_model'].items():
                print(f"  {model:30s}: ${data['cost']:.4f}")
        
        print(f"{'='*60}\n")


# 单例实例（延迟初始化）
_tracker_instance: Optional[CostTracker] = None


def get_cost_tracker(custom_prices: Dict[str, Dict[str, float]] = None) -> CostTracker:
    """获取全局 CostTracker 实例"""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = CostTracker(custom_prices=custom_prices)
    return _tracker_instance


def reset_tracker():
    """重置全局实例（用于测试或重新加载配置）"""
    global _tracker_instance
    _tracker_instance = None
