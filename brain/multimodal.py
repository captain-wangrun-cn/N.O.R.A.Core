'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-08 17:03:43
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import base64
import logging
import mimetypes
import os
import re
import uuid
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_IMAGE_TAG_PATTERN = re.compile(r'\[image:\s*(.*?)\]', re.IGNORECASE)
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB safety limit per image


def _generate_image_id() -> str:
    """生成短且可读的图片 ID，格式: img_<8位hex>"""
    return f"img_{uuid.uuid4().hex[:8]}"


def _resolve_local_image_path(raw_path: str) -> str:
    """
    解析本地图片路径。
    优先顺序：绝对路径 -> 项目根目录相对路径 -> 当前工作目录相对路径 -> workspace downloads 路径。
    """
    path = (raw_path or "").strip().strip('"').strip("'")
    if not path:
        return ""

    if os.path.isabs(path) and os.path.isfile(path):
        return os.path.normpath(path)

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = [
        os.path.join(project_root, path),
        os.path.join(os.getcwd(), path),
    ]

    # 尝试映射到 workspace 的 downloads / data 目录
    if path.replace('\\', '/').startswith('downloads/'):
        rel = path.replace('\\', '/')[len('downloads/'):]
        try:
            from workspace_config import get_workspace_manager
            downloads_dir = get_workspace_manager().downloads_dir
            candidates.append(os.path.join(downloads_dir, rel))
        except Exception:
            pass
    if path.replace('\\', '/').startswith('data/'):
        rel = path.replace('\\', '/')[len('data/'):]
        try:
            from workspace_config import get_workspace_manager
            data_dir = get_workspace_manager().data_dir
            candidates.append(os.path.join(data_dir, rel))
        except Exception:
            pass

    seen = set()
    for candidate in candidates:
        norm = os.path.normpath(candidate)
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isfile(norm):
            return norm

    return ""


def extract_image_payloads(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    从用户文本中提取 [image: ...] 标签并加载图片二进制。

    Returns:
        clean_text: 移除 image 标签后的文本
        images: [{"image_id", "path", "mime_type", "bytes", "base64"}, ...]
    """
    if not text:
        return "", []

    images: List[Dict[str, Any]] = []
    seen_paths = set()

    for match in _IMAGE_TAG_PATTERN.finditer(text):
        raw_path = match.group(1)
        resolved = _resolve_local_image_path(raw_path)
        if not resolved:
            logger.warning(f"多模态图片路径不存在，已跳过: {raw_path}")
            continue
        if resolved in seen_paths:
            continue

        mime_type, _ = mimetypes.guess_type(resolved)
        if not mime_type or not mime_type.startswith("image/"):
            logger.warning(f"标记为 image 但文件并非图片，已跳过: {resolved}")
            continue

        try:
            file_size = os.path.getsize(resolved)
            if file_size > _MAX_IMAGE_BYTES:
                logger.warning(f"图片过大（>{_MAX_IMAGE_BYTES} bytes），已跳过: {resolved}")
                continue

            with open(resolved, "rb") as f:
                raw_bytes = f.read()

            image_id = _generate_image_id()
            images.append({
                "image_id": image_id,
                "path": resolved,
                "mime_type": mime_type,
                "bytes": raw_bytes,
                "base64": base64.b64encode(raw_bytes).decode("utf-8"),
            })
            seen_paths.add(resolved)
        except Exception as e:
            logger.warning(f"读取图片失败，已跳过: {resolved} ({e})")

    clean_text = _IMAGE_TAG_PATTERN.sub("", text)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

    return clean_text, images
