---
title: "Generation & Validation"
---

This page expands Layers 3 and 4 of the [Text2SQL pipeline](/ARCHITECTURE): grounded SQL generation from the names already resolved in [Extraction & Resolution](/MATCHER), the deterministic validation guardrails, and the explain/trace information used to debug a turn.

## Layer 3: SQL Generation

Owned by [service/sql_generator.py](https://github.com/frankzh0330/text2sql-agent/blob/master/service/sql_generator.py).

Responsibilities:

- assemble the generation prompt from resolved entities: base table, metric expressions,
  qualified columns, filters, time predicate (ClickHouse syntax), join conditions, window/order intent
- window-intent recovery: when L1 truncates the window fragment (e.g. extracts only "in each region", leaving "top 3" in the original sentence), the parser retries on the full query text — recovering limit and group deterministically (recorded in explain as `recovered_from_full_text`)
- call the LLM to produce one ClickHouse SELECT; entity names are pinned by the prompt, the LLM
  assembles structure only (GROUP BY / JOIN / `LIMIT n BY` grouped ranking)
- repair loop: failed validation feeds the error back into the next round (max 2 extra rounds)

## Layer 4: Validation

Owned by [service/sql_validator.py](https://github.com/frankzh0330/text2sql-agent/blob/master/service/sql_validator.py) (sqlglot, `dialect="clickhouse"`) and
[service/sql_ast_analyzer.py](https://github.com/frankzh0330/text2sql-agent/blob/master/service/sql_ast_analyzer.py).

Responsibilities:

- single read-only statement (SELECT / WITH only)
- all table references must be in the schema whitelist
- default LIMIT injection
- AST analysis: column existence (after sqlglot alias qualification), join edges and ON keys must match the join graph declared in the semantic layer, no JOIN without ON
- entity fidelity against semantic drift: resolved tables, metric expressions and filter predicates must appear in the SQL
- per-group ranking ("top 3 per region") must be `LIMIT n BY <group>`; a plain `LIMIT n` is a global top-N, valid SQL with a different meaning, so it is rejected and repaired
- static cost warnings (estimated scan rows from catalog row counts, unfiltered full scans on fact tables, join depth), recorded in explain only (`explain.resolver_explain.sql_generation.ast_analysis`)
- analysis errors feed the same repair loop as validation errors; warnings never block
- deterministic guardrails, independent of the LLM

## Validation and Debugging

The system exposes rich explain/debug information:

- `resolver_explain`
- `turn_explain`
- candidate scores
- follow-up decision signals
- patch fields
- confirmed fields
- timing for major layers

This is important because the project is closer to an agent than a one-shot translator.

### Resolution trace

Every request produces a resolution trace ([service/resolution_trace.py](https://github.com/frankzh0330/text2sql-agent/blob/master/service/resolution_trace.py)):
one line per resolved field saying which phrase it came from, which rule produced it, and the key evidence. It is
built from the final response (no re-resolution), logged at INFO as `[trace <session_id>] ...`, and returned as
`explain.trace`. Telegram replies include it only when `TRACE_IN_REPLY=true` (off by default).

```text
extract   metric=['revenue'] group_by=['region'] time=['last 7 days']
turn      new_query (no_previous_state)
metric    'revenue' → revenue · exact · alias 'revenue' · 100
table     (not given) → orders · inferred_from_metric revenue (declared view)
group_by  'region' → users.region · exact_collision_distance_resolved · alias 'region' · 100
time      'last 7 days' → last_n_days n=7 · regex_match
joins     orders→users ✓
result    success
```

Follow-ups show patched vs inherited fields, confirmations show what the user picked, early exits show why
(e.g. `no_previous_state`), and a missing join shows as `payments→users ✗ no direct join`.
