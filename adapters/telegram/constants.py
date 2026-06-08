from __future__ import annotations

import re

# Telegram limits
TG_PHOTO_MAX_SIZE = 10 * 1024 * 1024       # 10MB for photos
TG_DOCUMENT_MAX_SIZE = 50 * 1024 * 1024     # 50MB for documents
COMPRESS_TARGET_SIZE = 9 * 1024 * 1024       # Compress target: 9MB (safety margin)
TG_MSG_MAX_LENGTH = 4096                     # Telegram message text limit
HISTORICAL_REPLY_IMAGE_DIRECT_MARKER = "[NORA_HISTORICAL_REPLY_IMAGE_DIRECT]"

# 文件扩展名 -> 媒体类型映射
MEDIA_TYPES = {
    'photo':    {'.png', '.jpg', '.jpeg', '.webp', '.bmp'},
    'gif':      {'.gif'},
    'video':    {'.mp4', '.mkv', '.avi', '.mov', '.webm'},
    'audio':    {'.mp3', '.ogg', '.wav', '.flac', '.m4a', '.opus'},
    'voice':    {'.oga'},
    'document': {
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.rar', '.7z', '.tar', '.gz',
        '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.cs',
        '.go', '.rs', '.rb', '.php', '.sh', '.bat', '.ps1',
        '.json', '.yml', '.yaml', '.toml', '.xml', '.csv',
        '.md', '.txt', '.log', '.ini', '.cfg', '.conf',
        '.html', '.css', '.sql', '.r', '.kt', '.swift',
    },
}

FILE_PATTERN = re.compile(
    r'(?:!\[.*?\]\((.*?)\))|'
    r'(?:<img\s+src="(.*?)"[^>]*>)|'
    r'(?:\[(?:image|file|audio|video|doc):\s*(.*?)\])|'
    r'(?:^|\s)((?:https?://[^\s]+\.(?:'
    + '|'.join(ext.lstrip('.') for exts in MEDIA_TYPES.values() for ext in exts)
    + r'))\b)|'
    r'(?:^|\s)((?:/|\.{0,2}/|[a-zA-Z]:\\)[^\s<>"\']+\.(?:'
    + '|'.join(ext.lstrip('.') for exts in MEDIA_TYPES.values() for ext in exts)
    + r'))\b',
    re.IGNORECASE | re.MULTILINE,
)
