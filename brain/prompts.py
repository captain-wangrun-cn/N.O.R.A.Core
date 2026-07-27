r'''
Author: WR(captain-wangrun-cn)
Date: 2026-02-07 15:44:02
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-03-05 00:00:00
FilePath: \N.O.R.A.Core\brain\prompts.py
'''
from jinja2 import Environment, FileSystemLoader
import json
import os
import sys  # Added sys import for path manipulation
import logging
import shutil
from datetime import datetime, timedelta, timezone
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
# Nora 的自主人物记忆区：她想记住某个人（尤其群聊里的访客）就在这里按昵称建文件。
WORKSPACE_PEOPLE_DIR = os.path.join(WORKSPACE_MEMORY_DIR, "people")
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
ADAPTER_GLOBAL_PROMPT_FILE = os.path.join(PROJECT_ROOT, "adapters", "PROMPT.md")
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

    # 确保 Nora 的自主人物记忆区存在（跨平台接力：她可按昵称给访客/熟人建档）。
    try:
        os.makedirs(WORKSPACE_PEOPLE_DIR, exist_ok=True)
    except Exception as e:
        logger.warning(f"创建人物记忆目录失败 {WORKSPACE_PEOPLE_DIR}: {e}")


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


def load_recent_daily_memory(days: int = 3) -> str:
    """按需加载最近若干自然日的每日记忆，按旧到新排列。"""
    try:
        day_count = max(0, int(days))
    except (TypeError, ValueError):
        day_count = 0
    if day_count <= 0:
        return ""

    today = datetime.now().date()
    sections = []
    for offset in range(day_count - 1, -1, -1):
        day = today - timedelta(days=offset)
        path = _resolve_memory_file(f"{day.isoformat()}.md")
        content = _read_file_safe(path)
        if content:
            sections.append(
                f'<daily_memory date="{day.isoformat()}">\n{content}\n</daily_memory>'
            )
    if not sections:
        return ""
    return "【近期每日记忆（Recent Daily Memory）】\n" + "\n\n".join(sections)


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


