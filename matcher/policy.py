"""实体解析判定阈值：唯一出处

MatcherService.resolve_with_candidates 与 LLM reranker 都只从这里取阈值。
分数尺度 0-100 = 别名相似度 × 别名置信度（见 matcher/entity_matcher.py）。
"""
from __future__ import annotations

from types import MappingProxyType

from common.types import MatcherType

# top1 达到该分且不与第二名并列 → 直接采纳
# metric 错配代价最高（口径错则数字全错）→ 收紧到 90；table/column 的模糊命中多为复数/typo
ACCEPT_SCORE = MappingProxyType({
    MatcherType.TABLE: 80.0,
    MatcherType.METRIC: 90.0,
    MatcherType.COLUMN: 80.0,
})

# [CONFIRM_FLOOR, 采纳线) 或并列 → 确认流；低于该分 → 视为无匹配（候选太模糊，问了也没价值）
CONFIRM_FLOOR = 40.0

# top1 领先第二名不足该分值 → 并列歧义，即使过采纳线也进确认流
TIE_MARGIN = 10.0

# LLM reranker（仅确认带触发）：相关性 ≥ 85 且领先第二名 ≥ 15 → 免确认直接采纳
LLM_ACCEPT_RELEVANCE = 85.0
LLM_ACCEPT_MARGIN = 15.0
