from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class LexiconManager:
    """轻量词库管理器（常加载 + 懒加载）。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        self.base_dir = root
        self.always_dir = root / "always"
        self.lazy_dir = root / "lazy"

        self.always_terms: Dict[str, str] = {}
        self.lazy_terms: Dict[str, str] = {}
        self.lazy_manifest: Dict[str, Path] = {}
        self.always_prompts: List[str] = []
        self.lazy_prompts_by_file: Dict[Path, List[str]] = {}

        self._lazy_files: List[Path] = []
        self._loaded_lazy_files: set[Path] = set()
        self._lazy_hit_count = 0

        self._load_always_terms()
        self._index_lazy_files()

    def _read_terms_from_file(self, file_path: Path) -> Dict[str, str]:
        terms, _ = self._parse_dict_file(file_path)
        return terms

    def _parse_dict_file(self, file_path: Path) -> tuple[Dict[str, str], List[str]]:
        terms: Dict[str, str] = {}
        prompt_hints: List[str] = []
        if not file_path.exists():
            return terms, prompt_hints

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("读取词库文件失败: %s (%s)", file_path, exc)
            return terms, prompt_hints

        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.lower().startswith("@prompt:"):
                hint = line.split(":", 1)[1].strip()
                if hint:
                    prompt_hints.append(hint)
                continue

            if ":" not in line:
                logger.debug("跳过非法词条行 %s:%d -> %s", file_path, line_number, raw_line)
                continue

            term, meaning = line.split(":", 1)
            term = term.strip()
            meaning = meaning.strip()
            if not term or not meaning:
                logger.debug("跳过空词条 %s:%d -> %s", file_path, line_number, raw_line)
                continue

            terms[term] = meaning

        return terms, prompt_hints

    def _load_always_terms(self) -> None:
        self.always_terms.clear()
        self.always_prompts = []
        if not self.always_dir.exists():
            return

        for file_path in sorted(self.always_dir.glob("*.dict")):
            terms, prompt_hints = self._parse_dict_file(file_path)
            self.always_terms.update(terms)
            self.always_prompts.extend(prompt_hints)

    def _index_lazy_files(self) -> None:
        if not self.lazy_dir.exists():
            self._lazy_files = []
            self.lazy_manifest = {}
            return

        self._lazy_files = sorted(self.lazy_dir.glob("*.dict"))
        self._build_lazy_manifest()

    def _build_lazy_manifest(self) -> None:
        """从懒加载词库中提取 key，建立 term -> file 的 manifest。"""
        manifest: Dict[str, Path] = {}
        prompts_by_file: Dict[Path, List[str]] = {}
        for file_path in self._lazy_files:
            terms, prompt_hints = self._parse_dict_file(file_path)
            for term in terms.keys():
                manifest[term] = file_path
            prompts_by_file[file_path] = prompt_hints

        self.lazy_manifest = manifest
        self.lazy_prompts_by_file = prompts_by_file

    def _load_one_lazy_file(self, file_path: Path) -> Dict[str, str]:
        terms = self._read_terms_from_file(file_path)
        self.lazy_terms.update(terms)
        self._loaded_lazy_files.add(file_path)
        return terms

    def _load_lazy_term_from_manifest(self, term: str) -> str | None:
        file_path = self.lazy_manifest.get(term)
        if not file_path:
            return None

        if file_path not in self._loaded_lazy_files:
            self._load_one_lazy_file(file_path)

        if term in self.lazy_terms:
            self._lazy_hit_count += 1
            return self.lazy_terms[term]

        return None

    def get(self, term: str) -> str | None:
        """按关键词精确查询含义。"""
        if not term:
            return None

        if term in self.always_terms:
            return self.always_terms[term]

        if term in self.lazy_terms:
            self._lazy_hit_count += 1
            return self.lazy_terms[term]

        meaning = self._load_lazy_term_from_manifest(term)
        if meaning is not None:
            return meaning

        return None

    def supplement_meanings_for_text(self, text: str, limit: int = 10) -> List[Tuple[str, str]]:
        """从用户输入中匹配懒加载关键词，并补充对应含义。"""
        if not text or limit <= 0:
            return []

        lowered = text.lower()
        candidates = set(self.lazy_manifest.keys())
        sorted_candidates = sorted(candidates, key=len, reverse=True)

        supplements: List[Tuple[str, str]] = []
        for term in sorted_candidates:
            if term.lower() not in lowered:
                continue

            meaning = self.get(term)
            if meaning is None:
                continue

            supplements.append((term, meaning))
            if len(supplements) >= limit:
                break

        return supplements

    def build_always_system_prompt_block(self, term_limit: int = 200) -> str:
        """构建注入到 system prompt 的常加载词库信息。"""
        if term_limit <= 0:
            return ""

        lines: List[str] = ["【词库上下文（常加载）】"]
        if self.always_prompts:
            lines.append("用途提示：")
            for hint in self.always_prompts:
                lines.append(f"- {hint}")

        lines.append("常用词释义：")
        count = 0
        for term in sorted(self.always_terms.keys()):
            lines.append(f"- {term}: {self.always_terms[term]}")
            count += 1
            if count >= term_limit:
                break

        if count == 0:
            return ""

        return "\n".join(lines)

    def build_lazy_user_prompt_block(self, text: str, limit: int = 10) -> str:
        """根据用户输入命中懒加载词条，构建注入到 user prompt 的补充信息。"""
        matched = self.supplement_meanings_for_text(text, limit=limit)
        if not matched:
            return ""

        lines: List[str] = ["【词库补充（懒加载命中）】"]

        # 命中的词汇所对应词库文件提示（可选）
        file_hints: List[str] = []
        for term, _ in matched:
            file_path = self.lazy_manifest.get(term)
            if not file_path:
                continue
            for hint in self.lazy_prompts_by_file.get(file_path, []):
                if hint not in file_hints:
                    file_hints.append(hint)

        if file_hints:
            lines.append("用途提示：")
            for hint in file_hints:
                lines.append(f"- {hint}")

        lines.append("命中词释义：")
        for term, meaning in matched:
            lines.append(f"- {term}: {meaning}")

        return "\n".join(lines)

    def search(self, keyword: str, limit: int = 10) -> List[Tuple[str, str]]:
        """模糊检索（在 term 或 meaning 中包含关键字）。"""
        if not keyword or limit <= 0:
            return []

        all_terms: Dict[str, str] = {}
        all_terms.update(self.always_terms)
        all_terms.update(self.lazy_terms)

        keyword_lower = keyword.lower()
        result: List[Tuple[str, str]] = []
        for term, meaning in all_terms.items():
            if keyword_lower in term.lower() or keyword_lower in meaning.lower():
                result.append((term, meaning))
                if len(result) >= limit:
                    break

        return result

    def stats(self) -> Dict[str, int]:
        """返回词库加载统计信息。"""
        return {
            "always_terms": len(self.always_terms),
            "lazy_terms_loaded": len(self.lazy_terms),
            "lazy_files_total": len(self._lazy_files),
            "lazy_files_loaded": len(self._loaded_lazy_files),
            "lazy_manifest_terms": len(self.lazy_manifest),
            "lazy_hit_count": self._lazy_hit_count,
        }
