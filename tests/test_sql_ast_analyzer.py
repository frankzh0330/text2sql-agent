"""SQL AST 后置分析器测试（纯确定性，不依赖 LLM）"""
from __future__ import annotations

import pytest

from matcher.schema_loader import load_sql_schema
from service.sql_ast_analyzer import analyze_sql, build_analysis_context


@pytest.fixture(scope="module")
def ctx():
    return build_analysis_context(load_sql_schema("catalog"))


GOOD_INTENT = {
    "base_table": "orders",
    "metrics": [{"id": "revenue", "expr": "sum(orders.amount)"}],
    "detail_columns": [],
    "group_by": ["users.region"],
    "filters": [{"column": "users.vip_level", "op": "=", "value": "vip"}],
    "time_expr": "orders.created_at >= now() - INTERVAL 7 DAY",
    "joins": [{"left": "orders", "right": "users", "condition": "orders.user_id = users.id"}],
    "window": None,
    "order_by": None,
}

GOOD_SQL = """
SELECT users.region AS region, sum(orders.amount) AS revenue
FROM orders JOIN users ON orders.user_id = users.id
WHERE orders.created_at >= now() - INTERVAL 7 DAY AND users.vip_level = 'vip'
GROUP BY users.region ORDER BY revenue DESC LIMIT 100
"""


class TestHappyPath:
    def test_good_sql_passes(self, ctx):
        result = analyze_sql(GOOD_SQL, ctx, GOOD_INTENT)
        assert result.errors == []
        assert result.cost["tables_scanned"] == ["orders", "users"]
        assert result.cost["join_count"] == 1
        assert result.cost["estimated_rows_scanned"] == 82_000_000

    def test_large_scan_warning(self, ctx):
        """orders(80M)+users(2M) 超过 10M 阈值 → 大扫描警告"""
        result = analyze_sql(GOOD_SQL, ctx, GOOD_INTENT)
        codes = {w["code"] for w in result.warnings}
        assert "large_scan" in codes

    def test_alias_sql_passes(self, ctx):
        """别名（o/u）经解析后实体保真仍然通过"""
        sql = """
        SELECT u.region, sum(o.amount) AS revenue
        FROM orders o JOIN users u ON o.user_id = u.id
        WHERE o.created_at >= now() - INTERVAL 7 DAY AND u.vip_level = 'vip'
        GROUP BY u.region LIMIT 100
        """
        result = analyze_sql(sql, ctx, GOOD_INTENT)
        assert result.errors == []


class TestStructuralErrors:
    def test_unknown_column(self, ctx):
        sql = "SELECT orders.foo FROM orders LIMIT 10"
        result = analyze_sql(sql, ctx, {"base_table": "orders", "metrics": [], "filters": [], "joins": []})
        assert any("unknown_column" in e and "orders.foo" in e for e in result.errors)

    def test_cartesian_join(self, ctx):
        sql = "SELECT count() FROM orders JOIN users LIMIT 10"
        intent = {"base_table": "orders", "metrics": [{"id": "order_count", "expr": "count()"}],
                  "filters": [], "joins": []}
        result = analyze_sql(sql, ctx, intent)
        assert any("cartesian_join" in e for e in result.errors)

    def test_undeclared_join_edge(self, ctx):
        """products ↔ users 之间没有声明 join"""
        sql = ("SELECT count() FROM orders "
               "JOIN products ON orders.product_id = products.id "
               "JOIN users ON products.id = users.region LIMIT 10")
        intent = {"base_table": "orders", "metrics": [{"id": "order_count", "expr": "count()"}],
                  "filters": [],
                  "joins": [{"left": "orders", "right": "products",
                              "condition": "orders.product_id = products.id"}]}
        result = analyze_sql(sql, ctx, intent)
        assert any("undeclared_join_edge" in e for e in result.errors)

    def test_join_key_mismatch(self, ctx):
        """orders↔products 边存在，但 ON 用了 user_id（声明是 product_id）"""
        sql = ("SELECT count() FROM orders "
               "JOIN products ON orders.user_id = products.id LIMIT 10")
        intent = {"base_table": "orders", "metrics": [{"id": "order_count", "expr": "count()"}],
                  "filters": [],
                  "joins": [{"left": "orders", "right": "products",
                              "condition": "orders.product_id = products.id"}]}
        result = analyze_sql(sql, ctx, intent)
        assert any("join_key_mismatch" in e for e in result.errors)


