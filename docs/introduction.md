---
title: "Introduction"
---

`text2sql-agent` is a **Text2SQL data agent for ClickHouse**. It turns natural-language questions into validated ClickHouse SQL — with deterministic entity resolution, multi-turn query state, a confirmation flow for ambiguous entities, and an AST-level analysis layer that catches drift before any SQL leaves the system.

## How it works

```text
Natural language
  -> LLM intent extraction            (fragments only — never names tables/columns)
  -> Deterministic entity resolution  (IDF-weighted inverted-index recall + fuzzy rerank)
  -> Turn logic                       (new query / follow-up patch / confirmation)
  -> Grounded SQL generation          (ClickHouse dialect; entities pinned by the prompt)
  -> sqlglot validation + AST analysis (read-only, whitelist, entity fidelity, static cost)
  -> ClickHouse SQL
```

The pipeline deliberately separates *understanding* from *assembly*: the LLM never invents entity names — tables, columns, and metrics are resolved deterministically against governed schema metadata, and low-confidence matches are escalated to the user instead of guessed.

## Example session

```text
Q1: Revenue by region for the last 7 days
->  SELECT users.region, sum(orders.amount) AS revenue
    FROM orders JOIN users ON orders.user_id = users.id
    WHERE orders.created_at >= now() - INTERVAL 7 DAY
    GROUP BY users.region ORDER BY revenue DESC LIMIT 100

Q2: only gold members, top 3 per region
->  patch: filters += [users.vip_level = 'gold'], window = {users.region, top 3}
    ... WHERE users.vip_level = 'gold' ...
    GROUP BY users.region ORDER BY revenue DESC LIMIT 3 BY users.region
```

Q2 inherits metric / time / grouping from Q1 — only the deltas are extracted and merged into the structured query state.

## Highlights

- **Deterministic entity resolution** — pluggable recall (IDF-weighted inverted index with typo probing by default) plus alias scoring weighted by alias-source confidence; one decision policy with type-calibrated confirmation bands and a deterministic tie guard
- **Cross-encoder reranking** — constrained LLM final selection, only inside the confirmation band (`RERANKER_ENABLED`)
- **Grounded SQL generation** — the generation prompt pins resolved table/column names, metric expressions, join conditions, and time predicates; the LLM assembles structure only
- **AST post-analysis** — column existence, entity-fidelity assertions, join-key consistency, and static cost estimates, wired into a bounded repair loop
- **Multi-turn as a state machine** — field-level patch merge with explicit/inherited provenance, persisted and restart-recoverable
- **Live evaluation** — 45 golden cases (mocked unit eval + real-LLM live eval via `scripts/live_eval.py`)

## Where to go next

- [Quickstart & API](https://github.com/frankzh0330/text2sql-agent#quick-start) — run the service, call `POST /nl2sql`, environment variables
- [Live demo (Telegram)](/LIVE_DEMO) — end-to-end verification steps over Telegram
- [Architecture](/ARCHITECTURE) — layer responsibilities, dependency direction, scenarios
- [Evaluation](/EVALUATION) — golden-case harness, live eval, known-gap xfail map
- [Memory design](/MEMORY) — session / project / user-preference memory layers
