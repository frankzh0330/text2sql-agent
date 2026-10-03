---
title: "Architecture Diagram"
---

```mermaid
graph TB
    subgraph Client["Client Layer"]
        TelegramUser["Telegram User"]
        HttpClient["HTTP Client"]
    end

    subgraph Gateway["Gateway Layer"]
        TelegramGateway["TelegramGateway<br/>Long Polling"]
        BaseGateway["BaseGateway"]
    end

    subgraph Ingress["Ingress Layer"]
        TelegramAdapter["TelegramAdapter"]
        Cleaner["MessageCleaner<br/>Text Cleaning"]
        Dedup["MessageDeduplicator<br/>Deduplication"]
        StandardMessage["StandardMessage<br/>Unified Message Model"]
    end

    subgraph Runtime["Runtime Layer"]
        Bus["Message Bus<br/>DirectCallBus / RedisBus"]
        Worker["AgentWorker"]
        Dispatcher["ResponseDispatcher"]
    end

    subgraph API["API Layer"]
        FastAPI["FastAPI App<br/>app.py"]
        Models["NL2SQLRequest<br/>NL2SQLResponse"]
        TelegramNotify["Telegram Notification Helper"]
    end

    subgraph Service["Service Layer"]
        Orchestrator["QueryOrchestrator<br/>Turn Routing + Business Flow"]
        LLM["LLM Intent Extraction<br/>SQLIntentJson"]
        SQLGen["SQL Generator<br/>Grounded ClickHouse Generation"]
        SQLVal["SQL Validator<br/>sqlglot Guardrails"]
        ASTAnalyzer["SQL AST Analyzer<br/>Entity Fidelity + Join Checks"]
        Reranker["LLM Reranker<br/>Optional, Low-Confidence Only"]
        SessionManager["SessionManager<br/>Session State"]
        TaskManager["TaskManager<br/>Confirmation Tasks"]
    end

    subgraph Memory["Memory Layer"]
        LongTermMemory["LongTermMemory<br/>Project Memory"]
        MemoryWriter["MemoryWriter<br/>Async Learning"]
        UserPreference["UserPreferenceStore<br/>Weak Pre-decision Bias"]
        Storage["JSONL / Markdown / JSON Storage"]
    end

    subgraph Matcher["Matcher Layer"]
        MatcherService["MatcherService<br/>Decision + Table/Join Inference"]
        Policy["policy.py<br/>All Thresholds"]
        EntityMatcher["EntityMatcher<br/>table / table.column / metric"]
        Retriever["Retriever (pluggable)<br/>LexicalRetriever: IDF Inverted Index"]
        TimeMatcher["TimeMatcher"]
    end

    subgraph Schema["Metadata Layer"]
        TablesYaml["tables.yaml<br/>Physical Catalog (Unity Catalog mock)"]
        MetricsYaml["metrics.yaml<br/>Semantic Layer (LookML mock)"]
        AliasesYaml["aliases.yaml<br/>Alias Table (source + confidence)"]
        SchemaLoader["Schema Loader<br/>Source Adapters + Merge"]
    end

    subgraph External["External Services"]
        TelegramAPI["Telegram Bot API"]
        LLMBackend["LLM Backend"]
    end

    TelegramUser --> TelegramAPI
    TelegramAPI --> TelegramGateway
    HttpClient --> FastAPI
    TelegramGateway -.-> BaseGateway
    TelegramGateway --> TelegramAdapter
    TelegramAdapter --> Cleaner
    TelegramAdapter --> Dedup
    TelegramAdapter --> StandardMessage
    StandardMessage --> Bus
    Bus --> Worker
    Worker --> Orchestrator
    Worker --> Bus
    Bus --> Dispatcher
    Dispatcher --> TelegramGateway
    FastAPI --> Orchestrator

    Orchestrator --> SessionManager
    Orchestrator --> TaskManager
    Orchestrator --> LLM
    Orchestrator --> MatcherService
    Orchestrator --> SQLGen
    Orchestrator --> MemoryWriter
    Orchestrator --> UserPreference
    Orchestrator --> Reranker
    Reranker --> LLMBackend

    SessionManager --> LongTermMemory
    LongTermMemory --> Storage
    MemoryWriter --> Storage
    UserPreference --> Storage

    SQLGen --> SQLVal
    SQLGen --> ASTAnalyzer
    SQLGen --> LLMBackend
    LLM --> LLMBackend

    MatcherService --> EntityMatcher
    MatcherService --> Policy
    MatcherService --> TimeMatcher
    EntityMatcher --> Retriever
    MatcherService --> SchemaLoader
    SchemaLoader --> TablesYaml
    SchemaLoader --> MetricsYaml
    SchemaLoader --> AliasesYaml
    TelegramGateway --> TelegramAPI
```
