from __future__ import annotations

import pytest

from common.text_utils import (
    contains_chinese,
    is_single_chinese_char,
    normalize,
    tokenize_mixed,
)
from matcher.entity_matcher import EntityMatcher, LexicalRetriever, MatchResult, recall_tokens
from matcher.schema_loader import load_sql_schema
from matcher.time_matcher import TimeMatcher
from matcher.matcher_service import MatcherService
from common.types import MatcherType

# =========================
# 测试数据
# =========================

MOCK_TABLES = {
    "orders": {
        "aliases": ["订单", "订单表", "下单", "order record"],
        "time_column": "created_at",
        "columns": {},
    },
    "users": {
        "aliases": ["用户", "用户表", "会员"],
        "columns": {},
    },
}

MOCK_COLUMNS = {
    "users.region": {
        "table": "users",
        "column": "region",
        "type": "LowCardinality(String)",
        "aliases": ["region", "地区", "区域"],
    },
    "users.vip_level": {
        "table": "users",
        "column": "vip_level",
        "type": "LowCardinality(String)",
        "aliases": ["会员等级", "VIP等级", "等级"],
    },
    "orders.amount": {
        "table": "orders",
        "column": "amount",
        "type": "Float64",
        "aliases": ["金额", "订单金额"],
    },
}

MOCK_METRICS = {
    "revenue": {
        "aliases": ["销售额", "营收", "GMV"],
        "expr": "sum(orders.amount)",
    },
    "order_count": {
        "aliases": ["订单量", "订单数", "单量"],
        "expr": "count()",
    },
}


# =========================
# text_utils 测试
# =========================

class TestTextUtils:
    """测试文本工具函数"""

    def test_normalize_lowercase(self):
        """测试小写转换"""
        assert normalize("APP_LAUNCH") == "app launch"

    def test_normalize_camel_case(self):
        """测试 camelCase 处理"""
        assert normalize("appLaunch") == "app launch"

    def test_normalize_underscore(self):
        """测试下划线处理"""
        assert normalize("app_launch") == "app launch"

    def test_normalize_special_chars(self):
        """测试特殊字符处理"""
        assert normalize("app@launch#test") == "app launch test"

    def test_tokenize_mixed_english(self):
        """测试英文分词"""
        tokens = tokenize_mixed("app launch test")
        assert "app" in tokens
        assert "launch" in tokens
        assert "test" in tokens

    def test_tokenize_mixed_camel_case(self):
        """camelCase 在 lower 之前拆开（此前先 lower 会得到 'shippingfee'）"""
        assert tokenize_mixed("shippingFee") == ["shipping", "fee"]

    def test_tokenize_mixed_max_tokens_none_keeps_all(self):
        text = "one two three four five six seven"
        assert len(tokenize_mixed(text)) == 5
        assert tokenize_mixed(text, max_tokens=None) == text.split()

    def test_tokenize_mixed_chinese(self):
        """测试中文分词"""
        tokens = tokenize_mixed("订单表")
        assert len(tokens) > 0

    def test_tokenize_mixed_combined(self):
        """测试中英混合分词"""
        tokens = tokenize_mixed("orders订单")
        assert "orders" in tokens
        assert len(tokens) > 1

    def test_contains_chinese_true(self):
        """测试检测中文 - 包含中文"""
        assert contains_chinese("订单表") is True

    def test_contains_chinese_false(self):
        """测试检测中文 - 不包含中文"""
        assert contains_chinese("app launch") is False

    def test_is_single_chinese_char_true(self):
        """测试单字中文 - 是单字"""
        assert is_single_chinese_char("订") is True

    def test_is_single_chinese_char_false(self):
        """测试单字中文 - 不是单字"""
        assert is_single_chinese_char("订单") is False
        assert is_single_chinese_char("app") is False


# =========================
# EntityMatcher 测试（表）
# =========================

