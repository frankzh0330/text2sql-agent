from __future__ import annotations

import re
from typing import List, Optional

import jieba


def contains_chinese(text: str) -> bool:
    """检测文本是否包含中文"""
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def is_single_chinese_char(token: str) -> bool:
    """检测是否为单个中文字符"""
    return bool(re.fullmatch(r"[\u4e00-\u9fff]", token))


def normalize(text: str) -> str:
    """
    全局 normalize：
    - lower
    - camelCase -> camel Case
    - _ - -> 空格
    - 去掉大部分特殊符号
    - 压缩空格
    """
    if text is None:
        return ""

    text = text.strip()

    # camelCase -> camel Case
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)

    text = text.lower()
    text = text.replace("_", " ").replace("-", " ")

    # 保留中英文、数字、空格
    text = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", text)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize_mixed(text: str, max_tokens: Optional[int] = 5) -> List[str]:
    """
    中英混合分词：
    - 英文 / 数字 / 下划线块：按 _ 和空格拆
    - 中文块：用 jieba.cut

    Args:
        text: 待分词文本
        max_tokens: 最多返回多少个 token，默认 5；None 表示不截断
                    （需要先过滤停用词再截断的调用方传 None，自行截断）
    """
    # 先 normalize 再切块：normalize 在 lower 之前拆 camelCase（shippingFee -> shipping fee），
    # 直接 lower 会把大小写边界抹掉
    norm_text = normalize(text)
    if not norm_text:
        return []

    # 按中英文块切开
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", norm_text)

    tokens: List[str] = []

    for part in parts:
        # 英文块
        if re.fullmatch(r"[a-z0-9_]+", part):
            english_tokens = re.split(r"[_\s]+", part)
            tokens.extend([t for t in english_tokens if t])

        # 中文块
        else:
            chinese_tokens = [t.strip() for t in jieba.cut(part) if t.strip()]
            tokens.extend(chinese_tokens)

    # normalize once more
    tokens = [normalize(t) for t in tokens if normalize(t)]
    tokens = list(dict.fromkeys(tokens))  # 去重保序

    # 限制 token 数量
    if max_tokens is not None and len(tokens) > max_tokens:
        tokens = tokens[:max_tokens]

    return tokens
