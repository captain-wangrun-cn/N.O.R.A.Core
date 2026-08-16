from abc import ABC, abstractmethod
from typing import Any, List, Dict, AsyncGenerator, Optional
import re
import logging

logger = logging.getLogger(__name__)


class BaseLLM(ABC):
    """Abstract Base Class for all LLM providers."""

    _THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
    _THINK_INLINE_PATTERN = re.compile(r"</?think>", re.IGNORECASE)

    def __init__(self):
        """初始化 LLM，设置 usage 跟踪"""
        self.last_usage: Optional[Dict[str, int]] = None  # {"input_tokens": int, "output_tokens": int}
        # 最近一次调用是否失败。providers 失败时返回的是「一段错误文本」而不是抛异常
        # （历史设计：好让对话链路能把原因说给用户），但非对话调用方会把这段文本当成
        # 正常产物继续用下去 —— draw_desc 被安全策略拦截时，那句 "Error: 模型因内容
        # 安全策略拒绝生成…" 就被当成生图提示词喂进了文生图模型。这个字段是它们唯一
        # 可靠的判别信号，别改成靠匹配返回文本。
        self.last_error: Optional[str] = None

    def _begin_call(self) -> None:
        """每次对外调用入口处清理上一轮的错误状态。"""
        self.last_error = None

    def _fail(self, message: str, reason: str = "") -> str:
        """记录失败并返回给上层的错误文本（保持既有「返回文本而非抛异常」的行为）。"""
        self.last_error = reason or message
        return message

    # 已知的"异常终止"原因。故意采用黑名单而非白名单：各家 SDK 的正常值互不相同
    # （OpenAI stop/tool_calls、Anthropic end_turn/tool_use/stop_sequence、
    # Gemini STOP），白名单漏一个就会把正常回复误判成失败。
    _BLOCKED_FINISH_KEYWORDS = (
        "prohibited",       # Gemini PROHIBITED_CONTENT
        "content_filter",   # OpenAI 内容过滤
        "safety",           # Gemini SAFETY
        "recitation",       # Gemini RECITATION（复述受版权保护内容）
        "blocklist",        # Gemini BLOCKLIST
        "spii",             # Gemini SPII（敏感个人信息）
        "refusal",          # Anthropic refusal
    )
    _TRUNCATED_FINISH_KEYWORDS = ("length", "max_tokens")

    # 失败返回文本统一带这个前缀。用于 is_error_result() 兜底判别——
    # 主判据始终是 last_error，前缀只覆盖「拿不到 client 实例」的场景。
    ERROR_RESULT_PREFIX = "Error: "

    @classmethod
    def is_error_result(cls, text: Any) -> bool:
        """判断一段返回文本是否是 provider 的失败提示而非可用产物。"""
        return str(text or "").lstrip().startswith(cls.ERROR_RESULT_PREFIX)

    def check_finish_reason_and_log(
        self,
        finish_reason: Any,
        response_obj: Any,
        content: Optional[str],
        context: str = "",
    ) -> Optional[str]:
        """
        检查 finish_reason 是否异常；异常时把原始响应打进日志并给出可读的错误文本。

        存在的理由：内容被安全策略拦截时，各家返回的都是 `content=None` + 非正常
        finish_reason，上层只会看到"返回空结果"，完全指向不了真实原因（实测
        draw_desc 被 Gemini 判 prohibited_content 时就是这样）。

        ⚠️ 返回的错误文本**不是可用产物**。调用方若不是把结果直接给用户看
        （生图提示词、参考图挑选等），必须同时检查 `client.last_error` 或
        `BaseLLM.is_error_result()`，否则这段文本会被当成正常输出继续用下去。

        Args:
            finish_reason: 模型返回的终止原因。可能是 str、proto 枚举或 int。
            response_obj: 原始响应对象，异常时整体记进日志——这是唯一的排查线索。
            content: 已提取出的正文，用于在日志里对照。
            context: 调用来源标记（如 "OpenAI chat/draw_desc"），便于定位。

        Returns:
            None 表示正常；非 None 表示异常，返回的文本可直接作为本次调用结果。
        """
        if finish_reason is None or finish_reason == "":
            return None

        # proto 枚举取 .name（str() 会得到 "FinishReason.STOP" 这种带前缀的形式），
        # 再统一去掉可能残留的 "xxx." 前缀。
        raw_name = getattr(finish_reason, "name", None) or str(finish_reason)
        reason_lower = raw_name.rsplit(".", 1)[-1].strip().lower()

        if any(kw in reason_lower for kw in self._BLOCKED_FINISH_KEYWORDS):
            logger.error(
                f"[{context}] 内容被安全策略拦截 finish_reason={raw_name}。"
                f"content={content!r}；原始响应: {response_obj!r:.1000}"
            )
            return self._fail(
                f"{self.ERROR_RESULT_PREFIX}模型因内容安全策略拒绝生成"
                f"（finish_reason={raw_name}）。"
                "检查输入里是否有触发未成年人保护、暴力、色情等策略的内容，"
                "或换一个审查更宽松的端点/模型。",
                reason=f"blocked:{raw_name}",
            )

        if any(kw in reason_lower for kw in self._TRUNCATED_FINISH_KEYWORDS):
            # 截断不算失败：正文通常可用，只是不完整。记 warning 便于回溯。
            logger.warning(
                f"[{context}] 输出被长度上限截断 finish_reason={raw_name}，"
                f"content 长度={len(content or '')}。考虑调大 max_output_tokens。"
            )
            return None

        return None

    @abstractmethod
    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Generates a chat response.

        Args:
            system_prompt: The initial system instruction.
            user_prompt: The latest user message.
            history: A list of previous turns in the conversation.
            tools: (Optional) A list of tool definitions (function declarations).
            multimodal_images: (Optional) 图片输入列表，每项包含 mime_type / bytes / base64 等字段。

        Returns:
            The generated text response from the LLM.
        """
        pass

    @abstractmethod
    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Generates a chat response as an asynchronous stream.

        Args:
            system_prompt: The initial system instruction.
            user_prompt: The latest user message.
            history: A list of previous turns in the conversation.
            tools: (Optional) A list of tool definitions.
            multimodal_images: (Optional) 图片输入列表，每项包含 mime_type / bytes / base64 等字段。

        Yields:
            Dict objects with "type" key ("text" or "tool_call" or "usage").
            Text chunks: {"type": "text", "content": str}
            Tool calls: {"type": "tool_call", "name": str, "args": dict}
            Usage info: {"type": "usage", "input_tokens": int, "output_tokens": int}
        """
        # This is an async generator, so it needs a yield statement.
        # The implementation in the provider will handle the actual yielding.
        if False:
            yield {}
    
    def get_last_usage(self) -> Optional[Dict[str, int]]:
        """获取最近一次调用的 token 使用量"""
        return self.last_usage

    async def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        文生图。默认不实现——只有具备 image-out 能力的 provider 覆盖它。

        Args:
            prompt: 生图提示词。
            reference_images: (Optional) 参考图列表，格式复用 multimodal_images
                （每项含 mime_type / bytes / base64），用于锚定形象一致性。

        Returns:
            {"bytes": bytes, "mime_type": str}

        Raises:
            NotImplementedError: provider 不支持文生图。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持文生图")

    @classmethod
    def strip_think_content(cls, text: str) -> str:
        """移除完整文本中的 think 标签及其内容。"""
        if not text:
            return ""
        cleaned = cls._THINK_BLOCK_PATTERN.sub("", text)
        cleaned = cls._THINK_INLINE_PATTERN.sub("", cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        return cleaned

    @classmethod
    def strip_stream_think_segment(cls, text: str, in_think_block: bool) -> tuple[str, bool]:
        """按流式片段清理 think 内容，支持跨 chunk 维持状态。"""
        if not text:
            return "", in_think_block

        lower_text = text.lower()
        out: List[str] = []
        i = 0

        while i < len(text):
            if in_think_block:
                close_idx = lower_text.find("</think>", i)
                if close_idx == -1:
                    return "".join(out), True
                i = close_idx + len("</think>")
                in_think_block = False
                continue

            open_idx = lower_text.find("<think>", i)
            close_idx = lower_text.find("</think>", i)

            if open_idx == -1 and close_idx == -1:
                out.append(text[i:])
                break

            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                out.append(text[i:close_idx])
                i = close_idx + len("</think>")
                continue

            if open_idx != -1:
                out.append(text[i:open_idx])
                i = open_idx + len("<think>")
                in_think_block = True
                continue

        return "".join(out), in_think_block
