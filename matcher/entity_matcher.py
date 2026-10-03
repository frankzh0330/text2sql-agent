"""实体匹配器：自然语言片段 → catalog 实体名（表 / 限定列名 / 指标）

一个类服务三种实体，流程两段：

  1. 召回（Retriever，可插拔）：从全部实体中取 top-k 候选
       - LexicalRetriever（默认）：倒排索引 + IDF 加权 + edit-distance-1 typo 容忍
       - 生产可并联 embedding 检索（见 docs/ARCHITECTURE.md「Retriever 接入」），
         多个 retriever 的候选取并集
  2. 打分：候选分 = max(别名模糊分 × 别名置信度)，置信度来自 alias 表的 source
       - 别名比 query 长时不允许子串命中（"customer" 不应以 90 分命中 "customer reviews"），
         只按整串/词序无关相似度计分

exact 别名命中在召回前短路；同一别名属于多个实体时返回冲突候选（不 first-wins）。
召回、exact 与打分都在单复数折叠后的 match key 上进行（"product categories" ≡ "product category"）。
本模块只产出排序后的候选，accept / confirm 的判定在 MatcherService。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from common.text_utils import is_single_chinese_char, normalize, tokenize_mixed

# 停用词：召回时索引和查询两侧都跳过（exact 别名整串匹配不受影响）
# - at: created_at/paid_at 等 snake_case 列名拆词副产物
# - 其余为别名 / L1 抽取短语中的介词冠词
# table/id/count/date 等高 df 词不停用——泛化词降权是 IDF 的职责
STOPWORDS = {"at", "of", "by", "per", "the", "for", "in", "and", "or", "with", "each", "every"}

_MAX_RECALL_TOKENS = 5    # 去停用词后每段文本最多保留的召回 token 数（查询与别名索引两侧一致）
_TYPO_MIN_TOKEN_LEN = 4   # 只对 ≥4 字符的零命中 token 做 edit-distance-1 探测
_TYPO_WEIGHT = 0.75       # typo 探测命中的 token 权重折减
_RECALL_TOP_K = 50        # 每个 retriever 最多返回的候选数
_CANDIDATES_KEPT = 5      # MatchResult 中保留的候选数（供确认流 / reranker）


@dataclass(frozen=True)
class MatchResult:
    """匹配结果：matched 为 top1（exact 冲突 / 无候选时为 None），candidates 按分数降序"""
    matched: Optional[str]
    score: float
    candidates: List[Dict[str, Any]] = field(default_factory=list)  # [{name, score, alias}]
    explain: Dict[str, Any] = field(default_factory=dict)


class Retriever(Protocol):
    """召回接口：query → 按相关性排序的实体名 + explain"""

    name: str

    def retrieve(self, query: str, k: int) -> Tuple[List[str], Dict[str, Any]]:
        ...


def _stem(token: str) -> str:
    """极简英文复数归一：categories → category，customers → customer

    -ss / -us / -is 结尾不动（address / status / analysis）。规则只需对 query 与别名
    两侧一致，不追求语言学正确。
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def match_key(text: str) -> str:
    """exact 查找与打分用的 key：normalize 后逐词复数归一"""
    return " ".join(_stem(t) for t in normalize(text).split())


def recall_tokens(text: str) -> List[str]:
    """召回 token：分词 → 去停用词 / 中文单字 → 复数归一 → 截断

    截断必须在去停用词之后：否则 "revenue by region for each of the top sellers"
    中的 by/for 会占满名额，把 top/sellers 挤掉。
    """
    tokens = []
    for t in tokenize_mixed(text, max_tokens=None):
        if t in STOPWORDS or is_single_chinese_char(t):
            continue
        tokens.append(_stem(t))
    return tokens[:_MAX_RECALL_TOKENS]