def load_identity_context(
    *,
    include_schedule: bool = True,
    actor_display_name: str = "",
    is_owner: bool = True,
    chat_type: str = "private",
    chat_title: str = "",
    group_member_count: Optional[int] = None,
    group_member_count_status: str = "",
    group_online_count: Optional[int] = None,
    group_online_count_status: str = "",
) -> str:
    """
    加载 SOUL.md + USER.md + MEMORY.md (+ SCHEDULE.md) 组成身份上下文。

    Args:
        include_schedule: 是否在身份上下文中包含 SCHEDULE.md（默认包含）。
        actor_display_name: 当前这轮说话人的昵称（用于"是谁在说话"判断）。
        is_owner: 当前说话人是否为主人。默认 True 以兼容旧调用（私聊即主人）。
        chat_type: 当前会话类型（private/group/...），用于决定是否提示群聊得体性。
        chat_title: 当前群聊/会话显示名。
        group_member_count: Telegram 群成员总数；未知时为 None。
        group_online_count: Telegram 群在线人数；Bot API 通常不可用，未知时为 None。
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

    speaker_block = _build_current_speaker_block(
        actor_display_name=actor_display_name,
        is_owner=is_owner,
        chat_type=chat_type,
    )
    if speaker_block:
        sections.append(speaker_block)

    chat_block = _build_current_chat_block(
        chat_type=chat_type,
        chat_title=chat_title,
        group_member_count=group_member_count,
        group_member_count_status=group_member_count_status,
        group_online_count=group_online_count,
        group_online_count_status=group_online_count_status,
    )
    if chat_block:
        sections.append(chat_block)

    if not sections:
        return ""

    return (
        "【身份与记忆上下文 (Identity & Memory Context)】\n"
        "以下文件在每次会话开始时自动加载。如果 SOUL.md 存在，请体现其人设和语气。\n"
        "请使用通用文件工具 `read_file`、`write_file`、`edit_file` 来更新这些文件。\n"
        "⚠️ USER.md 描述的是你的**主人**，不一定是当前跟你说话的人（见 <current_speaker>）。\n"
        "⚠️ 严格遵守各文件边界：SOUL=AI人设 | USER=主人信息 | SCHEDULE=作息日程 | MEMORY=长期记忆\n\n"
        + "\n\n".join(sections)
    )


def _build_current_speaker_block(
    *,
    actor_display_name: str = "",
    is_owner: bool = True,
    chat_type: str = "private",
) -> str:
    """构造"当前是谁在说话"注入块。

    跨平台接力 / 主人-访客分离：USER.md 写的是主人，但当前说话人不一定是主人
    （尤其群聊）。把说话人昵称与 is_owner 显式注入，让 Nora 判断语气与边界。
    主人私聊是默认场景，不额外加噪声；只有"非主人"或"群聊"才注入提示。
    """
    name = (actor_display_name or "").strip()
    is_group = str(chat_type or "").strip().lower() not in ("private", "")

    # 主人 + 私聊：默认场景，保持上下文纯净，不注入。
    if is_owner and not is_group:
        return ""

    lines = ["<current_speaker>"]
    who = name or "（未提供昵称）"
    if is_owner:
        # 主人，但在群里：提醒注意场合得体性。
        lines.append(f"当前说话人：{who}（这是你的**主人**本人）。")
        lines.append(
            "你正处在**群聊/公开场景**。USER.md 里的私人信息、隐私、私下约定，"
            "要像真人一样判断哪些适合在这种场合提起，哪些只适合私聊时再说——不要机械地把私密内容抖出来。"
        )
    else:
        # 非主人：这是访客。
        lines.append(f"当前说话人：{who}（**不是你的主人**，是一位访客/其他人）。")
        lines.append(
            "USER.md 描述的是你的主人，**不要把它当成对方**——别把主人的偏好、隐私、私下约定安在这位访客身上，"
            "也不要替主人向陌生人透露这些。对访客保持礼貌、有边界，像真人面对不太熟的人那样自然。"
        )
        lines.append(
            "如果你想记住这位访客（聊得多了、值得记），可以在 `data/memory/people/` 下按昵称给 ta 建一份笔记，"
            "记下显示名、平台 id、你对 ta 的印象等——那是你自己的人物记忆区，和主人档案 USER.md 互不混淆。"
        )
    lines.append("</current_speaker>")
    return "\n".join(lines)


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


def get_adapter_platform_metadata_prompt_block(platform: Optional[str] = None) -> str:
    """读取 adapters/<platform>/metadata.json 中的平台名称与平台描述。"""
    platform_id = str(platform or "").strip().lower()
    if not platform_id:
        return ""

    metadata_path = os.path.join(PROJECT_ROOT, "adapters", platform_id, "metadata.json")
    try:
        if not os.path.exists(metadata_path):
            return ""
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception as e:
        logger.warning(f"读取 adapter metadata 失败 {metadata_path}: {e}")
        return ""

    platform_info = metadata.get("platform") if isinstance(metadata, dict) else None
    if not isinstance(platform_info, dict):
        return ""

    platform_name = str(platform_info.get("name") or platform_id).strip()
    platform_description = str(platform_info.get("description") or "").strip()
    if not platform_name and not platform_description:
        return ""

    lines = ["【当前接入平台 (Adapter Platform Metadata)】"]
    if platform_name:
        lines.append(f"平台名称: {platform_name}")
    if platform_description:
        lines.append(f"平台描述: {platform_description}")
    return "\n".join(lines)


def _format_optional_count(value: Optional[int], status: str = "") -> str:
    if value is None:
        return f"未知（{status}）" if status else "未知"
    return str(value)


def _build_current_chat_block(
    *,
    chat_type: str = "private",
    chat_title: str = "",
    group_member_count: Optional[int] = None,
    group_member_count_status: str = "",
    group_online_count: Optional[int] = None,
    group_online_count_status: str = "",
) -> str:
    chat_type_value = str(chat_type or "private").strip().lower()
    if chat_type_value in ("", "private"):
        return ""

    title = (chat_title or "").strip() or "（未提供群名）"
    member_count = _format_optional_count(group_member_count, group_member_count_status)
    online_count = _format_optional_count(group_online_count, group_online_count_status)

    lines = [
        "<current_chat>",
        f"当前聊天类型：{chat_type_value}",
        f"群聊名称：{title}",
        f"群成员总数：{member_count}",
        f"当前在线人数：{online_count}",
    ]
    if group_online_count is None:
        lines.append(
            "注意：Telegram Bot API 通常不提供群聊实时在线人数；如果这里显示未知/不可用，"
            "不要臆测具体在线人数，也不要把未知说成确定数字。"
        )
    lines.append("</current_chat>")
    return "\n".join(lines)


def load_adapter_prompt(platform: Optional[str] = None) -> str:
    """读取通用 adapter prompt + 平台 metadata + 平台专属 prompt。"""
    blocks: list[str] = []
    global_prompt = _read_file_safe(ADAPTER_GLOBAL_PROMPT_FILE, max_chars=6000).strip()
    if global_prompt:
        blocks.append(f"【通用平台适配协议 (Adapter Prompt)】\n{global_prompt}")

    platform_name = str(platform or "").strip().lower()
    if platform_name:
        metadata_prompt = get_adapter_platform_metadata_prompt_block(platform_name)
        if metadata_prompt:
            blocks.append(metadata_prompt)

        prompt_path = os.path.join(PROJECT_ROOT, "adapters", platform_name, "PROMPT.md")
        platform_prompt = _read_file_safe(prompt_path, max_chars=6000).strip()
        if platform_prompt:
            blocks.append(f"【{platform_name} 平台专属协议】\n{platform_prompt}")

    return "\n\n".join(blocks)


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


def get_user_preferences_prompt_block() -> str:
    """返回基于配置生成的用户偏好 prompt 注入块。"""
    prefs = config.get_nora_preferences()
    return render_template("user_preferences.jinja", "user_preferences_block", preferences=prefs)


def get_system_prompt(
    instructions: Optional[List[str]] = None,
    platform: Optional[str] = None,
    custom_scope: str = "system",
    custom_scopes: Optional[Iterable[str]] = None,
    actor_display_name: str = "",
    is_owner: bool = True,
    chat_type: str = "private",
    chat_title: str = "",
    group_member_count: Optional[int] = None,
    group_member_count_status: str = "",
    group_online_count: Optional[int] = None,
    group_online_count_status: str = "",
) -> str:
    """
    使用 Jinja2 模板渲染最终的 System Prompt。

    Args:
        instructions: 额外的指令列表
        platform: 当前运行的平台名称 (如 'telegram'), 用于加载特定的 prompt
        actor_display_name: 当前说话人昵称（主人/访客分离用）
        is_owner: 当前说话人是否为主人（默认 True 兼容旧调用）
        chat_type: 会话类型（private/group），决定群聊得体性提示
        chat_title: 当前群聊显示名。
    """
    _ensure_workspace_identity_files()

    # 从 SOUL.md 读取人设（如果存在），否则回退到 persona_nora.jinja
    soul_content = _read_file_safe(WORKSPACE_SOUL_FILE)
    if soul_content:
        persona_prompt = soul_content
    else:
        persona_template = env.get_template('persona_nora.jinja')
        persona_prompt = persona_template.render()
    
    platform_prompt = load_adapter_prompt(platform)

    # 加载身份与记忆上下文与自定义指令，注入到 instructions
    identity_context = load_identity_context(
        actor_display_name=actor_display_name,
        is_owner=is_owner,
        chat_type=chat_type,
        chat_title=chat_title,
        group_member_count=group_member_count,
        group_member_count_status=group_member_count_status,
        group_online_count=group_online_count,
        group_online_count_status=group_online_count_status,
    )
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
    user_preferences_block = get_user_preferences_prompt_block()
    if user_preferences_block:
        all_instructions.insert(0, user_preferences_block)

    system_template = env.get_template('system.jinja')
    return system_template.render(
        persona_prompt=persona_prompt,
        instructions=all_instructions,
        platform_specific_prompt=platform_prompt
    )
