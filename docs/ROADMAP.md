---
title: "Roadmap"
---

Everything on this page is planned, not implemented. What it takes to run the current system in production (a real catalog instead of YAML, execution-time guardrails) is on [Demo vs Production](/PRODUCTION). The [How It Works](/ARCHITECTURE) pages describe the current system only; what memory does today is listed under "What Is Already Implemented" on the [Memory](/MEMORY) page.

## User History Layer

A future production version can add a dedicated `UserHistoryService` around:

- query history
- user aliases
- user preferences
- user patterns

The intended storage split is:

- PostgreSQL for durable query history, user aliases, and preference records
- Redis for hot user context and preference caches
- optional vector database for semantic memory retrieval, few-shot selection, and ambiguous-rule recall

### Future Data Models

The likely future model set is:

- `UserPreferences`: default metric, table, filter values, or time range
- `UserAlias`: user-defined phrases mapped to canonical tables, columns, or metrics
- `UserPattern`: aggregated top tables, metrics, columns, and query frequency
- `QueryHistory`: successful and failed query traces for replay, learning, and evaluation

Example of a `UserAlias`: “大单” (big orders) -> `orders.amount > 1000` filter.

### Where User Context Should Apply

User history should not replace the resolver. It should apply at controlled
points:

- before LLM extraction: inject selected aliases, defaults, and personalized few-shot examples
- after matcher recall, before the accept/confirm decision: apply a weak, bounded bias from user patterns
- after successful queries: save history asynchronously and update aggregated patterns

### Important Constraint

Personalization should stay bounded by explicit scope:

```text
project_id + user_id
```

The priority order should remain:

```text
explicit user input
  > session state
  > project memory
  > user history / preference signals
```

This prevents historical behavior from silently overriding a clearer current
query or a project-level business rule.

## Smarter Long-Term Memory Selection

Short-term query state is already structured, so it needs no AI summarization (see [Memory](/MEMORY)). Long-term project memory is different; it keeps growing:

- project rules
- correction rules
- caveats

As it grows, the system may later need smarter selection for:

- memory snippet choice
- few-shot example choice
- ambiguous explanation generation

So “AI retrieval/summarization does not apply” only holds for short-term structured turn state, not for memory as a whole.

## Recommended Next Steps

1. add category-aware project memory retrieval
2. split `UserAlias`, `UserPattern`, and `UserPreferences` out of simple counts, to take over the personal preferences the judge no longer writes
3. add persisted `QueryHistory` for replay, pattern aggregation, and failure analysis
4. consider a hybrid Markdown + embedding index for long-term project memory retrieval
5. add more project memory / user preference eval cases
6. add a conflict policy for when project memory and user preference disagree
7. move file writes to external storage or file locks for multi-process deployments
