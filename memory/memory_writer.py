"""LLM 驱动的自动记忆写入

在查询成功后，用一次轻量 LLM 调用判断本轮是否有值得记住的信息。
如果有，追加到 project 的 auto_learned.md 文件中。

设计：
- 异步非阻塞，不影晌主流程响应速度
- 追加到一个文件（auto_learned.md），FIFO 淘汰
- 写入前检查去重
- 自动维护 MEMORY.md 索引
- 只写 project 作用域的知识（correction / constraint）；个人习惯属于 user 作用域，
  由 UserPreferenceStore 负责，不得写入 project memory 污染其他用户
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from memory.judge_prompt import JUDGE_SYSTEM_PROMPT, JUDGE_TOOL_SCHEMA, JUDGE_USER_TEMPLATE

logger = logging.getLogger(__name__)

_MAX_AUTO_LINES = 30  # auto_learned.md 最大行数
_PROJECT_CATEGORIES = frozenset({"correction", "constraint"})


class MemoryWriter:
    """LLM 驱动的记忆写入器"""

    def __init__(self, data_path: str = "data/memory"):
        self.data_path = Path(data_path)
        # 去重 → 追加 → 裁剪 是读改写序列，并发请求下需串行
        self._write_lock = threading.Lock()

    async def maybe_save(
        self,
        project_id: int,
        user_query: str,
        extraction: Dict[str, Any],
        resolver_explain: Dict[str, Any],
        current_state: Dict[str, Any],
        prev_state: Optional[Dict[str, Any]] = None,
        confirmed_selection: Optional[Dict[str, str]] = None,
    ) -> None:
        """LLM 判断是否写入记忆，是则追加到 auto_learned.md

        非阻塞：失败只记日志，不影响主流程。
        """
        try:
            project_dir = self.data_path / f"project_{project_id}"
            project_dir.mkdir(parents=True, exist_ok=True)

            # 读取已有记忆（用于去重 + 给 LLM 参考）
            existing = self._read_existing(project_dir)
            logger.debug(f"MemoryWriter: project={project_id}, existing_len={len(existing)}, query={user_query[:60]}")

            # 如果有确认选择，注入到 resolver_explain
            explain = dict(resolver_explain)
            if confirmed_selection:
                explain["user_confirmed"] = confirmed_selection

            # 调用 LLM 判断
            logger.debug(f"MemoryWriter: calling judge for project={project_id}")
            result = await self._judge(
                user_query=user_query,
                extraction=extraction,
                resolver_explain=explain,
                current_state=current_state,
                prev_state=prev_state,
                existing_memory=existing,
            )

            if not result or not result.get("should_save"):
                logger.debug("Memory judge: nothing to save")
                return

            content = result.get("content", "").strip()
            category = result.get("category", "correction")

            if not content:
                return
            if category not in _PROJECT_CATEGORIES:
                logger.debug(f"Memory judge: category={category} is not project-scoped, skip: {content[:50]}")
                return

            with self._write_lock:
                if self._is_duplicate(project_dir, content):
                    logger.debug(f"Memory judge: duplicate, skip: {content[:50]}")
                    return
                self._append_memory(project_dir, category, content)
            logger.info(f"Memory saved [{category}]: {content[:80]}")

        except Exception as e:
            logger.warning(f"Memory writer error (non-critical): {e}")

    async def _judge(
        self,
        user_query: str,
        extraction: Dict[str, Any],
        resolver_explain: Dict[str, Any],
        current_state: Dict[str, Any],
        prev_state: Optional[Dict[str, Any]],
        existing_memory: str,
    ) -> Optional[Dict[str, Any]]:
        """调用 LLM 判断"""
        from service.llm_extractions import get_llm_client, _get_model_name, _supports_tool_calling, _extract_json

        user_content = JUDGE_USER_TEMPLATE.format(
            user_query=user_query,
            extraction=json.dumps(extraction, ensure_ascii=False),
            resolver_explain=json.dumps(resolver_explain, ensure_ascii=False),
            current_state=json.dumps(current_state, ensure_ascii=False),
            prev_state=json.dumps(prev_state, ensure_ascii=False) if prev_state else "无（首次查询）",
            existing_memory=existing_memory if existing_memory else "无",
        )

        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        client = get_llm_client()
        model = _get_model_name()
        use_tool = _supports_tool_calling()

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }

        if use_tool:
            kwargs["tools"] = [JUDGE_TOOL_SCHEMA]
            kwargs["tool_choice"] = {"type": "function", "function": {"name": "judge_memory"}}

        # 异步执行（to_thread 避免阻塞事件循环）
        resp = await asyncio.to_thread(
            client.chat.completions.create, **kwargs
        )

        choice = resp.choices[0]
        message = choice.message

        if use_tool and message.tool_calls:
            return json.loads(message.tool_calls[0].function.arguments)
        else:
            content = message.content if hasattr(message, "content") else str(message)
            content = _extract_json(content)
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                logger.warning(f"Memory judge returned invalid JSON: {content[:200]}")
                return None

    def _append_memory(self, project_dir: Path, category: str, content: str) -> None:
        """追加一条记忆到 auto_learned.md"""
        auto_file = project_dir / "auto_learned.md"

        # 确保索引文件包含 auto_learned.md
        self._ensure_index(project_dir)

        # 追加一行
        line = f"- [{category}] {content}\n"
        if auto_file.exists():
            with open(auto_file, "a", encoding="utf-8") as f:
                f.write(line)
        else:
            auto_file.write_text(line, encoding="utf-8")

        # FIFO 淘汰：超过行数限制时裁剪
        self._trim_if_needed(auto_file)

    def _ensure_index(self, project_dir: Path) -> None:
        """确保 MEMORY.md 索引包含 auto_learned.md"""
        index_file = project_dir / "MEMORY.md"
        auto_entry = "- [Auto Learned](auto_learned.md)\n"

        if not index_file.exists():
            index_file.write_text(auto_entry, encoding="utf-8")
            return

        content = index_file.read_text(encoding="utf-8")
        if "auto_learned.md" not in content:
            # 追加到末尾
            if not content.endswith("\n"):
                content += "\n"
            content += auto_entry
            index_file.write_text(content, encoding="utf-8")

    def _trim_if_needed(self, auto_file: Path) -> None:
        """超过 _MAX_AUTO_LINES 时保留最新的"""
        lines = auto_file.read_text(encoding="utf-8").strip().split("\n")
        if len(lines) > _MAX_AUTO_LINES:
            trimmed = "\n".join(lines[-_MAX_AUTO_LINES:]) + "\n"
            auto_file.write_text(trimmed, encoding="utf-8")

    def _is_duplicate(self, project_dir: Path, content: str) -> bool:
        """检查是否已有相似记忆（hash + 归一化全文去重）"""
        auto_file = project_dir / "auto_learned.md"
        if not auto_file.exists():
            return False

        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        existing = auto_file.read_text(encoding="utf-8")

        # 逐行 hash 去重
        for line in existing.splitlines():
            if hashlib.md5(line.strip().encode("utf-8")).hexdigest() == content_hash:
                return True

        # 归一化全文包含检查
        normalized_existing = re.sub(r"[^\w\u4e00-\u9fff]", "", existing)
        normalized_content = re.sub(r"[^\w\u4e00-\u9fff]", "", content)
        return normalized_content in normalized_existing

    def _read_existing(self, project_dir: Path) -> str:
        """读取已有的 auto_learned.md 内容"""
        auto_file = project_dir / "auto_learned.md"
        if not auto_file.exists():
            return ""
        try:
            return auto_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