class TestTableEntities:
    """测试表匹配器"""

    @pytest.fixture
    def matcher(self):
        return EntityMatcher(MOCK_TABLES)

    def test_exact_match(self, matcher: EntityMatcher):
        result = matcher.match("orders")
        assert result.matched == "orders"
        assert result.score == 100.0

    def test_alias_match(self, matcher: EntityMatcher):
        result = matcher.match("订单表")
        assert result.matched == "orders"
        assert result.score == 100.0

    def test_fuzzy_match(self, matcher: EntityMatcher):
        """英文 typo：token 召回 + 别名打分（不依赖 jieba 全局词典状态）"""
        result = matcher.match("order recrd")
        assert result.matched == "orders"
        assert 70.0 <= result.score < 100.0

    def test_no_match(self, matcher: EntityMatcher):
        result = matcher.match("xyz123不存在的")
        assert result.score < 100.0


# =========================
# EntityMatcher 测试（列）
# =========================

class TestColumnEntities:
    """测试列匹配器（doc = table.column）"""

    @pytest.fixture
    def matcher(self):
        return EntityMatcher(MOCK_COLUMNS)

    def test_exact_match(self, matcher: EntityMatcher):
        result = matcher.match("region")
        assert result.matched == "users.region"

    def test_chinese_alias_match(self, matcher: EntityMatcher):
        result = matcher.match("地区")
        assert result.matched == "users.region"

    def test_qualified_names_distinguish_tables(self, matcher: EntityMatcher):
        """同名列可区分归属表：金额 → orders.amount 而不是别的表的列"""
        result = matcher.match("金额")
        assert result.matched == "orders.amount"


# =========================
# EntityMatcher 测试（指标）
# =========================

class TestMetricEntities:
    """测试业务指标匹配器"""

    @pytest.fixture
    def matcher(self):
        return EntityMatcher(MOCK_METRICS)

    def test_revenue_match(self, matcher: EntityMatcher):
        result = matcher.match("销售额")
        assert result.matched == "revenue"

    def test_order_count_match(self, matcher: EntityMatcher):
        result = matcher.match("订单量")
        assert result.matched == "order_count"

    def test_english_alias_match(self, matcher: EntityMatcher):
        result = matcher.match("GMV")
        assert result.matched == "revenue"


# =========================
# TimeMatcher 测试
# =========================

class TestTimeMatcher:
    """测试时间匹配器"""

    @pytest.fixture
    def matcher(self):
        return TimeMatcher()

    def test_last_n_days_chinese(self, matcher: TimeMatcher):
        result = matcher.match("近7天")
        assert result.days == 7
        assert result.time_type == "last_n_days"

    def test_last_n_days_english(self, matcher: TimeMatcher):
        result = matcher.match("last 30 days")
        assert result.days == 30
        assert result.time_type == "last_n_days"

    def test_yesterday(self, matcher: TimeMatcher):
        result = matcher.match("昨天")
        assert result.days == 1
        assert result.time_type == "yesterday"

    def test_today(self, matcher: TimeMatcher):
        result = matcher.match("今天")
        assert result.days == 1
        assert result.time_type == "today"

    def test_this_week(self, matcher: TimeMatcher):
        result = matcher.match("本周")
        assert result.days == 7
        assert result.time_type == "this_week"

    def test_default(self, matcher: TimeMatcher):
        result = matcher.match("无效输入")
        assert result.days == 7
        assert result.time_type == "default"


# =========================
# SchemaLoader 测试
# =========================

