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
import shutil
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Iterable, List

from workspace_config import get_workspace_manager
import config
from lexicon import LexiconManager

logger = logging.getLogger(__name__)

# 设置 Jinja2 环境
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
env = Environment(loader=FileSystemLoader(template_dir))


def render_template(template_name: str, block_name: str, **kwargs) -> str:
    """
    从指定模板文件中渲染某个 block 的内容。
    
    使用 Jinja2 block 机制，每个模板文件可包含多个命名 block，
    调用时指定 block_name 即可获取对应片段。
    
    Args:
        template_name: 模板文件名 (如 'compression.jinja')
        block_name: 模板中的 block 名称 (如 'compress_system')
        **kwargs: 传递给模板的变量
    
    Returns:
        渲染后的字符串
    """
    try:
        template = env.get_template(template_name)
        # 渲染整个模板以初始化 blocks
        context = template.new_context(kwargs)
        # 获取指定 block
        block_func = template.blocks.get(block_name)
        if block_func is None:
            logger.error(f"模板 {template_name} 中未找到 block '{block_name}'")
            return ""
        return "".join(block_func(context)).strip()
    except Exception as e:
        logger.error(f"渲染模板 {template_name}#{block_name} 失败: {e}")
        return ""

# 项目根目录（brain/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))

# 工作区与数据路径（优先放到 workspace 下）
_workspace = get_workspace_manager()
WORKSPACE_ROOT = _workspace.root
WORKSPACE_DATA_DIR = _workspace.data_dir
WORKSPACE_MEMORY_DIR = os.path.join(WORKSPACE_DATA_DIR, "memory")
LEGACY_MEMORY_DIR = os.path.join(PROJECT_ROOT, "memory")
os.makedirs(WORKSPACE_MEMORY_DIR, exist_ok=True)

# 身份/用户/记忆文件路径（优先 workspace，回退仓库根目录兼容旧数据）
WORKSPACE_SOUL_FILE = os.path.join(WORKSPACE_ROOT, "SOUL.md")
WORKSPACE_USER_FILE = os.path.join(WORKSPACE_ROOT, "USER.md")
WORKSPACE_MEMORY_FILE = os.path.join(WORKSPACE_MEMORY_DIR, "MEMORY.md")
WORKSPACE_SCHEDULE_FILE = os.path.join(WORKSPACE_ROOT, "SCHEDULE.md")
WORKSPACE_CUSTOM_FILE = os.path.join(WORKSPACE_ROOT, "CUSTOM.md")
WORKSPACE_SECRET_FILE = os.path.join(WORKSPACE_ROOT, "SECRET.md")
LEXICON_GLOBAL_PROMPT_FILE = os.path.join(PROJECT_ROOT, "lexicon", "PROMPT.md")
LEGACY_SOUL_FILE = os.path.join(PROJECT_ROOT, "SOUL.md")
LEGACY_USER_FILE = os.path.join(PROJECT_ROOT, "USER.md")
LEGACY_MEMORY_FILE = os.path.join(PROJECT_ROOT, "MEMORY.md")
LEGACY_SCHEDULE_FILE = os.path.join(PROJECT_ROOT, "SCHEDULE.md")
LEGACY_CUSTOM_FILE = os.path.join(PROJECT_ROOT, "CUSTOM.md")
LEGACY_SECRET_FILE = os.path.join(PROJECT_ROOT, "SECRET.md")

# 注入到 system prompt 的最大字符数（防止 token 爆炸）
BOOTSTRAP_MAX_CHARS = 20000
_LEXICON_MANAGER: Optional[LexiconManager] = None


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


def _ensure_workspace_identity_files():
    """
    如果 workspace 下缺少 SOUL/USER/MEMORY，则从仓库默认文件复制过去。
    """
    mapping = [
        (WORKSPACE_SOUL_FILE, LEGACY_SOUL_FILE),
        (WORKSPACE_USER_FILE, LEGACY_USER_FILE),
        (WORKSPACE_MEMORY_FILE, LEGACY_MEMORY_FILE),
        (WORKSPACE_SCHEDULE_FILE, LEGACY_SCHEDULE_FILE),
        (WORKSPACE_CUSTOM_FILE, LEGACY_CUSTOM_FILE),
    ]
    for dst, src in mapping:
        if os.path.exists(dst):
            continue
        if src and os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copyfile(src, dst)
                logger.info(f"已将默认文件复制到 workspace: {dst}")
            except Exception as e:
                logger.warning(f"复制 {src} 到 {dst} 失败: {e}")


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


def get_soul_prompt() -> str:
    """
    读取 SOUL.md 内容，优先 workspace 版本，回退到 persona_nora.jinja。
    供需要人设上下文的轻量场景（如中断检测）使用。
    """
    _ensure_workspace_identity_files()
    soul = _read_file_safe(WORKSPACE_SOUL_FILE)
    if soul:
        return soul
    persona_template = env.get_template('persona_nora.jinja')
    return persona_template.render()