class TestEntityFidelity:
    def test_missing_table(self, ctx):
        intent = dict(GOOD_INTENT)
        sql = "SELECT sum(orders.amount) FROM orders WHERE users.vip_level = 'vip' LIMIT 10"
        result = analyze_sql(sql, ctx, intent)
        assert any("missing_table" in e and "users" in e for e in result.errors)

    def test_missing_metric_expr(self, ctx):
        """意图要求 revenue=sum(orders.amount)，SQL 只用了 count()"""
        sql = "SELECT users.region, count() FROM orders JOIN users ON orders.user_id = users.id WHERE orders.created_at >= now() - INTERVAL 7 DAY AND users.vip_level = 'vip' GROUP BY users.region LIMIT 10"
        result = analyze_sql(sql, ctx, GOOD_INTENT)
        assert any("missing_metric_expr" in e for e in result.errors)

    def test_missing_filter(self, ctx):
        """意图带 VIP 过滤，SQL 缺失该谓词"""
        sql = ("SELECT users.region, sum(orders.amount) FROM orders "
               "JOIN users ON orders.user_id = users.id "
               "WHERE orders.created_at >= now() - INTERVAL 7 DAY "
               "GROUP BY users.region LIMIT 100")
        result = analyze_sql(sql, ctx, GOOD_INTENT)
        assert any("missing_filter" in e and "vip_level" in e for e in result.errors)

    def test_numeric_filter_value_unquoted_match(self, ctx):
        """数值过滤值不带引号也能对上（'1000' vs 1000）"""
        intent = {
            "base_table": "orders",
            "metrics": [{"id": "order_count", "expr": "count()"}],
            "filters": [{"column": "orders.amount", "op": ">", "value": "1000"}],
            "joins": [], "time_expr": "",
        }
        sql = "SELECT count() FROM orders WHERE amount > 1000 LIMIT 10"
        result = analyze_sql(sql, ctx, intent)
        assert not any("missing_filter" in e for e in result.errors)


class TestWarnings:
    def test_full_scan_on_fact_table_without_time_filter(self, ctx):
        """80M 的 orders 没有时间过滤 → full_scan 警告"""
        intent = {"base_table": "orders", "metrics": [{"id": "order_count", "expr": "count()"}],
                  "filters": [], "joins": [], "time_expr": "orders.created_at >= now() - INTERVAL 7 DAY"}
        sql = "SELECT count() FROM orders LIMIT 10"
        result = analyze_sql(sql, ctx, intent)
        codes = {w["code"] for w in result.warnings}
        assert "full_scan_on_fact_table" in codes
        assert "missing_time_filter" in codes

    def test_non_grouped_column_warning(self, ctx):
        sql = ("SELECT users.region, orders.channel, sum(orders.amount) FROM orders "
               "JOIN users ON orders.user_id = users.id "
               "WHERE orders.created_at >= now() - INTERVAL 7 DAY AND users.vip_level = 'vip' "
               "GROUP BY users.region LIMIT 100")
        result = analyze_sql(sql, ctx, GOOD_INTENT)
        assert any(w["code"] == "non_grouped_column" for w in result.warnings)


class TestContext:
    def test_build_context_from_real_schema(self, ctx):
        assert ctx is not None
        assert "orders" in ctx.tables_columns
        assert "created_at" in ctx.tables_columns["orders"]
        assert frozenset({"orders", "users"}) in ctx.join_pairs
        assert ctx.time_columns["orders"] == "created_at"
        assert ctx.est_rows["orders"] == 80_000_000

    def test_build_context_tolerates_bad_schema(self):
        """mock/异常 schema → None（分析跳过，不炸主流程）"""
        from service.sql_ast_analyzer import build_analysis_context

        class Bad:
            tables = None

        assert build_analysis_context(Bad()) is None


WINDOW_INTENT = {**GOOD_INTENT, "window": {"group_by": "users.region", "limit": 3}}
WINDOW_SQL_HEAD = """
SELECT users.region AS region, sum(orders.amount) AS revenue
FROM orders JOIN users ON orders.user_id = users.id
WHERE orders.created_at >= now() - INTERVAL 7 DAY AND users.vip_level = 'vip'
GROUP BY users.region ORDER BY revenue DESC
"""


class TestWindowLimitBy:
    """分组内 top-N 必须是 LIMIT n BY <分组列>，只写 LIMIT n 会变成全局 top-N"""

    @pytest.mark.parametrize("tail", [
        "LIMIT 3 BY users.region",
        "LIMIT 3 BY region",          # SELECT 别名
    ])
    def test_limit_by_group_passes(self, ctx, tail):
        assert analyze_sql(WINDOW_SQL_HEAD + tail, ctx, WINDOW_INTENT).errors == []

    def test_plain_limit_is_rejected(self, ctx):
        errors = analyze_sql(WINDOW_SQL_HEAD + "LIMIT 3", ctx, WINDOW_INTENT).errors
        assert len(errors) == 1 and errors[0].startswith("missing_limit_by")
        assert "LIMIT 3 BY users.region" in errors[0]

    @pytest.mark.parametrize("tail", ["LIMIT 5 BY users.region", "LIMIT 3 BY users.city"])
    def test_wrong_n_or_group_is_rejected(self, ctx, tail):
        errors = analyze_sql(WINDOW_SQL_HEAD + tail, ctx, WINDOW_INTENT).errors
        assert len(errors) == 1 and errors[0].startswith("window_limit_mismatch")

    def test_no_window_intent_allows_plain_limit(self, ctx):
        assert analyze_sql(WINDOW_SQL_HEAD + "LIMIT 3", ctx, GOOD_INTENT).errors == []