class TestSchemaLoader:
    """测试 schema YAML 加载（真实 demo catalog）"""

    @pytest.fixture
    def schema(self):
        return load_sql_schema("catalog")

    def test_tables_loaded(self, schema):
        assert "orders" in schema.tables
        assert "users" in schema.tables
        assert "products" in schema.tables

    def test_columns_qualified_names(self, schema):
        assert "users.region" in schema.columns
        assert schema.columns["users.region"]["table"] == "users"

    def test_joins_loaded(self, schema):
        # YAML 1.1 会把裸 on 解析为布尔，condition key 必须可用
        assert len(schema.joins) == 5
        assert schema.joins[0]["condition"] == "orders.user_id = users.id"

    def test_metrics_expr(self, schema):
        assert schema.metrics["revenue"]["expr"] == "sum(orders.amount)"

    def test_find_join(self, schema):
        j = schema.find_join("users", "orders")
        assert j is not None
        assert j["condition"] == "orders.user_id = users.id"

    def test_sources_merged(self, schema):
        """物理 catalog / 语义层 / alias 表三个源合并到同一实体"""
        orders = schema.tables["orders"]
        assert orders["est_rows"] == 80_000_000          # tables.yaml
        assert orders["time_column"] == "created_at"      # metrics.yaml views
        assert orders["aliases"][0] == "orders"           # 规范名由 loader 添加
        assert "purchases" in orders["aliases"]           # aliases.yaml
        assert schema.columns["orders.status"]["enum_values"][0] == "created"


def _write_catalog(tmp_path, aliases_yaml: str, metrics_yaml: str | None = None):
    (tmp_path / "tables.yaml").write_text(
        "tables:\n"
        "  - name: orders\n"
        "    columns:\n"
        "      - {name: amount, type_text: Float64}\n"
    )
    (tmp_path / "metrics.yaml").write_text(metrics_yaml or (
        "metrics:\n"
        "  revenue: {view: orders, sql: 'sum(orders.amount)'}\n"
    ))
    (tmp_path / "aliases.yaml").write_text(aliases_yaml)
    return str(tmp_path)


class TestCatalogAssembly:
    """多源合并：置信度 / 审核状态 / 引用校验"""

    def test_confidence_by_source_and_pending_skipped(self, tmp_path):
        base = _write_catalog(tmp_path, (
            "aliases:\n"
            "  - {entity_id: 'metric:revenue', alias: gmv, source: llm}\n"
            "  - {entity_id: 'metric:revenue', alias: gmv, source: glossary}\n"
            "  - {entity_id: 'metric:revenue', alias: sales, source: query_log, confidence: 0.4}\n"
            "  - {entity_id: 'metric:revenue', alias: turnover, source: llm, status: pending}\n"
        ))
        weights = load_sql_schema(base).metrics["revenue"]["alias_weights"]
        assert weights == {"revenue": 1.0, "gmv": 0.9, "sales": 0.4}

    def test_unknown_alias_entity_rejected(self, tmp_path):
        base = _write_catalog(tmp_path, "aliases:\n  - {entity_id: 'column:orders.nope', alias: x}\n")
        with pytest.raises(ValueError, match="unknown entity_id"):
            load_sql_schema(base)

    def test_unknown_alias_source_rejected(self, tmp_path):
        base = _write_catalog(tmp_path, "aliases:\n  - {entity_id: 'table:orders', alias: x, source: wiki}\n")
        with pytest.raises(ValueError, match="unknown source"):
            load_sql_schema(base)

    def test_semantic_layer_reference_checked(self, tmp_path):
        base = _write_catalog(tmp_path, "aliases: []\n", metrics_yaml=(
            "joins:\n  - {from: orders, to: users, sql_on: 'orders.user_id = users.id'}\n"
        ))
        with pytest.raises(ValueError, match="unknown table 'users'"):
            load_sql_schema(base)


