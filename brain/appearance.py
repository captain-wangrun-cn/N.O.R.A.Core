r'''
形象系统共享原语：APPEARANCE.md 读取、参考图加载/落盘、manifest 维护。

两条生图路径共用这里，避免各写一份：
- 前脑 `[DRAW:...]` → core/appearance_gen.py::AppearanceGenMixin
- 后脑工具 → brain/tools.py::generate_appearance_reference
'''
import base64
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from brain.prompts import (
    WORKSPACE_APPEARANCE_FILE,
    WORKSPACE_APPEARANCE_GENERATED_DIR,
    WORKSPACE_APPEARANCE_MANIFEST,
    WORKSPACE_APPEARANCE_REFS_DIR,
)

logger = logging.getLogger(__name__)

EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}
MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
# 单次生图最多带几张参考图：再多只会摊薄注意力，成本还线性涨。
MAX_REFERENCE_IMAGES = 3

def now_local() -> datetime:
    """当前时间，时区跟 memory.message_history.timezone 走（别用系统本地时区）。"""
    try:
        from zoneinfo import ZoneInfo

        tz_name = ((config.get_message_history_config() or {}).get("timezone") or "").strip()
        if tz_name:
            return datetime.now(ZoneInfo(tz_name))
    except Exception:
        logger.debug("解析显示时区失败，回退系统本地时区。", exc_info=True)
    return datetime.now()


