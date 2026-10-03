"""MemoryWriter — LLM 驱动记忆写入测试"""
import json
from unittest import mock

import pytest


class TestMemoryWriter:
    """memory/memory_writer.py"""

    def test_ensure_index_creates_memory_md(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        writer._ensure_index(project_dir)
        index = project_dir / "MEMORY.md"
        assert index.exists()
        assert "auto_learned.md" in index.read_text()

    def test_ensure_index_appends_to_existing(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)
        (project_dir / "MEMORY.md").write_text("- [Manual](manual.md)\n")

        writer._ensure_index(project_dir)
        content = (project_dir / "MEMORY.md").read_text()
        assert "manual.md" in content
        assert "auto_learned.md" in content

    def test_ensure_index_idempotent(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        writer._ensure_index(project_dir)
        writer._ensure_index(project_dir)
        content = (project_dir / "MEMORY.md").read_text()
        assert content.count("auto_learned.md") == 1

    def test_append_memory_creates_file(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        writer._append_memory(project_dir, "correction", "purchase 应匹配 payment_submit")
        auto = project_dir / "auto_learned.md"
        assert auto.exists()
        assert "purchase 应匹配 payment_submit" in auto.read_text()
        assert "[correction]" in auto.read_text()

    def test_append_memory_appends(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        writer._append_memory(project_dir, "correction", "第一条")
        writer._append_memory(project_dir, "constraint", "第二条")

        lines = (project_dir / "auto_learned.md").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_is_duplicate_detects_similar(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        writer._append_memory(project_dir, "correction", "purchase 应匹配 payment_submit")
        assert writer._is_duplicate(project_dir, "purchase 应匹配 payment_submit")
        assert not writer._is_duplicate(project_dir, "完全不同的内容")

    def test_is_duplicate_no_file(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)
        assert not writer._is_duplicate(project_dir, "anything")

    def test_trim_limits_lines(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        # 写入超过限制的行数
        for i in range(40):
            writer._append_memory(project_dir, "test", f"记忆 {i}")

        lines = (project_dir / "auto_learned.md").read_text().strip().split("\n")
        assert len(lines) <= 30
        # 最新的保留
        assert "记忆 39" in lines[-1]

    def test_read_existing(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True)

        assert writer._read_existing(project_dir) == ""
        writer._append_memory(project_dir, "test", "内容")
        assert "内容" in writer._read_existing(project_dir)

    @pytest.mark.asyncio
    async def test_maybe_save_no_save_when_llm_says_no(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))

        # mock _judge 返回 should_save=false
        writer._judge = mock.AsyncMock(return_value={"should_save": False})

        await writer.maybe_save(
            project_id=55,
            user_query="德国的PV",
            extraction={"event": "app_launch"},
            resolver_explain={"event": {"score": 100}},
            current_state={"event": "app_launch"},
        )

        auto = tmp_path / "memory" / "project_55" / "auto_learned.md"
        assert not auto.exists()

    @pytest.mark.asyncio
    async def test_maybe_save_skips_user_scoped_preference(self, tmp_path):
        """个人偏好不属于项目知识，不能写进 project memory 注入给其他用户"""
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))
        writer._judge = mock.AsyncMock(return_value={
            "should_save": True,
            "category": "preference",
            "content": "用户偏好查询UV而非PV",
        })

        await writer.maybe_save(
            project_id=55,
            user_query="我要看UV",
            extraction={"metric": "uv"},
            resolver_explain={"metric": {"score": 100, "value": "uv"}},
            current_state={"metric": "uv"},
        )

        assert not (tmp_path / "memory" / "project_55" / "auto_learned.md").exists()

    @pytest.mark.asyncio
    async def test_maybe_save_writes_when_llm_says_yes(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))

        writer._judge = mock.AsyncMock(return_value={
            "should_save": True,
            "category": "constraint",
            "content": "本项目的'交易表'对应 orders 表",
        })

        await writer.maybe_save(
            project_id=55,
            user_query="交易表的销售额",
            extraction={"table": "交易表"},
            resolver_explain={"table": {"score": 0, "value": "orders"}},
            current_state={"tables": ["orders"]},
        )

        auto = tmp_path / "memory" / "project_55" / "auto_learned.md"
        assert auto.exists()
        content = auto.read_text()
        assert "本项目的'交易表'对应 orders 表" in content
        assert "[constraint]" in content

        # 索引文件也应被创建
        index = tmp_path / "memory" / "project_55" / "MEMORY.md"
        assert index.exists()
        assert "auto_learned.md" in index.read_text()

    @pytest.mark.asyncio
    async def test_maybe_save_skips_duplicate(self, tmp_path):
        from memory.memory_writer import MemoryWriter
        writer = MemoryWriter(data_path=str(tmp_path / "memory"))

        # 先写入一条
        writer._judge = mock.AsyncMock(return_value={
            "should_save": True,
            "category": "correction",
            "content": "purchase 应匹配 payment_submit",
        })
        await writer.maybe_save(55, "purchase", {}, {}, {})

        # 再次返回相同内容 → 应被去重跳过
        await writer.maybe_save(55, "purchase", {}, {}, {})

        auto = tmp_path / "memory" / "project_55" / "auto_learned.md"
        lines = [l for l in auto.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 1  # 不重复写入