# =========================
# 运行测试
# =========================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestEnumNormalization:
    """schema enum_values：过滤值的确定性规范化"""

    @pytest.fixture
    def schema(self):
        return load_sql_schema("catalog")

    @pytest.mark.parametrize("raw,expected,method", [
        ("credit_card", "credit_card", "exact"),
        ("credit card", "credit_card", "normalized"),
        ("Credit-Card", "credit_card", "normalized"),
        ("Gold", "gold", "normalized"),
        ("cancelled", "canceled", "fuzzy"),
        ("vip", "vip", "unmatched"),
    ])
    def test_normalize(self, schema, raw, expected, method):
        col = "users.vip_level" if raw in ("Gold", "vip") else (
            "orders.status" if raw == "cancelled" else "payments.payment_type")
        assert schema.normalize_enum_value(col, raw) == (expected, method)

    def test_non_enum_column_untouched(self, schema):
        assert schema.normalize_enum_value("orders.amount", "100") == ("100", "not_enum")


# =========================
# 召回增强与类型化阈值测试
# =========================

class TestRecallEnhancements:
    """IDF 加权召回 + typo 容忍（LexicalRetriever）"""

    @pytest.fixture
    def schema_matcher(self):
        return EntityMatcher(load_sql_schema("catalog").tables)

    def test_typo_in_every_token_still_recalls(self, schema_matcher):
        """'orde tablez' 两个 token 全是 typo：edit-distance-1 探测救回召回"""
        result = schema_matcher.match("orde tablez")
        assert result.matched == "orders"
        assert result.score >= 80.0
        assert result.explain["recall"]["lexical"]["typo_matched"] == {
            "orde": "order", "tablez": "table",
        }

    def test_typo_probe_ignored_for_short_tokens(self, schema_matcher):
        """<4 字符 token 不做探测（避免 'id'/'if' 这类误纠）"""
        result = schema_matcher.match("id xyzq")
        assert result.explain["recall"]["lexical"]["candidate_count"] == 0

    def test_idf_weighting_prefers_rare_token_doc(self):
        """命中数打平时，命中稀有 token 的文档胜出（朴素计数下按 doc id 任意排序）"""
        catalog = {
            "doc_a": {"aliases": ["alpha", "alpha two", "alpha three"], "columns": {}},
            "doc_b": {"aliases": ["alpha", "alpha four", "alpha five"], "columns": {}},
            "doc_c": {"aliases": ["alpha", "alpha six", "alpha seven"], "columns": {}},
            "doc_rare": {"aliases": ["zephyr dashboard"], "columns": {}},
        }
        # 'alpha' 高 df（3/4 文档），'zephyr' 仅 1 个文档
        _, explain = LexicalRetriever(catalog).retrieve("alpha zephyr", k=10)
        top = explain["top_candidates"][0]
        assert top["name"] == "doc_rare"
        # 原始命中数一致（各 1），但加权分不同
        others = [c for c in explain["top_candidates"] if c["name"] != "doc_rare"]
        assert all(top["score"] > c["score"] for c in others)

    def test_recall_explain_keeps_raw_hit_count(self, schema_matcher):
        _, explain = schema_matcher.retrievers[0].retrieve("order table", k=10)
        for c in explain["top_candidates"]:
            assert "hit_count" in c and "score" in c

    def test_recall_tokens_truncate_after_stopwords(self):
        """停用词不占 token 名额：by/for/each/of/the 被过滤后才截断到 5 个"""
        tokens = recall_tokens("Total revenue by region for each of the top sellers")
        assert tokens == ["total", "revenue", "region", "top", "seller"]

    def test_recall_tokens_split_camel_case(self):
        assert recall_tokens("shippingFee") == ["shipping", "fee"]

    def test_long_alias_tail_tokens_are_indexed(self):
        """别名侧同样先去停用词再截断：长别名尾部的判别词进入倒排索引"""
        catalog = {
            "doc_long": {"aliases": ["share of the orders in each of the regions"]},
            "doc_other": {"aliases": ["order share"]},
        }
        retriever = LexicalRetriever(catalog)
        assert retriever.token_to_names.get("region") == ["doc_long"]


