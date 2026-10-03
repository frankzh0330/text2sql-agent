"""Memory Module - 记忆文件系统

架构:
┌─────────────────────────────────────────┐
│  Working Memory (session_manager.py)    │
│  - 当前会话消息 (JSONL 持久化)           │
│  - QueryState (上下文继承)               │
└─────────────────────────────────────────┘
                 ↓ 读取
┌─────────────────────────────────────────┐
│  Long-Term Memory (long_term_memory.py) │
│  - 纠正/约束 (Markdown + MEMORY.md 索引) │
│  - 写入: memory_writer.py (异步 LLM judge)│
└─────────────────────────────────────────┘
┌─────────────────────────────────────────┐
│  User Preference (user_preference_store) │
│  - project+user 使用计数, recall 后弱加权 │
└─────────────────────────────────────────┘
"""

from memory.long_term_memory import LongTermMemory

__all__ = ["LongTermMemory"]
