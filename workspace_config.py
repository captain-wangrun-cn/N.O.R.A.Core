'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-08 20:19:58
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
# workspace_config.py - 工作区配置管理

import os
import json
from pathlib import Path
from typing import Optional

# 默认工作区位置（可在 config.yml 中覆盖）
DEFAULT_WORKSPACE_PATH = os.path.expanduser("~/.nora/workspace")

# 工作区子目录结构
WORKSPACE_STRUCTURE = {
    "skills": "技能脚本存储目录",
    "downloads": "下载文件存储目录",
    "data": "数据存储目录",
    "logs": "日志文件目录",
    "cache": "缓存目录",
}

class WorkspaceManager:
    """管理 N.O.R.A. Core 的工作区。"""
    
    def __init__(self, workspace_path: Optional[str] = None):
        """
        初始化工作区管理器。
        
        Args:
            workspace_path: 工作区路径。如果为 None，使用 config.yml 中的设置或默认路径。
        """
        if workspace_path:
            self.workspace_root = os.path.abspath(os.path.expanduser(workspace_path))
        else:
            # 尝试从 config 读取
            try:
                import config
                config.load_config()
                workspace_cfg = config.get_config().get("workspace", {})
                raw_path = workspace_cfg.get("root_path", DEFAULT_WORKSPACE_PATH)
                self.workspace_root = os.path.abspath(os.path.expanduser(raw_path))
            except:
                self.workspace_root = os.path.abspath(DEFAULT_WORKSPACE_PATH)
        
        # 初始化工作区
        self._init_workspace()
    
    def _init_workspace(self):
        """初始化工作区目录结构。"""
        os.makedirs(self.workspace_root, exist_ok=True)
        
        # 创建所有子目录
        for subdir in WORKSPACE_STRUCTURE.keys():
            subdir_path = os.path.join(self.workspace_root, subdir)
            os.makedirs(subdir_path, exist_ok=True)
        
        # 创建 .workspace 标记文件
        marker_file = os.path.join(self.workspace_root, ".workspace")
        if not os.path.exists(marker_file):
            with open(marker_file, 'w', encoding='utf-8') as f:
                f.write(json.dumps({
                    "version": "1.0",
                    "description": "N.O.R.A. Core Workspace"
                }, indent=2, ensure_ascii=False))
    
    @property
    def root(self) -> str:
        """工作区根路径。"""
        return self.workspace_root
    
    @property
    def skills_dir(self) -> str:
        """技能目录。"""
        return os.path.join(self.workspace_root, "skills")
    
    @property
    def downloads_dir(self) -> str:
        """下载目录。"""
        return os.path.join(self.workspace_root, "downloads")
    
    @property
    def data_dir(self) -> str:
        """数据目录。"""
        return os.path.join(self.workspace_root, "data")
    
    @property
    def logs_dir(self) -> str:
        """日志目录。"""
        return os.path.join(self.workspace_root, "logs")
    
    @property
    def cache_dir(self) -> str:
        """缓存目录。"""
        return os.path.join(self.workspace_root, "cache")
    
    def get_subdir(self, name: str) -> str:
        """获取指定名称的子目录路径（如果存在）。"""
        if name in WORKSPACE_STRUCTURE:
            return os.path.join(self.workspace_root, name)
        raise ValueError(f"Unknown workspace subdirectory: {name}")
    
    def is_path_in_workspace(self, path: str) -> bool:
        """检查路径是否在工作区内。"""
        abs_path = os.path.abspath(path)
        return abs_path.startswith(os.path.abspath(self.workspace_root))


# 全局工作区实例（惰性初始化）
_workspace_manager = None

def get_workspace_manager(workspace_path: Optional[str] = None) -> WorkspaceManager:
    """获取全局工作区管理器实例。"""
    global _workspace_manager
    if _workspace_manager is None:
        _workspace_manager = WorkspaceManager(workspace_path)
    return _workspace_manager

def reset_workspace(workspace_path: str):
    """重置工作区（用于测试或切换工作区）。"""
    global _workspace_manager
    _workspace_manager = WorkspaceManager(workspace_path)