class TestAliasScoring:
    """别名打分：置信度加权 / 长别名不截断 / 子串命中方向 / 可插拔 retriever"""

    @pytest.fixture(scope="class")
    def schema(self):
        return load_sql_schema("catalog")

    def test_short_query_does_not_substring_hit_longer_alias(self, schema):
        """'customer' 不应以 90 分子串命中 reviews 的 'customer reviews'，复数归一后 exact 命中 users"""
        r = EntityMatcher(schema.tables).match("customer")
        assert r.matched == "users"
        assert r.explain["method"] == "exact_alias_match"
        assert r.candidates[0]["alias"] == "customer"  # 'customers' 的 match key
        reviews = [c for c in r.candidates if c["name"] == "reviews"]
        assert not reviews or reviews[0]["score"] < 70

    def test_plural_query_exact_hits_singular_alias(self, schema):
        """'product categories' 与别名 'product category' 复数折叠后 exact 命中（-ies → -y）"""
        r = EntityMatcher(schema.columns).match("product categories")
        assert (r.matched, r.score, r.explain["method"]) == ("products.category", 100.0, "exact_alias_match")

    def test_stem_keeps_ss_us_is_endings(self):
        from matcher.entity_matcher import match_key
        assert match_key("Addresses status analysis") == "addresse status analysis"
        assert match_key("categories orders") == "category order"

    def test_bare_entity_noun_as_metric_is_count(self, schema):
        """L1 常把 'How many orders' 抽成指标 'orders'：alias 表把它映射为 order_count"""
        r = EntityMatcher(schema.metrics).match("orders")
        assert (r.matched, r.score) == ("order_count", 100.0)

    def test_long_query_still_hits_shorter_alias(self, schema):
        """query 比别名长（加了修饰词）仍允许子串命中"""
        r = EntityMatcher(schema.metrics).match("total revenue")
        assert r.matched == "revenue"

    def test_long_aliases_are_indexed(self, schema):
        """>20 字符的别名不再被静默丢弃"""
        r = EntityMatcher(schema.metrics).match("gross merchandise value")
        assert r.matched == "revenue"
        assert r.explain["method"] == "exact_alias_match"

    def test_alias_confidence_scales_score(self):
        m = EntityMatcher({
            "revenue": {"aliases": ["revenue", "turnover"], "alias_weights": {"turnover": 0.6}},
        })
        r = m.match("turnover")
        assert r.candidates == [{"name": "revenue", "score": 60.0, "alias": "turnover"}]

    def test_pluggable_retriever_candidates_are_unioned(self):
        """额外 retriever（如 embedding）召回词法召回不到的实体，打分仍走别名"""

        class FakeSemantic:
            name = "semantic"

            def retrieve(self, query, k):
                return ["revenue"], {"note": "fake"}

        entities = {"revenue": {"aliases": ["income"]}, "orders": {"aliases": ["orders"]}}
        m = EntityMatcher(entities, retrievers=[LexicalRetriever(entities), FakeSemantic()])
        r = m.match("earnings")  # 无 exact 别名、词法召回不到，只能靠额外 retriever
        assert r.matched == "revenue"
        assert set(r.explain["recall"]) == {"lexical", "semantic"}


