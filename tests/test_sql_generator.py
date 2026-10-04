"""SQL 生成器纯函数测试：window/order 文本解析与降级"""
from __future__ import annotations

from service.sql_generator import parse_order_text, parse_window_text, time_range_to_ch_expr


class TestParseWindowText:
    def test_per_group_ranking(self):
        w = parse_window_text("每个地区前3")
        assert w == {"group_text": "地区", "limit": 3, "raw": "每个地区前3"}

    def test_ge_variant(self):
        w = parse_window_text("各品类前10")
        assert w["group_text"] == "品类"
        assert w["limit"] == 10

    def test_direction_word_demotes_to_none(self):
        """'各品类销售额最高的前5' 是全局 TopN，不是分组排名 → None 交由 order 路径"""
        assert parse_window_text("各品类销售额最高的前5") is None

    def test_plain_text(self):
        assert parse_window_text("随便一句话") is None


class TestParseOrderText:
    def test_direction_desc(self):
        o = parse_order_text("销售额最高的前5")
        assert o["metric_text"] == "销售额"
        assert o["limit"] == 5
        assert o["direction"] == "DESC"

    def test_direction_asc(self):
        o = parse_order_text("客单价最低的3个")
        assert o["metric_text"] == "客单价"
        assert o["direction"] == "ASC"

    def test_bare_topn_fallback(self):
        o = parse_order_text("销售额前5")
        assert o["metric_text"] == "销售额"
        assert o["limit"] == 5
        assert o["direction"] == "DESC"

    def test_prefix_quantifier_stripped(self):
        """'各品类销售额最高的前5' → 指标文本剥掉前缀量词'各'"""
        o = parse_order_text("各品类销售额最高的前5")
        assert o["metric_text"] == "品类销售额"
        assert o["limit"] == 5


class TestTimeRangeToChExpr:
    def test_last_n_days(self):
        assert time_range_to_ch_expr({"type": "last_n_days", "n": 7}, "orders.created_at") == \
            "orders.created_at >= now() - INTERVAL 7 DAY"

    def test_yesterday_half_open(self):
        assert time_range_to_ch_expr({"type": "yesterday", "n": 1}, "t") == \
            "t >= today() - 1 AND t < today()"

    def test_this_month(self):
        assert "toStartOfMonth" in time_range_to_ch_expr({"type": "this_month", "n": 30}, "t")

    def test_no_time_column_returns_empty(self):
        assert time_range_to_ch_expr({"type": "last_n_days", "n": 7}, "") == ""


class TestEnglishWindowAndOrder:
    def test_window_per_group(self):
        w = parse_window_text("top 3 per region")
        assert w["group_text"] == "region"
        assert w["limit"] == 3

    def test_window_each_group_first(self):
        w = parse_window_text("each category top 5")
        assert w["group_text"] == "category"
        assert w["limit"] == 5

    def test_window_no_match(self):
        assert parse_window_text("whatever") is None

    def test_order_top_n_by_metric(self):
        o = parse_order_text("top 5 by average order value")
        assert o["metric_text"] == "average order value"
        assert o["limit"] == 5
        assert o["direction"] == "DESC"

    def test_order_bottom_n(self):
        o = parse_order_text("bottom 3 by refund rate")
        assert o["direction"] == "ASC"
        assert o["limit"] == 3

    def test_order_lowest_without_limit(self):
        o = parse_order_text("lowest average rating")
        assert o["direction"] == "ASC"
        assert o["limit"] is None
        assert o["metric_text"] == "average rating"


class TestEnglishVerbatimSentences:
    """LLM 常把整句原话放进 window/order_extractions（名词短语夹在 N 与介词之间）"""

    def test_window_with_noun_phrase(self):
        w = parse_window_text("Top 3 categories in each region")
        assert w["group_text"] == "region"
        assert w["limit"] == 3

    def test_window_top_n_by_group(self):
        w = parse_window_text("top 3 by region")
        assert w["group_text"] == "region"

    def test_order_with_noun_phrase(self):
        o = parse_order_text("Top 5 product categories by revenue")
        assert o["metric_text"] == "revenue"
        assert o["limit"] == 5
        assert o["direction"] == "DESC"

    def test_order_bottom_with_noun_phrase(self):
        o = parse_order_text("Bottom 3 channels by refund rate")
        assert o["metric_text"] == "refund rate"
        assert o["direction"] == "ASC"


class TestWindowRepairLoop:
    """LLM 把分组内 top-N 写成全局 LIMIT n → AST 校验拒绝 → 错误回灌后修正为 LIMIT n BY"""

    def test_plain_limit_is_repaired_to_limit_by(self):
        import asyncio
        from unittest import mock

        from matcher.schema_loader import load_sql_schema
        from service import sql_generator
        from service.sql_ast_analyzer import build_analysis_context

        head = ("SELECT users.region AS region, sum(orders.amount) AS revenue "
                "FROM orders JOIN users ON orders.user_id = users.id "
                "WHERE orders.created_at >= now() - INTERVAL 7 DAY "
                "GROUP BY users.region ORDER BY revenue DESC ")
        intent = {
            "base_table": "orders",
            "metrics": [{"id": "revenue", "expr": "sum(orders.amount)"}],
            "group_by": ["users.region"], "filters": [],
            "time_expr": "orders.created_at >= now() - INTERVAL 7 DAY",
            "joins": [{"left": "orders", "right": "users", "condition": "orders.user_id = users.id"}],
            "window": {"group_by": "users.region", "limit": 3},
        }
        replies = iter([head + "LIMIT 3", head + "LIMIT 3 BY users.region"])
        seen_repair_prompts = []

        def fake_llm(messages):
            seen_repair_prompts.append(messages[0]["content"])
            return next(replies)

        ctx = build_analysis_context(load_sql_schema("catalog"))
        with mock.patch.object(sql_generator, "_call_llm", side_effect=fake_llm):
            sql, explain = asyncio.run(sql_generator.generate_sql(
                "Top 3 per region", intent, "schema", ["orders", "users"], analysis_context=ctx))

        assert sql.endswith("LIMIT 3 BY users.region")
        assert explain["repaired"] is True
        assert explain["rounds"][0]["errors"][0].startswith("missing_limit_by")
        assert "missing_limit_by" in seen_repair_prompts[1]   # 错误确实回灌给了第二轮
