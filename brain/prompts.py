r'''
Author: WR(captain-wangrun-cn)
Date: 2026-02-07 15:44:02
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-03-05 00:00:00
FilePath: \N.O.R.A.Core\brain\prompts.py
'''
from jinja2 import Environment, FileSystemLoader
import os
import sys  # Added sys import for path manipulation
import logging
from datetime import datetime, timezone
from typing import Optional

from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

# 设置 Jinja2 环境
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
env = Environment(loader=FileSystemLoader(template_dir))

# 项目根目录（brain/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))

# 工作区与数据路径（优先放到 workspace 下）
_workspace = get_workspace_manager()
WORKSPACE_ROOT = _workspace.root
WORKSPACE_DATA_DIR = _workspace.data_dir
WORKSPACE_MEMORY_DIR = os.path.join(WORKSPACE_DATA_DIR, "memory")
LEGACY_MEMORY_DIR = os.path.join(PROJECT_ROOT, "memory")
os.makedirs(WORKSPACE_MEMORY_DIR, exist_ok=True)

# 身份/用户/记忆文件路径（记忆优先使用 workspace 路径，回退到仓库根目录兼容旧数据）
SOUL_FILE = os.path.join(PROJECT_ROOT, "SOUL.md")
USER_FILE = os.path.join(PROJECT_ROOT, "USER.md")
WORKSPACE_MEMORY_FILE = os.path.join(WORKSPACE_MEMORY_DIR, "MEMORY.md")
LEGACY_MEMORY_FILE = os.path.join(PROJECT_ROOT, "MEMORY.md")

# 注入到 system prompt 的最大字符数（防止 token 爆炸）
BOOTSTRAP_MAX_CHARS = 20000


def _read_file_safe(path: str, max_chars: int = BOOTSTRAP_MAX_CHARS) -> str:
    """安全读取文件，返回内容或空字符串。超过 max_chars 会截断。"""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n... [截断：文件超过 {max_chars} 字符]"
            return content
    except Exception as e:
        logger.warning(f"读取文件失败 {path}: {e}")
    return ""


def _resolve_memory_file(filename: str) -> str:
    """
    优先返回 workspace 下的记忆文件路径；若不存在则回退到仓库根目录对应路径（兼容旧数据）。
    """
    workspace_path = os.path.join(WORKSPACE_MEMORY_DIR, filename)
    legacy_dir_path = os.path.join(LEGACY_MEMORY_DIR, filename)
    legacy_root_path = os.path.join(PROJECT_ROOT, filename)

    if os.path.exists(workspace_path):
        return workspace_path
    if os.path.exists(legacy_dir_path):
        return legacy_dir_path
    return workspace_path if not os.path.exists(legacy_root_path) else legacy_root_path


def _load_daily_memory() -> str:
    """（已禁用）每日记忆不再自动注入，以防上下文膨胀。"""
    return ""


def load_identity_context() -> str:
    """
    加载 SOUL.md + USER.md + MEMORY.md + 每日记忆，拼接为身份上下文注入块。
    在每次会话开始时调用，注入到 system prompt。
    """
    sections = []

    soul = _read_file_safe(SOUL_FILE)
    if soul:
        sections.append(f"<soul>\n{soul}\n</soul>")

    user = _read_file_safe(USER_FILE)
    if user:
        sections.append(f"<user_profile>\n{user}\n</user_profile>")

    memory_file_path = _resolve_memory_file("MEMORY.md")
    memory = _read_file_safe(memory_file_path)
    if memory:
        sections.append(f"<long_term_memory>\n{memory}\n</long_term_memory>")

    if not sections:
        return ""

    return (
        "【身份与记忆上下文 (Identity & Memory Context)】\n"
        "以下文件在每次会话开始时自动加载。如果 SOUL.md 存在，请体现其人设和语气。\n"
        "请使用通用文件工具 `read_file`、`write_file`、`edit_file` 来更新这些文件。\n\n"
        + "\n\n".join(sections)
    )


def get_system_prompt(instructions: list = None, platform: str = None) -> str:
    """
    使用 Jinja2 模板渲染最终的 System Prompt。
    
    Args:
        instructions: 额外的指令列表
        platform: 当前运行的平台名称 (如 'telegram'), 用于加载特定的 prompt
    """
    # 从 SOUL.md 读取人设（如果存在），否则回退到 persona_nora.jinja
    soul_content = _read_file_safe(SOUL_FILE)
    if soul_content:
        persona_prompt = soul_content
    else:
        persona_template = env.get_template('persona_nora.jinja')
        persona_prompt = persona_template.render()
    
    # 尝试加载平台特定的 prompt
    platform_prompt = ""
    if platform:
        # 直接尝试读取 adapters/{platform}/PROMPT.md
        prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'adapters', platform, 'PROMPT.md')
        try:
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    platform_prompt = f.read()
        except Exception as e:
            print(f"Warning: Failed to load {platform} prompt: {e}")

    # 加载身份与记忆上下文，注入到 instructions
    identity_context = load_identity_context()
    all_instructions = list(instructions or [])
    if identity_context:
        # 身份上下文放在 instructions 最前面
        all_instructions.insert(0, identity_context)

    system_template = env.get_template('system.jinja')
    return system_template.render(
        persona_prompt=persona_prompt,
        instructions=all_instructions,
        platform_specific_prompt=platform_prompt
    )
