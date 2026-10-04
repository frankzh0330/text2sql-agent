"""消息清洗器 - 清洗脏数据和无意义的前缀"""

import re
from typing import List


class MessageCleaner:
    """消息清洗器

    功能：
    1. 移除控制字符和脏字符
    2. 移除无意义前缀（如机器人命令）
    3. 标准化空白字符
    """

    # 脏数据模式
    DIRTY_PATTERNS = [
        r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]',  # 控制字符
        r'[^\w\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef,.!?;:()""''【】（）。，！？；：]',  # 非常用字符
    ]

    # 无意义的前缀（如机器人命令）
    PREFIX_PATTERNS = [
        r'^/[a-zA-Z]+\s*',  # Telegram bot commands: /start, /help 等
    ]

    def __init__(self):
        # 编译正则表达式
        self.dirty_regex = re.compile('|'.join(self.DIRTY_PATTERNS))
        self.prefix_regex = re.compile('|'.join(self.PREFIX_PATTERNS))

    def clean(self, text: str) -> str:
        """清洗文本"""
        if not text:
            return ""

        # 1. 先移除无意义前缀（但保留内容）
        #    必须在脏字符清洗之前，否则前缀可能被破坏
        text = self.prefix_regex.sub('', text)

        # 2. 移除控制字符和脏字符
        text = self.dirty_regex.sub('', text)

        # 3. 标准化空白字符
        text = re.sub(r'\s+', ' ', text)

        # 4. 去除首尾空格
        text = text.strip()

        # 5. 检查清洗后是否为空；纯数字短消息保留（确认流的编号回复，如 "1"）
        if not text or (len(text) < 2 and not text.isdigit()):
            return ""

        return text

    def is_valid(self, text: str) -> bool:
        """检查文本是否有效"""
        if not text:
            return False
        return bool(self.clean(text))