class TestTypeThresholds:
    """按类型阈值 + 并列歧义守卫（matcher_service.py）"""

    @staticmethod
    def _service_with(matcher, matcher_type):
        svc = MatcherService.__new__(MatcherService)
        setattr(svc, f"{matcher_type.value}_matcher", matcher)
        return svc

    def test_metric_auto_accept_is_stricter(self):
        """b02 回归：metric fuzzy 85.5 落入确认带（metric 阈值 90）"""
        svc = MatcherService.__new__(MatcherService)
        svc.metric_matcher = FakeMatcher({"customer lifetime value": _mr_with_cands(
            "avg_order_value", 85.5, [("avg_order_value", 85.5), ("revenue", 70.0)])})
        r = svc.resolve_with_candidates(MatcherType.METRIC, [_E("customer lifetime value")])
        assert r.needs_confirmation is True
        assert r.method == "fuzzy_low_confidence"

    def test_metric_exact_still_auto_accepted(self):
        svc = MatcherService.__new__(MatcherService)
        svc.metric_matcher = FakeMatcher({"gmv": _mr_with_cands("revenue", 100.0, [])})
        r = svc.resolve_with_candidates(MatcherType.METRIC, [_E("gmv")])
        assert r.needs_confirmation is False

    def test_tied_high_scores_force_confirmation(self):
        """table 95 分但第二名 88（分差<10）→ 并列歧义进确认"""
        svc = MatcherService.__new__(MatcherService)
        svc.table_matcher = FakeMatcher({"payment table": _mr_with_cands(
            "payments", 95.0, [("payments", 95.0), ("orders", 88.0)])})
        r = svc.resolve_with_candidates(MatcherType.TABLE, [_E("payment table")])
        assert r.needs_confirmation is True
        assert r.method == "fuzzy_tied"

    def test_clear_margin_auto_accepted(self):
        """95 分且领先第二名 >= 10 → 直接采纳"""
        svc = MatcherService.__new__(MatcherService)
        svc.table_matcher = FakeMatcher({"orde table": _mr_with_cands(
            "orders", 95.0, [("orders", 95.0), ("reviews", 73.0)])})
        r = svc.resolve_with_candidates(MatcherType.TABLE, [_E("orde table")])
        assert r.needs_confirmation is False


class TestDecisionOrder:
    """候选 → 偏好弱加权 → 分档：偏好在判定前生效，且判定只有一处"""

    @staticmethod
    def _svc(score_a, score_b):
        svc = MatcherService.__new__(MatcherService)
        svc.table_matcher = FakeMatcher({"q": _mr_with_cands(
            "orders", score_a, [("orders", score_a), ("payments", score_b)])})
        return svc

    def test_bias_applies_before_banding(self):
        """85 vs 80 并列 → 偏好给 orders +6 后拉开分差 → 直接采纳"""
        assert self._svc(85.0, 80.0).resolve_with_candidates(MatcherType.TABLE, [_E("q")]).method == "fuzzy_tied"
        r = self._svc(85.0, 80.0).resolve_with_candidates(MatcherType.TABLE, [_E("q")], bias=_bias_orders)
        assert (r.value, r.needs_confirmation, r.method) == ("orders", False, "fuzzy")

    def test_bias_cannot_lift_candidate_over_accept_line(self):
        """原始分 76 < 采纳线 80：偏好 +6 后 82 也不能静默采纳，只能排第一进确认流"""
        r = self._svc(76.0, 40.0).resolve_with_candidates(MatcherType.TABLE, [_E("q")], bias=_bias_orders)
        assert (r.value, r.needs_confirmation, r.method) == ("orders", True, "fuzzy_low_confidence")

    def test_bias_cannot_lift_candidate_over_confirm_floor(self):
        """原始分 36 < CONFIRM_FLOOR：偏好加分后仍视为无匹配"""
        r = self._svc(36.0, 10.0).resolve_with_candidates(MatcherType.TABLE, [_E("q")], default="", bias=_bias_orders)
        assert (r.method, r.needs_confirmation) == ("no_match", False)

    def test_bias_flip_marked_and_still_banded(self):
        """偏好把第二名推到第一时标注 +user_bias，且仍按分档判定（55 分仍需确认）"""
        def bias(cands):
            return [{"value": "payments", "score": 56.0, "raw_score": 50.0},
                    {"value": "orders", "score": 55.0, "raw_score": 55.0}]
        r = self._svc(55.0, 50.0).resolve_with_candidates(MatcherType.TABLE, [_E("q")], bias=bias)
        assert r.value == "payments"
        assert r.needs_confirmation is True
        assert r.method == "fuzzy_low_confidence+user_bias"

    def test_below_floor_is_no_match(self):
        r = self._svc(30.0, 10.0).resolve_with_candidates(MatcherType.TABLE, [_E("q")], default="orders")
        assert (r.value, r.method, r.needs_confirmation) == ("orders", "no_match", False)

    def test_low_confidence_column_goes_to_confirmation_not_guessed(self):
        """列低置信/并列不再静默取 top1：'customer' 在 orders.user_id / users.id 并列"""
        from service.query_orchestrator import QueryOrchestrator

        svc = MatcherService(catalog_path="catalog")
        sink: dict = {}
        cols, entries = QueryOrchestrator._resolve_texts_to_columns(None, svc, ["customer"], sink)
        assert cols == []
        assert {c["value"] for c in entries[0]["confirm_candidates"]} == {"orders.user_id", "users.id"}


