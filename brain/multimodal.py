'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-08 17:03:43
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import base64
import io
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_IMAGE_TAG_PATTERN = re.compile(r'\[image:\s*(.*?)\]', re.IGNORECASE)
_STICKER_TAG_PATTERN = re.compile(r'\[sticker:\s*(.*?)\]', re.IGNORECASE)
_VIDEO_TAG_PATTERN = re.compile(r'\[video:\s*(.*?)\]', re.IGNORECASE)
_FILE_TAG_PATTERN = re.compile(r'\[file:\s*(.*?)\]', re.IGNORECASE)
_VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v', '.3gp'}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB safety limit per image
_MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100MB safety limit per video (Gemini inline limit)
_VIDEO_COMPRESS_THRESHOLD = 10 * 1024 * 1024  # 10MB: videos above this are compressed before upload
_VIDEO_COMPRESS_MAX_DIM = 1280  # max width/height after scaling (720p equivalent)


def _generate_image_id() -> str:
    """生成短且可读的图片 ID，格式: img_<8位hex>"""
    return f"img_{uuid.uuid4().hex[:8]}"


def _generate_video_id() -> str:
    """生成短且可读的视频 ID，格式: vid_<8位hex>"""
    return f"vid_{uuid.uuid4().hex[:8]}"


def _convert_gif_to_png(raw_bytes: bytes) -> Optional[bytes]:
    """
    将 GIF 图片转换为 PNG（提取第一帧）。
    Gemini 不支持 image/gif 格式，需要在发送前转换。
    返回 PNG bytes，失败则返回 None。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes))
        # 提取第一帧（GIF 可能是动图）
        if hasattr(img, "n_frames") and img.n_frames > 1:
            img.seek(0)
        # 转换为 RGBA 或 RGB（处理调色板模式等）
        if img.mode in ("P", "PA"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"GIF 转 PNG 失败: {e}")
        return None


def _resolve_local_image_path(raw_path: str) -> str:
    """
    解析本地媒体文件路径。
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


def _compress_video(input_path: str, max_bytes: int) -> bytes:
    """用 ffmpeg 压缩视频：缩放至最大 {_VIDEO_COMPRESS_MAX_DIM}px，CRF 28，fast preset。"""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        out_path = tmp.name

    try:
        scale = f"scale='min({_VIDEO_COMPRESS_MAX_DIM},iw)':'min({_VIDEO_COMPRESS_MAX_DIM},ih)':force_original_aspect_ratio=decrease"
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", scale,
            "-c:v", "libx264", "-preset", "fast", "-crf", "28",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart",
            out_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=120)

        with open(out_path, "rb") as f:
            compressed = f.read()

        # 如果压缩后仍然超过阈值，不再进一步压缩（避免无限循环或质量极差）
        if len(compressed) > max_bytes:
            logger.warning(
                f"视频压缩后仍 {len(compressed)} bytes（目标 {max_bytes}），"
                f"将使用压缩版本（已比原始更小）"
            )

        return compressed
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _sniff_video_mime(path: str) -> Optional[str]:
    """Best-effort video MIME detection for platform downloads with generic extensions."""
    try:
        with open(path, "rb") as f:
            header = f.read(512)
    except Exception:
        return None

    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "video/x-msvideo"
    if header.startswith(b"FLV"):
        return "video/x-flv"
    if header.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"):
        return "video/x-ms-wmv"
    return None


