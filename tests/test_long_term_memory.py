"""LongTermMemory 记忆注入测试"""
import time


class TestLongTermMemory:
    """memory/long_term_memory.py"""

    def _write_memory_index(self, directory, filename, content):
        """辅助：创建 MEMORY.md 索引和引用的记忆文件"""
        directory.mkdir(parents=True, exist_ok=True)
        memory_file = directory / filename
        memory_file.write_text(content, encoding="utf-8")
        index = directory / "MEMORY.md"
        index.write_text(f"- [Test]({filename})\n", encoding="utf-8")

    def test_no_memory_dir_returns_empty(self, long_term_memory, tmp_path):
        # data_path 已被 fixture 创建（mkdir parents=True），但里面没有内容
        result = long_term_memory.load_memory_context(project_id=55)
        assert result == ""

    def test_empty_global_dir_returns_empty(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        global_dir.mkdir(parents=True)
        # 没有 MEMORY.md
        result = long_term_memory.load_memory_context()
        assert result == ""

    def test_global_memory_loaded(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        self._write_memory_index(global_dir, "corrections.md", "Always use ROW for global queries")

        result = long_term_memory.load_memory_context()
        assert "Always use ROW for global queries" in result

    def test_project_memory_loaded(self, long_term_memory, tmp_path):
        # 先创建 global
        global_dir = tmp_path / "memory" / "_global"
        self._write_memory_index(global_dir, "common.md", "Global rule")

        # 再创建 project_55
        project_dir = tmp_path / "memory" / "project_55"
        self._write_memory_index(project_dir, "eu.md", "EU metrics only")

        result = long_term_memory.load_memory_context(project_id=55)
        assert "Global rule" in result
        assert "EU metrics only" in result

    def test_project_memory_not_loaded_for_wrong_id(self, long_term_memory, tmp_path):
        project_dir = tmp_path / "memory" / "project_55"
        self._write_memory_index(project_dir, "eu.md", "EU metrics only")

        result = long_term_memory.load_memory_context(project_id=60)
        assert "EU metrics only" not in result

    def test_no_project_id_loads_only_global(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        self._write_memory_index(global_dir, "common.md", "Global rule")

        project_dir = tmp_path / "memory" / "project_55"
        self._write_memory_index(project_dir, "eu.md", "EU metrics only")

        result = long_term_memory.load_memory_context(project_id=None)
        assert "Global rule" in result
        assert "EU metrics only" not in result

    def test_cache_returns_same_result(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        self._write_memory_index(global_dir, "common.md", "Global rule")

        r1 = long_term_memory.load_memory_context()
        r2 = long_term_memory.load_memory_context()
        assert r1 == r2

    def test_cache_invalidated_on_mtime_change(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        self._write_memory_index(global_dir, "common.md", "Version 1")

        r1 = long_term_memory.load_memory_context()
        assert "Version 1" in r1

        # 更新文件内容（mtime 变化）
        (global_dir / "common.md").write_text("Version 2", encoding="utf-8")

        r2 = long_term_memory.load_memory_context()
        assert "Version 2" in r2
        assert "Version 1" not in r2

    def test_truncate_at_max_lines(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        long_content = "\n".join([f"Line {i}" for i in range(100)])
        self._write_memory_index(global_dir, "big.md", long_content)

        result = long_term_memory.load_memory_context()
        # _MAX_MEMORY_LINES = 50
        assert len(result.split("\n")) <= 50

    def test_frontmatter_stripped(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        content = "---\nname: test\ntype: feedback\n---\nActual content here"
        self._write_memory_index(global_dir, "note.md", content)

        result = long_term_memory.load_memory_context()
        assert "name: test" not in result
        assert "Actual content here" in result

    def test_missing_linked_file_skipped(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        global_dir.mkdir(parents=True, exist_ok=True)
        # MEMORY.md 引用不存在的文件
        (global_dir / "MEMORY.md").write_text("- [Missing](nonexistent.md)\n", encoding="utf-8")

        result = long_term_memory.load_memory_context()
        # 不崩溃，返回空
        assert result == ""

    def test_selects_relevant_memory_parts_for_query(self, long_term_memory, tmp_path):
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "activation.md").write_text("激活默认映射 activation_success", encoding="utf-8")
        (project_dir / "payment.md").write_text("支付成功默认看 payment_success", encoding="utf-8")
        (project_dir / "MEMORY.md").write_text(
            "- [Activation](activation.md)\n- [Payment](payment.md)\n",
            encoding="utf-8",
        )

        result = long_term_memory.load_memory_context(project_id=55, query_text="看激活数据")
        assert "activation_success" in result
        assert "payment_success" not in result

    def test_selection_is_per_query_not_cached(self, long_term_memory, tmp_path):
        """缓存只缓存磁盘内容；第二条 query 必须按自己的关键词重新选择"""
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "activation.md").write_text("激活默认映射 activation_success", encoding="utf-8")
        (project_dir / "payment.md").write_text("支付成功默认看 payment_success", encoding="utf-8")
        (project_dir / "MEMORY.md").write_text(
            "- [Activation](activation.md)\n- [Payment](payment.md)\n",
            encoding="utf-8",
        )

        r1 = long_term_memory.load_memory_context(project_id=55, query_text="看激活数据")
        r2 = long_term_memory.load_memory_context(project_id=55, query_text="看支付成功")
        assert "activation_success" in r1 and "payment_success" not in r1
        assert "payment_success" in r2 and "activation_success" not in r2

    def test_list_file_selected_per_entry(self, long_term_memory, tmp_path):
        """auto_learned.md 这类列表文件按条目选择，而不是整文件注入"""
        project_dir = tmp_path / "memory" / "project_55"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "auto_learned.md").write_text(
            "- [constraint] 交易表 对应 orders 表\n"
            "- [correction] 退款率 指 refund_rate 而不是 cancel_rate\n",
            encoding="utf-8",
        )
        (project_dir / "MEMORY.md").write_text("- [Auto Learned](auto_learned.md)\n", encoding="utf-8")

        result = long_term_memory.load_memory_context(project_id=55, query_text="交易表的销售额")
        assert "orders" in result
        assert "refund_rate" not in result

    def test_prose_file_kept_whole(self, long_term_memory, tmp_path):
        global_dir = tmp_path / "memory" / "_global"
        self._write_memory_index(global_dir, "rules.md", "# Rules\n- revenue 排除已取消订单\n- 时间默认近 7 天")

        result = long_term_memory.load_memory_context(query_text="revenue")
        assert "# Rules" in result
        assert "时间默认近 7 天" in result