class LexicalRetriever:
    """倒排索引召回（BM25-lite）

    - IDF 加权：df 高的泛化 token（table/amount/id）降权，判别性 token 主导排序
    - typo 容忍：零命中且长度达标的 token 做 edit-distance-1 词表探测，权重打 75 折
    """

    name = "lexical"

    def __init__(self, entities: Dict[str, Dict[str, Any]]):
        index: Dict[str, Set[str]] = {}
        for name, info in entities.items():
            for alias in info.get("aliases", []):
                for token in recall_tokens(alias):
                    index.setdefault(token, set()).add(name)
        self.token_to_names = {t: sorted(names) for t, names in index.items()}
        self.n_docs = max(1, len(entities))

    def retrieve(self, query: str, k: int) -> Tuple[List[str], Dict[str, Any]]:
        scores: Dict[str, float] = {}
        hit_counts: Dict[str, int] = {}
        token_hits = []
        typo_matched: Dict[str, str] = {}

        for token in recall_tokens(query):
            names = self.token_to_names.get(token, [])
            weight = 1.0
            if not names and len(token) >= _TYPO_MIN_TOKEN_LEN:
                probed = self._probe_typo(token)
                if probed:
                    typo_matched[token] = probed
                    names = self.token_to_names[probed]
                    weight = _TYPO_WEIGHT
            token_hits.append((token, len(names)))
            if not names:
                continue
            idf = math.log(1.0 + self.n_docs / (1.0 + len(names)))
            for name in names:
                scores[name] = scores.get(name, 0.0) + idf * weight
                hit_counts[name] = hit_counts.get(name, 0) + 1

        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]
        explain = {
            "token_hits": token_hits,
            "typo_matched": typo_matched,
            "candidate_count": len(ranked),
            "top_candidates": [
                {"name": n, "hit_count": hit_counts[n], "score": round(s, 3)} for n, s in ranked[:10]
            ],
        }
        return [n for n, _ in ranked], explain

    def _probe_typo(self, token: str) -> Optional[str]:
        """零命中 token 的 edit-distance-1 词表探测（词表遍历；万级词时换 deletes-1 索引）"""
        for vocab in sorted(self.token_to_names):
            if len(vocab) < _TYPO_MIN_TOKEN_LEN or abs(len(vocab) - len(token)) > 1:
                continue
            if Levenshtein.distance(token, vocab, score_cutoff=1) <= 1:
                return vocab
        return None


def alias_similarity(query: str, alias: str) -> float:
    """query 与单个别名的相似度（0-100，入参已经过 match_key）

    WRatio 含子串匹配：query 比别名长时（"total revenue" vs "revenue"）有用；
    别名比 query 长时子串命中意味着 query 缺了别名的判别词（"customer" vs
    "customer reviews"），此时只用词序无关的整串相似度。
    """
    if len(alias) > len(query):
        return float(fuzz.token_sort_ratio(query, alias))
    return float(fuzz.WRatio(query, alias))


class EntityMatcher:
    """表 / 列 / 指标通用匹配器

    :param entities: {name: {"aliases": [...], "alias_weights": {alias: confidence}}}
                     （alias_weights 缺省时全部按 1.0）
    :param retrievers: 召回器列表，默认只有 LexicalRetriever
    """

    def __init__(
        self,
        entities: Dict[str, Dict[str, Any]],
        retrievers: Optional[List[Retriever]] = None,
    ):
        self.retrievers: List[Retriever] = retrievers or [LexicalRetriever(entities)]

        # name -> [(match_key(alias), confidence)]
        self.aliases: Dict[str, List[Tuple[str, float]]] = {}
        # normalized_alias -> [(name, confidence)]；长度 >1 即 exact 冲突
        self.exact: Dict[str, List[Tuple[str, float]]] = {}
        for name, info in entities.items():
            weights = info.get("alias_weights") or {}
            seen: Dict[str, float] = {}
            for alias in [name] + list(info.get("aliases", [])):
                norm = match_key(alias)
                if norm:
                    seen[norm] = max(seen.get(norm, 0.0), float(weights.get(alias, 1.0)))
            self.aliases[name] = list(seen.items())
            for norm, conf in seen.items():
                self.exact.setdefault(norm, []).append((name, conf))

    def match(self, query: str) -> MatchResult:
        norm_query = match_key(query)
        if not norm_query:
            return MatchResult(None, 0.0, explain={"method": "empty_query", "query": query})

        # exact 命中：唯一 → 直接返回；多实体 → 暴露冲突（如 amount / time / region）
        exact = self.exact.get(norm_query)
        if exact:
            cands = sorted(
                ({"name": n, "score": round(100.0 * c, 2), "alias": norm_query} for n, c in exact),
                key=lambda x: (-x["score"], x["name"]),
            )
            if len(exact) > 1:
                return MatchResult(None, cands[0]["score"], cands,
                                   {"method": "exact_alias_collision", "query": query})
            return MatchResult(cands[0]["name"], cands[0]["score"], cands,
                               {"method": "exact_alias_match", "query": query})

        # 召回：多 retriever 候选取并集（保序）
        recalled: List[str] = []
        recall_explain: Dict[str, Any] = {}
        for r in self.retrievers:
            names, r_explain = r.retrieve(query, _RECALL_TOP_K)
            recall_explain[r.name] = r_explain
            recalled += [n for n in names if n not in recalled and n in self.aliases]

        # 打分：max(别名相似度 × 别名置信度)
        scored = []
        for name in recalled:
            best = max(
                ((alias_similarity(norm_query, a) * conf, a) for a, conf in self.aliases[name]),
                key=lambda x: x[0],
            )
            scored.append({"name": name, "score": round(best[0], 2), "alias": best[1]})
        scored.sort(key=lambda x: (-x["score"], x["name"]))
        cands = scored[:_CANDIDATES_KEPT]

        explain = {"query": query, "normalized_query": norm_query, "recall": recall_explain}
        if not cands:
            return MatchResult(None, 0.0, [], {"method": "no_candidates", **explain})
        top = cands[0]
        return MatchResult(top["name"], top["score"], cands, {"method": "recall_and_score", **explain})