def read_appearance_text() -> str:
    """读 APPEARANCE.md 全文；不存在或为空返回 ""。"""
    try:
        if not os.path.exists(WORKSPACE_APPEARANCE_FILE):
            return ""
        with open(WORKSPACE_APPEARANCE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning(f"读取 APPEARANCE.md 失败 {WORKSPACE_APPEARANCE_FILE}: {e}")
        return ""


def load_manifest() -> Dict[str, Any]:
    """读 refs/manifest.json；缺失或损坏时返回空骨架（不抛）。"""
    try:
        if os.path.exists(WORKSPACE_APPEARANCE_MANIFEST):
            with open(WORKSPACE_APPEARANCE_MANIFEST, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("refs"), list):
                return data
            logger.warning("manifest.json 结构不符合预期，按空处理。")
    except Exception as e:
        logger.warning(f"读取参考图 manifest 失败 {WORKSPACE_APPEARANCE_MANIFEST}: {e}")
    return {"refs": []}


def list_reference_files() -> List[str]:
    """列出 refs/ 下实际存在的图片文件名（不含 manifest.json）。"""
    try:
        if not os.path.isdir(WORKSPACE_APPEARANCE_REFS_DIR):
            return []
        names = []
        for name in sorted(os.listdir(WORKSPACE_APPEARANCE_REFS_DIR)):
            ext = os.path.splitext(name)[1].lower()
            if ext in MIME_BY_EXT and os.path.isfile(
                os.path.join(WORKSPACE_APPEARANCE_REFS_DIR, name)
            ):
                names.append(name)
        return names
    except Exception as e:
        logger.warning(f"列出参考图失败 {WORKSPACE_APPEARANCE_REFS_DIR}: {e}")
        return []


def describe_manifest_for_prompt() -> str:
    """把 manifest + 实际文件合成一段给 draw_desc 看的清单。

    以磁盘为准：manifest 里写了但文件没了的条目不列出，避免模型挑到不存在的图。
    """
    existing = list_reference_files()
    if not existing:
        return "（当前没有任何参考图）"

    usage_by_file = {}
    for entry in load_manifest().get("refs", []):
        if isinstance(entry, dict) and entry.get("file"):
            usage_by_file[str(entry["file"])] = str(entry.get("usage") or "").strip()

    lines = []
    for name in existing:
        usage = usage_by_file.get(name)
        lines.append(f"- {name}: {usage}" if usage else f"- {name}: （未标注用途）")
    return "\n".join(lines)


def load_reference_images(filenames: List[str]) -> List[Dict[str, Any]]:
    """
    按文件名加载参考图，返回 multimodal_images 同构结构
    （mime_type / bytes / base64），gemini 与 openai 两条路都能直接用。

    不存在的文件跳过并记日志——降级为少带几张参考图，不阻断生图。
    """
    images: List[Dict[str, Any]] = []
    for raw in filenames:
        name = os.path.basename(str(raw or "").strip())
        if not name:
            continue
        path = os.path.join(WORKSPACE_APPEARANCE_REFS_DIR, name)
        if not os.path.isfile(path):
            logger.warning(f"参考图不存在，已跳过: {path}")
            continue
        try:
            with open(path, "rb") as f:
                raw_bytes = f.read()
        except Exception as e:
            logger.warning(f"读取参考图失败 {path}: {e}")
            continue
        if not raw_bytes:
            continue
        ext = os.path.splitext(name)[1].lower()
        images.append({
            "mime_type": MIME_BY_EXT.get(ext, "image/png"),
            "bytes": raw_bytes,
            "base64": base64.b64encode(raw_bytes).decode("utf-8"),
        })
        if len(images) >= MAX_REFERENCE_IMAGES:
            break
    return images


def save_reference_image(view: str, image_bytes: bytes, mime_type: str, usage: str = "") -> str:
    """把生成的图存成 refs/{view}{ext} 并更新 manifest，返回绝对路径。"""
    safe_view = "".join(c for c in str(view or "").strip() if c.isalnum() or c in "-_") or "ref"
    ext = EXT_BY_MIME.get((mime_type or "").lower(), ".png")
    os.makedirs(WORKSPACE_APPEARANCE_REFS_DIR, exist_ok=True)
    path = os.path.join(WORKSPACE_APPEARANCE_REFS_DIR, f"{safe_view}{ext}")
    with open(path, "wb") as f:
        f.write(image_bytes)

    filename = os.path.basename(path)
    manifest = load_manifest()
    refs = [
        entry for entry in manifest.get("refs", [])
        if isinstance(entry, dict) and str(entry.get("file") or "") != filename
    ]
    refs.append({
        "file": filename,
        "view": safe_view,
        "usage": (usage or "").strip(),
        "updated_at": now_local().isoformat(timespec="seconds"),
    })
    manifest["refs"] = refs
    try:
        with open(WORKSPACE_APPEARANCE_MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写入参考图 manifest 失败 {WORKSPACE_APPEARANCE_MANIFEST}: {e}")

    return os.path.abspath(path)


def find_reference_path(view: str) -> str:
    """找某个 view 已有的参考图路径（任意支持的后缀）；没有返回 ""。"""
    safe_view = "".join(c for c in str(view or "").strip() if c.isalnum() or c in "-_")
    if not safe_view:
        return ""
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        path = os.path.join(WORKSPACE_APPEARANCE_REFS_DIR, f"{safe_view}{ext}")
        if os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def new_generated_id() -> str:
    """生成图 id，风格对齐 brain/multimodal.py::_generate_image_id。"""
    return f"draw_{uuid.uuid4().hex[:8]}"


def save_generated_image(image_bytes: bytes, mime_type: str, image_id: Optional[str] = None) -> str:
    """把日常生图存到 generated/YYYY-MM-DD/ 下，返回绝对路径。"""
    image_id = image_id or new_generated_id()
    ext = EXT_BY_MIME.get((mime_type or "").lower(), ".png")
    day_dir = os.path.join(WORKSPACE_APPEARANCE_GENERATED_DIR, now_local().strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"{image_id}{ext}")
    with open(path, "wb") as f:
        f.write(image_bytes)
    return os.path.abspath(path)


def draw_models_configured() -> bool:
    """draw 与 draw_desc 都配了才算启用形象生图。"""
    return bool(config.get_model_name("draw")) and bool(config.get_model_name("draw_desc"))

