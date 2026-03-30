'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-08 20:19:58
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
# workspace_config.py - 工作区配置管理

import os
import json
import shutil
import re
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
                cfg = config.get_config() or {}
                workspace_cfg = cfg.get("workspace", {})
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

        # 启动时同步仓库预装技能到 workspace/skills：缺失则复制，低版本则升级覆盖
        self._sync_bundled_skills_by_version()

    def _parse_version_tuple(self, version_str: str) -> tuple:
        """将版本号转换为可比较的三段整数元组，非法值回退为 (0,0,0)。"""
        if not version_str:
            return (0, 0, 0)
        parts = re.findall(r"\d+", str(version_str))
        if not parts:
            return (0, 0, 0)
        nums = [int(x) for x in parts[:3]]
        while len(nums) < 3:
            nums.append(0)
        return tuple(nums)

    def _extract_skill_version(self, skill_md_path: str) -> str:
        """从 SKILL.md 的 YAML Frontmatter 中提取 version。"""
        try:
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            fm = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
            block = fm.group(1) if fm else content
            m = re.search(r'^version\s*:\s*([^\n#]+)', block, re.MULTILINE)
            return m.group(1).strip() if m else "0.0.0"
        except Exception:
            return "0.0.0"

    def _sync_bundled_skills_by_version(self):
        """启动时同步仓库预装技能：缺失则复制，workspace 版本低于仓库则自动覆盖更新。"""
        seed_marker = os.path.join(self.skills_dir, ".seeded")

        # 仓库自带技能目录（workspace_config.py 同级的 skills/）
        bundled_skills_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
        if not os.path.isdir(bundled_skills_root):
            return

        os.makedirs(self.skills_dir, exist_ok=True)

        copied = 0
        upgraded = 0
        skipped = 0
        for item in os.listdir(bundled_skills_root):
            src_dir = os.path.join(bundled_skills_root, item)
            src_skill_md = os.path.join(src_dir, "SKILL.md")
            if not os.path.isdir(src_dir):
                continue
            # 仅复制真正的技能目录（包含 SKILL.md）
            if not os.path.isfile(src_skill_md):
                continue

            dst_dir = os.path.join(self.skills_dir, item)
            dst_skill_md = os.path.join(dst_dir, "SKILL.md")

            if not os.path.exists(dst_dir):
                shutil.copytree(src_dir, dst_dir)
                copied += 1
                continue

            repo_ver = self._extract_skill_version(src_skill_md)
            ws_ver = self._extract_skill_version(dst_skill_md) if os.path.isfile(dst_skill_md) else "0.0.0"
            if self._parse_version_tuple(ws_ver) < self._parse_version_tuple(repo_ver):
                shutil.rmtree(dst_dir, ignore_errors=True)
                shutil.copytree(src_dir, dst_dir)
                upgraded += 1
            else:
                skipped += 1

        with open(seed_marker, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                "seeded": True,
                "copied_skills": copied,
                "upgraded_skills": upgraded,
                "skipped_skills": skipped,
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