def _load_video_bytes(resolved: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """读取视频文件，过大时自动 ffmpeg 压缩。返回 (bytes, mime_type, error_message)。"""
    mime_type, _ = mimetypes.guess_type(resolved)
    if not mime_type or not mime_type.startswith("video/"):
        mime_type = _sniff_video_mime(resolved)
    if not mime_type or not mime_type.startswith("video/"):
        return None, None, f"文件并非视频: {resolved}"

    try:
        file_size = os.path.getsize(resolved)
        if file_size > _MAX_VIDEO_BYTES:
            return None, None, f"视频过大（>{_MAX_VIDEO_BYTES} bytes）: {resolved}"

        if file_size > _VIDEO_COMPRESS_THRESHOLD:
            logger.info(
                f"视频 {os.path.basename(resolved)} 大小 {file_size} 字节，"
                f"超过 {_VIDEO_COMPRESS_THRESHOLD} 阈值，正在 ffmpeg 压缩..."
            )
            raw_bytes = _compress_video(resolved, _VIDEO_COMPRESS_THRESHOLD)
            ratio = len(raw_bytes) / file_size * 100
            logger.info(
                f"视频压缩完成: {file_size} -> {len(raw_bytes)} 字节 ({ratio:.0f}%)"
            )
            return raw_bytes, "video/mp4", None

        with open(resolved, "rb") as f:
            raw_bytes = f.read()
        return raw_bytes, mime_type, None
    except Exception as e:
        return None, None, str(e)


def _load_single_image(raw_path: str, seen_paths: set, is_sticker: bool = False) -> Optional[Dict[str, Any]]:
    """尝试从 raw_path 加载一张图片，返回 image payload dict 或 None（失败/跳过时）。

    Args:
        raw_path: 原始路径字符串（来自 [image:...] 或 [sticker:...] 标签）
        seen_paths: 已处理路径集合（去重用）
        is_sticker: 是否是表情包/sticker（影响 payload 中的标记字段）
    """
    resolved = _resolve_local_image_path(raw_path)
    if not resolved:
        # sticker 标签可能包含 emoji 描述文本（如 "🐱 from CatStickers"），
        # 这类非路径内容静默跳过，不打 warning
        if not is_sticker or any(c in raw_path for c in ("/", "\\", ".")):
            logger.warning(f"多模态图片路径不存在，已跳过: {raw_path}")
        return None
    if resolved in seen_paths:
        return None

    mime_type, _ = mimetypes.guess_type(resolved)
    if not mime_type or not mime_type.startswith("image/"):
        # 视频贴纸（.webm 等）无法作为图片解析，静默跳过
        if not is_sticker:
            logger.warning(f"标记为 image 但文件并非图片，已跳过: {resolved}")
        return None

    try:
        file_size = os.path.getsize(resolved)
        if file_size > _MAX_IMAGE_BYTES:
            logger.warning(f"图片过大（>{_MAX_IMAGE_BYTES} bytes），已跳过: {resolved}")
            return None

        with open(resolved, "rb") as f:
            raw_bytes = f.read()

        # GIF 图片兼容性处理：Gemini 不支持 image/gif，
        # 提取第一帧转换为 PNG
        if mime_type == "image/gif":
            png_bytes = _convert_gif_to_png(raw_bytes)
            if png_bytes:
                raw_bytes = png_bytes
                mime_type = "image/png"
                logger.info(f"GIF 已转换为 PNG: {resolved}")

        image_id = _generate_image_id()
        seen_paths.add(resolved)
        payload: Dict[str, Any] = {
            "image_id": image_id,
            "path": resolved,
            "mime_type": mime_type,
            "bytes": raw_bytes,
            "base64": base64.b64encode(raw_bytes).decode("utf-8"),
        }
        if is_sticker:
            payload["is_sticker"] = True
        return payload
    except Exception as e:
        logger.warning(f"读取图片失败，已跳过: {resolved} ({e})")
        return None


def extract_image_payloads(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    从用户文本中提取 [image: ...] 和 [sticker: ...] 标签并加载图片二进制。

    [sticker: path] 标签加载后会在 payload 中设置 is_sticker=True，
    供下游判断"是否为表情包"（用于 fast-image 快速描述）。

    Returns:
        clean_text: 移除 image/sticker 标签后的文本
        images: [{"image_id", "path", "mime_type", "bytes", "base64", "is_sticker"?}, ...]
    """
    if not text:
        return "", []

    images: List[Dict[str, Any]] = []
    seen_paths = set()

    for match in _IMAGE_TAG_PATTERN.finditer(text):
        payload = _load_single_image(match.group(1), seen_paths, is_sticker=False)
        if payload:
            images.append(payload)

    for match in _STICKER_TAG_PATTERN.finditer(text):
        payload = _load_single_image(match.group(1), seen_paths, is_sticker=True)
        if payload:
            images.append(payload)

    clean_text = _IMAGE_TAG_PATTERN.sub("", text)
    clean_text = _STICKER_TAG_PATTERN.sub("", clean_text)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

    return clean_text, images


def get_sticker_images(images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 image payloads 列表中过滤出 is_sticker=True 的表情包图片。"""
    return [img for img in images if img.get("is_sticker")]


def extract_video_payloads(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    从用户文本中提取 [video: ...] 标签以及 [file: ...] 中视频后缀的文件，并加载视频二进制。

    Returns:
        clean_text: 移除已匹配标签后的文本
        videos: [{"video_id", "path", "mime_type", "bytes", "base64"}, ...]
    """
    if not text:
        return "", []

    videos: List[Dict[str, Any]] = []
    seen_paths = set()
    matched_file_tags: List[re.Match] = []

    # 1) 匹配 [video: ...] 标签
    for match in _VIDEO_TAG_PATTERN.finditer(text):
        raw_path = match.group(1)
        resolved = _resolve_local_image_path(raw_path)
        if not resolved:
            logger.warning(f"多模态视频路径不存在，已跳过: {raw_path}")
            continue
        if resolved in seen_paths:
            continue

        raw_bytes, mime_type, error = _load_video_bytes(resolved)
        if error:
            logger.warning(f"加载视频失败: {resolved} ({error})")
            continue

        video_id = _generate_video_id()
        videos.append({
            "video_id": video_id,
            "path": resolved,
            "mime_type": mime_type,
            "bytes": raw_bytes,
            "base64": base64.b64encode(raw_bytes).decode("utf-8"),
        })
        seen_paths.add(resolved)

    # 2) 匹配 [file: ...] 标签中视频后缀的文件
    for match in _FILE_TAG_PATTERN.finditer(text):
        raw_path = match.group(1)
        ext = os.path.splitext(raw_path)[1].lower()
        if ext not in _VIDEO_EXTENSIONS:
            continue

        resolved = _resolve_local_image_path(raw_path)
        if not resolved:
            continue
        if resolved in seen_paths:
            matched_file_tags.append(match)
            continue

        raw_bytes, mime_type, error = _load_video_bytes(resolved)
        if error:
            logger.warning(f"加载文件形式视频失败: {resolved} ({error})")
            continue

        video_id = _generate_video_id()
        videos.append({
            "video_id": video_id,
            "path": resolved,
            "mime_type": mime_type,
            "bytes": raw_bytes,
            "base64": base64.b64encode(raw_bytes).decode("utf-8"),
        })
        seen_paths.add(resolved)
        matched_file_tags.append(match)

    # 清理已匹配的标签：先处理 [file:] 标签（从后往前避免位移），再处理 [video:]
    result_text = text
    for m in reversed(sorted(matched_file_tags, key=lambda x: x.start())):
        result_text = result_text[:m.start()] + result_text[m.end():]
    result_text = _VIDEO_TAG_PATTERN.sub("", result_text)
    result_text = re.sub(r'\n{3,}', '\n\n', result_text).strip()

    return result_text, videos
