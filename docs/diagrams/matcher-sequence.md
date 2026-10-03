---
title: "Matcher Sequence"
---

```mermaid
sequenceDiagram
    participant Server as server.py lifespan
    participant Loader as schema_loader
    participant Files as catalog/*.yaml
    participant ORC as QueryOrchestrator
    participant MS as MatcherService
    participant EM as EntityMatcher (table / column / metric)
    participant Ret as Retrievers
    participant Pref as UserPreferenceStore
    participant LLM as reranker (optional)

    rect rgb(227, 242, 253)
    Note over Server,EM: Initialization - merge metadata sources, build indexes (once at startup)
    Server->>MS: MatcherService(catalog_path)
    MS->>Loader: load_sql_schema()
    Loader->>Files: tables.yaml (physical catalog) / metrics.yaml (semantic layer) / aliases.yaml (alias table)
    Files-->>Loader: one source adapter per file
    Loader-->>MS: SQLSchema (qualified columns + aliases with confidence), references validated
    MS->>EM: EntityMatcher(tables / columns / metrics)
    EM->>Ret: LexicalRetriever builds token -> entity index
    end

    rect rgb(232, 245, 233)
    Note over ORC,LLM: Runtime - resolve one extracted field
    ORC->>MS: resolve_with_candidates(type, extractions, base_table, bias)
    MS->>EM: match(first extraction text)
    alt Exact alias
        EM-->>MS: candidates (score = 100 × alias confidence)<br/>or exact_alias_collision with all entities
    else No exact alias
        EM->>Ret: retrieve(query, k) per retriever, union
        Ret-->>EM: candidate entity names
        EM->>EM: score = max(alias similarity × alias confidence)
        EM-->>MS: ranked candidates
    end
    MS->>Pref: bias(candidates) — weak, bounded (≤ +6)
    Pref-->>MS: reordered candidates
    MS->>MS: collision → join-distance resolve or confirm<br/>banding from matcher/policy.py on the raw recall score (metric 90 / table·column 80,<br/>confirm floor 40); tie margin 10 on the biased score
    MS-->>ORC: ResolvedResult (accept / confirm / no_match)
    opt needs_confirmation and RERANKER_ENABLED
        ORC->>LLM: rerank existing candidates
        LLM-->>ORC: auto-accept clear winner, else keep confirmation
    end
    end

    rect rgb(255, 243, 224)
    Note over ORC,MS: Table and join inference
    ORC->>MS: infer_main_table (explicit / metric's view / column votes)
    ORC->>MS: infer_joins(base_table, qualified_columns)
    MS-->>ORC: join steps from the semantic-layer join graph, or missing path
    end
```
