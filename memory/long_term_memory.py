"""长期记忆管理器

读取 MEMORY.md 索引文件中引用的记忆文件，拆成条目后按本轮 query 选择相关条目，
拼接后用于注入 system prompt。按 project_id 分桶，避免跨项目污染。

存储结构:
  data/memory/
    ├── _global/                  # 所有项目共享
    │   ├── MEMORY.md
    │   └── common_corrections.md
    ├── project_55/               # 只给 project_id=55 注入
    │   ├── MEMORY.md
    │   └── eu_metrics.md
    └── project_60/               # 只给 project_id=60 注入
        ├── MEMORY.md
        └── us_events.md
"""

import logging
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_MEMORY_LINES = 50
_MAX_MEMORY_BYTES = 5000
_MAX_SELECTED_PARTS = 5


class LongTermMemory:
    """长期记忆 — 按 project_id 分桶读取记忆文件并构建注入内容"""

    def __init__(self, data_path: str = "data/memory"):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        # 缓存: key=(project_id,), value=记忆条目列表（选择前的原始条目）
        self._cache: Dict[Tuple[int, ...], list[str]] = {}
        self._cache_mtime: Dict[Tuple[int, ...], float] = {}

    def load_memory_context(
        self,
        project_id: Optional[int] = None,
        *,
        query_text: str | None = None,
        last_query_state: Optional[Dict[str, object]] = None,
    ) -> str:
        """加载记忆内容（用于注入 system prompt）

        按 project_id 分桶加载：
        1. 始终加载 _global/ 下的全局记忆
        2. 如果指定了 project_id，额外加载 project_{id}/ 下的项目记忆

        Args:
            project_id: 项目ID，为 None 时只加载全局记忆
        """
        parts = self._load_parts(project_id)
        if not parts:
            logger.debug(f"LongTermMemory: no memory found for project={project_id}")
            return ""

        # 选择依赖本轮 query，必须每次重新做；缓存只缓存磁盘读取结果
        selected_parts = self._select_relevant_parts(parts, query_text=query_text, last_query_state=last_query_state)
        result = self._truncate("\n\n".join(selected_parts))
        logger.debug(f"LongTermMemory: selected {len(selected_parts)}/{len(parts)} entries, {len(result)} chars for project={project_id}")
        return result

    def _load_parts(self, project_id: Optional[int]) -> list[str]:
        """读取 _global + project_{id} 下的全部记忆条目（按 mtime 缓存，与 query 无关）"""
        cache_key = (project_id,) if project_id is not None else (None,)

        if cache_key in self._cache and not self._is_cache_stale(cache_key):
            logger.debug(f"LongTermMemory: cache hit for project={project_id}")
            return self._cache[cache_key]

        logger.debug(f"LongTermMemory: loading from disk for project={project_id}")
        parts: list[str] = []

        # 1. 加载全局记忆
        global_dir = self.data_path / "_global"
        global_parts = self._load_dir(global_dir)
        if global_parts:
            parts.extend(global_parts)

        # 2. 加载项目记忆
        if project_id is not None:
            project_dir = self.data_path / f"project_{project_id}"
            project_parts = self._load_dir(project_dir)
            if project_parts:
                parts.extend(project_parts)

        self._cache[cache_key] = parts
        self._cache_mtime[cache_key] = self._latest_mtime(project_id)
        return parts

    def _load_dir(self, directory: Path) -> list[str]:
        """读取某个目录下的 MEMORY.md 索引及其引用的文件"""
        memory_index = directory / "MEMORY.md"
        if not memory_index.exists():
            return []

        return self._load_all_files(memory_index)

    def _load_all_files(self, memory_index: Path) -> list[str]:
        """读取 MEMORY.md 索引中引用的所有文件内容"""
        try:
            index_content = memory_index.read_text(encoding="utf-8").strip()
        except OSError:
            return []

        base_dir = memory_index.parent
        parts = []
        for line in index_content.split("\n"):
            match = re.match(r"- \[.+?\]\((.+?)\)", line)
            if match:
                file_path = base_dir / match.group(1)
                if file_path.exists():
                    try:
                        content = file_path.read_text(encoding="utf-8")
                        # 去掉 frontmatter（--- ... ---）
                        content = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
                        parts.extend(self._split_entries(content))
                    except OSError as e:
                        logger.warning(f"Failed to read memory file {file_path}: {e}")

        return parts

    @staticmethod
    def _split_entries(content: str) -> list[str]:
        """把一个记忆文件拆成可独立选择的条目

        纯列表文件（如 auto_learned.md，每行一条 "- [category] ..."）按顶层列表项拆分，
        否则整个文件作为一个条目（手写的说明性文档保持完整）。
        """
        stripped = content.strip()
        if not stripped:
            return []
        lines = stripped.split("\n")
        if not all(line.startswith("- ") or line.startswith("  ") or not line.strip() for line in lines):
            return [stripped]
        entries: list[str] = []
        for line in lines:
            if line.startswith("- "):
                entries.append(line)
            elif line.strip() and entries:
                entries[-1] += "\n" + line
        return entries

    def _select_relevant_parts(
        self,
        parts: list[str],
        *,
        query_text: str | None = None,
        last_query_state: Optional[Dict[str, object]] = None,
    ) -> list[str]:
        if not query_text and not last_query_state:
            return parts

        keywords = self._build_keywords(query_text=query_text, last_query_state=last_query_state)
        if not keywords:
            return parts

        scored_parts = []
        for part in parts:
            lowered = part.lower()
            score = sum(1 for kw in keywords if kw and kw in lowered)
            scored_parts.append((score, len(part), part))

        matched = [part for score, _, part in sorted(scored_parts, key=lambda item: (item[0], item[1]), reverse=True) if score > 0]
        if matched:
            return matched[:_MAX_SELECTED_PARTS]

        # 没有命中时保守回退，避免完全丢失上下文
        return parts[:2]

    @staticmethod
    def _build_keywords(
        *,
        query_text: str | None = None,
        last_query_state: Optional[Dict[str, object]] = None,
    ) -> list[str]:
        raw_tokens: list[str] = []
        if query_text:
            normalized_query = query_text.lower()
            raw_tokens.extend(re.findall(r"[a-z0-9_]+", normalized_query))
            cjk_chunks = re.findall(r"[\u4e00-\u9fff]+", normalized_query)
            for chunk in cjk_chunks:
                raw_tokens.append(chunk)
                if len(chunk) <= 2:
                    continue
                for idx in range(len(chunk) - 1):
                    raw_tokens.append(chunk[idx : idx + 2])

        if last_query_state:
            for key in ("tables", "metrics"):
                values_state = last_query_state.get(key)
                if isinstance(values_state, list):
                    raw_tokens.extend(str(value).lower() for value in values_state if value)
            for key in ("group_by", "detail_columns"):
                values_state = last_query_state.get(key)
                if isinstance(values_state, list):
                    raw_tokens.extend(str(value).lower() for value in values_state if value)

        seen = set()
        keywords = []
        for token in raw_tokens:
            if len(token) < 2:
                continue
            if token in seen:
                continue
            seen.add(token)
            keywords.append(token)
        return keywords

    def _is_cache_stale(self, cache_key: Tuple[int, ...]) -> bool:
        """检查相关目录的文件是否有更新"""
        project_id = cache_key[0]
        current_mtime = self._latest_mtime(project_id)
        cached_mtime = self._cache_mtime.get(cache_key, 0.0)
        return current_mtime > cached_mtime

    def _latest_mtime(self, project_id: Optional[int] = None) -> float:
        """获取相关目录下所有 .md 文件的最新修改时间"""
        latest = 0.0
        dirs = [self.data_path / "_global"]
        if project_id is not None:
            dirs.append(self.data_path / f"project_{project_id}")

        for d in dirs:
            try:
                for f in d.glob("*.md"):
                    latest = max(latest, f.stat().st_mtime)
            except OSError:
                pass
        return latest

    @staticmethod
    def _truncate(
        text: str,
        max_lines: int = _MAX_MEMORY_LINES,
        max_bytes: int = _MAX_MEMORY_BYTES,
    ) -> str:
        """截断保护，避免注入内容占用过多 token"""
        lines = text.split("\n")
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines])
        if len(text.encode("utf-8")) > max_bytes:
            cut = text.rfind("\n", 0, max_bytes)
            text = text[:cut] if cut > 0 else text[:max_bytes]
        return text