def load_identity_context(*, include_schedule: bool = True) -> str:
    """
    加载 SOUL.md + USER.md + MEMORY.md (+ SCHEDULE.md) 组成身份上下文。

    Args:
        include_schedule: 是否在身份上下文中包含 SCHEDULE.md（默认包含）。
    """
    _ensure_workspace_identity_files()

    sections = []

    soul = _read_file_safe(WORKSPACE_SOUL_FILE)
    if soul:
        sections.append(f"<soul>\n{soul}\n</soul>")
    else:
        logger.warning(f"SOUL.md 未找到或为空: {WORKSPACE_SOUL_FILE}")

    user = _read_file_safe(WORKSPACE_USER_FILE)
    if user:
        sections.append(f"<user_profile>\n{user}\n</user_profile>")
    else:
        logger.warning(f"USER.md 未找到或为空: {WORKSPACE_USER_FILE}")

    memory_file_path = _resolve_memory_file("MEMORY.md")
    memory = _read_file_safe(memory_file_path)
    if memory:
        sections.append(f"<long_term_memory>\n{memory}\n</long_term_memory>")

    if include_schedule:
        schedule = _read_file_safe(WORKSPACE_SCHEDULE_FILE)
        if schedule:
            sections.append(f"<schedule>\n{schedule}\n</schedule>")

    if not sections:
        return ""

    return (
        "【身份与记忆上下文 (Identity & Memory Context)】\n"
        "以下文件在每次会话开始时自动加载。如果 SOUL.md 存在，请体现其人设和语气。\n"
        "请使用通用文件工具 `read_file`、`write_file`、`edit_file` 来更新这些文件。\n"
        "⚠️ 严格遵守各文件边界：SOUL=AI人设 | USER=用户信息 | SCHEDULE=作息日程 | MEMORY=长期记忆\n\n"
        + "\n\n".join(sections)
    )


def load_custom_prompt() -> str:
    """读取 CUSTOM.md 作为用户自定义全局指令（只读注入，不应被通用文件工具修改）。"""
    _ensure_workspace_identity_files()
    return _read_file_safe(WORKSPACE_CUSTOM_FILE)


def _normalize_custom_scopes(scopes: Optional[Iterable[str]]) -> List[str]:
    if not scopes:
        return []
    normalized = []
    for scope in scopes:
        if scope is None:
            continue
        value = str(scope).strip().lower()
        if value:
            normalized.append(value)
    return normalized


def should_inject_custom(scope: str, scopes_override: Optional[Iterable[str]] = None) -> bool:
    """
    Decide whether CUSTOM.md should be injected for a given scope.

    Args:
        scope: injection scope identifier (e.g. "system", "smart", "image").
        scopes_override: optional override list; when omitted, uses config.yml custom_injection.scopes.
    """
    scope_value = str(scope).strip().lower()
    scopes = _normalize_custom_scopes(scopes_override)
    if not scopes:
        scopes = _normalize_custom_scopes(config.get_custom_injection_scopes())

    if not scopes:
        return True

    if "none" in scopes or "off" in scopes:
        return False

    return scope_value in scopes


def get_lexicon_manager() -> Optional[LexiconManager]:
    """懒初始化词库管理器。"""
    global _LEXICON_MANAGER
    if _LEXICON_MANAGER is not None:
        return _LEXICON_MANAGER

    try:
        _LEXICON_MANAGER = LexiconManager()
    except Exception:
        logger.warning("词库管理器初始化失败，已降级忽略。", exc_info=True)
        _LEXICON_MANAGER = None

    return _LEXICON_MANAGER


def get_always_lexicon_system_prompt_block() -> str:
    """返回注入到 system prompt 的常加载词库块。"""
    manager = get_lexicon_manager()
    if manager is None:
        return ""

    try:
        return manager.build_always_system_prompt_block()
    except Exception:
        logger.warning("构建常加载词库 system 注入块失败，已忽略。", exc_info=True)
        return ""


def load_lexicon_global_prompt() -> str:
    """读取词库全局用途说明（lexicon/PROMPT.md）。"""
    return _read_file_safe(LEXICON_GLOBAL_PROMPT_FILE, max_chars=6000)


def get_lexicon_global_system_prompt_block() -> str:
    """返回用于全链路 system prompt 注入的词库全局说明块。"""
    global_prompt = load_lexicon_global_prompt().strip()
    if not global_prompt:
        return ""
    return f"【词库全局说明 (Lexicon Global Prompt)】\n{global_prompt}"


def get_lazy_lexicon_user_prompt_block(user_text: str, limit: int = 10) -> str:
    """根据用户输入返回懒加载词库命中注入块（用于 user prompt）。"""
    manager = get_lexicon_manager()
    if manager is None:
        return ""

    try:
        return manager.build_lazy_user_prompt_block(user_text, limit=limit)
    except Exception:
        logger.warning("构建懒加载词库 user 注入块失败，已忽略。", exc_info=True)
        return ""


def get_system_prompt(
    instructions: Optional[List[str]] = None,
    platform: Optional[str] = None,
    custom_scope: str = "system",
    custom_scopes: Optional[Iterable[str]] = None,
) -> str:
    """
    使用 Jinja2 模板渲染最终的 System Prompt。
    
    Args:
        instructions: 额外的指令列表
        platform: 当前运行的平台名称 (如 'telegram'), 用于加载特定的 prompt
    """
    _ensure_workspace_identity_files()

    # 从 SOUL.md 读取人设（如果存在），否则回退到 persona_nora.jinja
    soul_content = _read_file_safe(WORKSPACE_SOUL_FILE)
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

    # 加载身份与记忆上下文与自定义指令，注入到 instructions
    identity_context = load_identity_context()
    custom_prompt = load_custom_prompt() if should_inject_custom(custom_scope, custom_scopes) else ""
    all_instructions = list(instructions or [])
    if custom_prompt:
        all_instructions.insert(0, f"【用户自定义全局指令 CUSTOM.md】\n{custom_prompt}")
    if identity_context:
        # 身份上下文放在 instructions 最前面
        all_instructions.insert(0, identity_context)
    lexicon_system_block = get_always_lexicon_system_prompt_block()
    if lexicon_system_block:
        all_instructions.insert(0, lexicon_system_block)

    system_template = env.get_template('system.jinja')
    return system_template.render(
        persona_prompt=persona_prompt,
        instructions=all_instructions,
        platform_specific_prompt=platform_prompt
    )