def _bias_orders(cands):
    """模拟 UserPreferenceStore：orders +6，保留 raw_score"""
    return sorted(
        [{**c, "raw_score": c["score"], "score": c["score"] + (6 if c["value"] == "orders" else 0)} for c in cands],
        key=lambda c: -c["score"])


class _E:
    def __init__(self, text):
        self.text = text


class FakeMatcher:
    """text -> 预置 MatchResult 的假匹配器（仅测试用）"""

    def __init__(self, results=None):
        self.results = results or {}

    def match(self, text):
        if text in self.results:
            return self.results[text]
        return MatchResult(matched=None, score=0.0)


def _mr_with_cands(matched, score, candidates):
    cands = [{"name": v, "score": s} for v, s in candidates]
    return MatchResult(matched=matched, score=score, candidates=cands)


class TestExactAliasCollision:
    """同名别名冲突：暴露歧义而非 first-wins（a01/a02 修复的单元层）"""

    @pytest.fixture
    def column_matcher(self):
        return EntityMatcher(load_sql_schema("catalog").columns)

    def test_collision_detected(self, column_matcher):
        """'amount' 在 orders/payments 两列 → matched=None + 冲突候选"""
        r = column_matcher.match("amount")
        assert r.matched is None
        assert r.explain["method"] == "exact_alias_collision"
        names = {c["name"] for c in r.candidates}
        assert names == {"orders.amount", "payments.amount"}

    def test_three_way_collision(self, column_matcher):
        """'time' 三路冲突（orders/payments/reviews 的时间列）"""
        r = column_matcher.match("time")
        assert r.explain["method"] == "exact_alias_collision"
        assert len(r.candidates) == 3

    def test_unique_alias_unaffected(self, column_matcher):
        r = column_matcher.match("payment type")
        assert r.matched == "payments.payment_type"
        assert r.explain["method"] == "exact_alias_match"


class TestCollisionDistancePolicy:
    """exact 冲突的 join 距离消歧（matcher_service 层）"""

    @pytest.fixture
    def service(self):
        return MatcherService(catalog_path="catalog")

    def test_two_way_resolved_by_distance(self, service):
        """revenue 语境（base=orders）下 region：users 1 跳 vs sellers 2 跳 → 自动选 users"""
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("region")], base_table="orders")
        assert r.value == "users.region"
        assert r.method == "exact_collision_distance_resolved"
        assert r.needs_confirmation is False

    def test_no_base_table_confirms(self, service):
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("region")])
        assert r.needs_confirmation is True
        assert r.method == "exact_alias_collision"
        assert {c["value"] for c in r.candidates} == {"users.region", "sellers.region"}

    def test_three_way_always_confirms(self, service):
        """3 路超泛化词（time）即使有基表也不猜"""
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("time")], base_table="orders")
        assert r.needs_confirmation is True
        assert len(r.candidates) == 3

    def test_base_table_own_column_wins(self, service):
        """base=payments 下 amount：payments.amount 距离 0"""
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("amount")], base_table="payments")
        assert r.value == "payments.amount"
        assert r.needs_confirmation is False
