"""Ingress 模块测试：清洗、去重、Telegram 适配"""
import time


class TestMessageDeduplicator:
    """ingress/deduplicator.py"""

    def test_new_message_not_duplicate(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator()
        assert dedup.is_duplicate("msg_1") is False

    def test_same_message_is_duplicate(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator()
        dedup.is_duplicate("msg_1")
        assert dedup.is_duplicate("msg_1") is True

    def test_different_messages_not_duplicate(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator()
        dedup.is_duplicate("msg_1")
        assert dedup.is_duplicate("msg_2") is False

    def test_ttl_expiry(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator(ttl_seconds=1)
        dedup.is_duplicate("msg_1")
        time.sleep(1.1)
        assert dedup.is_duplicate("msg_1") is False

    def test_window_size_eviction(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator(window_size=2)
        dedup.is_duplicate("msg_1")
        dedup.is_duplicate("msg_2")
        dedup.is_duplicate("msg_3")  # msg_1 evicted
        assert dedup.is_duplicate("msg_1") is False

    def test_reset_clears_all(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator()
        dedup.is_duplicate("msg_1")
        dedup.is_duplicate("msg_2")
        dedup.reset()
        assert dedup.size() == 0
        assert dedup.is_duplicate("msg_1") is False

    def test_size_tracking(self):
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator()
        for i in range(5):
            dedup.is_duplicate(f"msg_{i}")
        assert dedup.size() == 5


class TestMessageCleaner:
    """ingress/cleaner.py"""

    def test_clean_normal_text(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.clean("hello world") == "hello world"

    def test_clean_control_chars(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        result = cleaner.clean("hello\x00world\x01")
        assert "hello" in result
        assert "world" in result
        assert "\x00" not in result
        assert "\x01" not in result

    def test_clean_bot_command_prefix(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        result = cleaner.clean("/start hello")
        assert result == "hello"

    def test_clean_empty_input(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.clean("") == ""

    def test_clean_too_short_returns_empty(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.clean("a") == ""

    def test_clean_single_digit_kept_for_confirmation_reply(self):
        """确认流的编号回复（"1"）只有一个字符，不能被当成空消息丢掉"""
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.clean("1") == "1"
        assert cleaner.clean(" 3 ") == "3"
        assert cleaner.is_valid("1") is True

    def test_clean_chinese_preserved(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.clean("应用启动") == "应用启动"

    def test_is_valid_true(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.is_valid("hello") is True

    def test_is_valid_false_empty(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.is_valid("") is False

    def test_is_valid_false_too_short(self):
        from ingress.cleaner import MessageCleaner
        cleaner = MessageCleaner()
        assert cleaner.is_valid("a") is False


class TestTelegramAdapter:
    """ingress/telegram_adapter.py"""

    def _make_update(self, text="hello", update_id=1, message_id=100, user_id=42, chat_id=999):
        return {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "from": {"id": user_id},
                "chat": {"id": chat_id},
                "text": text,
            },
        }

    def test_adapt_basic_message(self):
        from ingress.telegram_adapter import TelegramAdapter
        from ingress.deduplicator import MessageDeduplicator
        adapter = TelegramAdapter(deduplicator=MessageDeduplicator())
        msg = adapter.adapt(self._make_update())

        assert msg.channel == "telegram"
        assert msg.user_id == "42"
        assert msg.chat_id == "999"
        assert msg.text == "hello"
        assert msg.is_duplicate is False

    def test_adapt_cleans_text(self):
        from ingress.telegram_adapter import TelegramAdapter
        from ingress.deduplicator import MessageDeduplicator
        adapter = TelegramAdapter(deduplicator=MessageDeduplicator())
        msg = adapter.adapt(self._make_update(text="/start 查询PV"))

        assert msg.text == "查询PV"
        assert msg.is_cleaned is True

    def test_adapt_detects_duplicate(self):
        from ingress.telegram_adapter import TelegramAdapter
        from ingress.deduplicator import MessageDeduplicator
        dedup = MessageDeduplicator()
        adapter = TelegramAdapter(deduplicator=dedup)

        update = self._make_update(update_id=42, message_id=100)
        adapter.adapt(update)
        msg2 = adapter.adapt(update)
        assert msg2.is_duplicate is True

    def test_extract_message_id_format(self):
        from ingress.telegram_adapter import TelegramAdapter
        adapter = TelegramAdapter()
        msg_id = adapter.extract_message_id(self._make_update(update_id=42, message_id=100))
        assert msg_id == "telegram_42_100"

    def test_adapt_empty_text(self):
        from ingress.telegram_adapter import TelegramAdapter
        from ingress.deduplicator import MessageDeduplicator
        adapter = TelegramAdapter(deduplicator=MessageDeduplicator())
        msg = adapter.adapt(self._make_update(text=""))

        assert msg.text == ""
