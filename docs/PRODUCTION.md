---
title: "Demo vs Production"
---

The demo deliberately stops at **NL → validated ClickHouse SQL** and runs on checked-in metadata. A production
deployment needs more on two fronts: where the metadata comes from, and what protects the warehouse once SQL is
actually executed. Future features (user history, smarter memory selection) are on the [Roadmap](/ROADMAP) page.

## Metadata: YAML files vs a real catalog

In the demo, three YAML files stand in for three independently owned systems, each read by its own source adapter
and merged into one `SQLSchema` at startup (details in [Extraction & Resolution](/MATCHER#catalog-and-metadata)).

| | Demo | Production |
|---|---|---|
| Physical schema | `catalog/tables.yaml`, hand-written | Unity Catalog / DataHub / OpenMetadata API: tables, columns, types, comments, owners, row-count statistics |
| Metric definitions and joins | `catalog/metrics.yaml` | Semantic layer (LookML, dbt MetricFlow, Cube), owned by the data team |
| Aliases | `catalog/aliases.yaml` | Alias table in a database, fed by glossary sync, comment extraction, query-log mining and reviewed LLM suggestions |
| Freshness | Loaded once at startup; a change needs a restart | Versioned snapshots: change events as the trigger, periodic pull as fallback; rebuild the index in the background and swap it atomically |
| Failure mode | A bad YAML fails fast at startup | Keep a last-known-good snapshot so the service still starts when the catalog is down |
| Scale | 6 tables, 31 columns, 14 metrics | Tens of thousands of entities: per-project index cache, embedding recall next to the lexical index |
| Access | Every entity is visible | Only entities the user is granted are indexed or shown, following catalog permissions |
| Traceability | — | Log the `schema_version` with every query, so a wrong answer can be reproduced |

What already carries over: one adapter per source, a merged `SQLSchema` that downstream code never sees as files,
and the `set_matcher_service()` seam for a hot swap. Production replaces the adapters, not the matcher.

## Guardrails: static checks vs a defended warehouse

The demo never executes SQL, so all of its guardrails are static: they check the query text, not its effect
(details in [Generation & Validation](/SQL_PIPELINE#layer-4-validation)).

| | Demo (implemented) | Production (needed) |
|---|---|---|
| Write protection | Parser accepts one `SELECT` / `WITH` statement only | Also a read-only database account and `readonly` setting: the parser is one layer, not the only one |
| Table access | Whitelist of tables in the schema | Per-user grants: row policies for row-level security, column masking for PII |
| Result size | `LIMIT 100` injected when missing | Server-side caps such as `max_result_rows`, `max_rows_to_read`, `max_bytes_to_read` |
| Cost | Static estimate from catalog row counts; warnings only (large scan over 10M rows, unfiltered fact table over 1M rows, more than 3 joins, subquery depth over 2) | A real pre-execution check (`EXPLAIN ESTIMATE`) that blocks or asks for confirmation above a budget; `max_execution_time` timeouts; per-user quotas and rate limits |
| Semantic correctness | AST checks: columns exist, joins match the declared graph, resolved tables, metric expressions and filters must appear in the SQL; at most 2 repair rounds | Same checks, plus a verified-query library and the golden eval as a release gate for every alias, prompt or model change |
| LLM exposure | Only phrases and schema metadata reach the model; names are pinned, so user text cannot inject a table or column | Same principle, plus a pinned model version, no row data or PII in prompts, and a fallback when the model is unavailable |
| Audit | Resolution trace in logs and `explain` | Durable audit log: user, question, resolved intent, SQL, `schema_version`, rows read, latency |

The design choice that makes this upgrade incremental: guardrails are deterministic and independent of the LLM, so
production adds execution-time layers around them instead of rewriting them.
