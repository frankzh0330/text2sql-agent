---
title: "Evaluation Strategy"
---

[Chinese version](https://github.com/frankzh0330/text2sql-agent/blob/master/docs/EVALUATION.zh-CN.md)

This document explains how `text2sql-agent` is evaluated today, what the current end-to-end eval harness covers, and how to expand it safely as the agent evolves.

## Why Evaluation Matters Here

`text2sql-agent` is not just a matcher or a prompt wrapper. It has:

- turn-based follow-up handling
- session persistence
- confirmation flow
- project memory injection
- user preference rerank
- async memory learning

That means many regressions will not show up in isolated unit tests. A bug often appears only when multiple stages interact.

Examples:

- a follow-up is misclassified as a new query
- confirmation resumes but loses the partial query state
- project memory is loaded but not injected into the LLM context
- user preference changes ranking too aggressively
- restart recovery works for session but not for task confirmation

This is why the project needs both:

- unit / integration tests
- end-to-end golden-style eval cases

## Evaluation Layers

### 1. Unit Tests

Purpose:

- verify pure logic with narrow scope

Examples:

- `followup_resolver.py`
- `query_state_merger.py`
- `user_preference_store.py`
- `long_term_memory.py`

Good for:

- deterministic rules
- edge cases
- scoring / merge logic

### 2. Endpoint / Integration Tests

Purpose:

- verify the FastAPI entry path and orchestration logic

Examples:

- [tests/test_app_endpoints.py](https://github.com/frankzh0330/text2sql-agent/blob/master/tests/test_app_endpoints.py)
- [tests/test_session_manager.py](https://github.com/frankzh0330/text2sql-agent/blob/master/tests/test_session_manager.py)
- [tests/test_task_manager.py](https://github.com/frankzh0330/text2sql-agent/blob/master/tests/test_task_manager.py)

Good for:

- confirmation flow
- session/task persistence
- explain payloads
- follow-up patch execution

### 3. End-to-End Eval Harness

Purpose:

- verify full query behavior from request to final agent outcome
- express realistic multi-turn scenarios in data rather than one test function at a time

Main files:

- [tests/evals/nl2sql_cases.yaml](https://github.com/frankzh0330/text2sql-agent/blob/master/tests/evals/nl2sql_cases.yaml)
- [tests/test_end_to_end_evals.py](https://github.com/frankzh0330/text2sql-agent/blob/master/tests/test_end_to_end_evals.py)

This harness is intentionally closer to “golden cases” than pure unit testing.

## Current Case Inventory (45 cases, 9 groups)

Cases in [tests/evals/nl2sql_cases.yaml](https://github.com/frankzh0330/text2sql-agent/blob/master/tests/evals/nl2sql_cases.yaml) are grouped by capability:

| Group | Count | Covers |
|---|---|---|
| `s*` single table | 7 | aggregation, alias hits (gmv/aov), time defaults |
| `j*` joins | 6 | auto-join for dimension columns, multi-join |
| `m*` multi-hop joins | 3 | payments→orders→users style paths (known gaps, see below) |
| `t*` time expressions | 6 | last week/month/quarter, today, time column on non-orders tables |
| `f*` filters | 5 | enum value normalization (`credit card` → `credit_card`), hallucinated columns |
| `a*` ambiguity | 3 | same-named columns, low-confidence tables |
| `w*` window / TopN | 3 | global top-K vs per-group ranking (`LIMIT n BY`) |
| `u*` follow-ups | 9 | time/metric/group/filter/window patches, confirmation, restart recovery, new-topic rejection, memory injection |
| `b*` negative | 3 | unknown metric/group-by, no-extraction |

### Strict xfail = known-gap map

Cases whose *correct* behavior is asserted but not yet implemented carry an `xfail` field (strict). A fix flips them to failure until the marker is removed, so the gap list can never silently rot. Current gaps (5): multi-hop join inference (`m01-03`), silently dropped unknown group-by (`b01`), unsupported time expressions falling back silently (`t05`). Closed so far: `b02` (per-type thresholds), `a01/a02` (exact-alias collisions now surface as confirmation, or resolve deterministically via join distance when a base-table context exists).

## Live Eval (real LLM, no mocks)

[scripts/live_eval.py](https://github.com/frankzh0330/text2sql-agent/blob/master/scripts/live_eval.py) reuses the same YAML expectations but runs the real LLM for extraction and SQL generation — measuring what the mocked pytest eval cannot: extraction quality and SQL quality. Cases that require a forced mock are skipped automatically.

```bash
./.venv311/bin/python scripts/live_eval.py            # all runnable cases
./.venv311/bin/python scripts/live_eval.py -k s0 -k j0
./.venv311/bin/python scripts/live_eval.py --out eval_results/run.json
```

Latest run (current code, zhipu backend, 2026-10-04): **36/36 regular cases pass, 34/34 generated SQL valid (sqlglot), 33/33 metric expressions faithfully used, avg latency ~7.6s**; 4 cases that need a forced mock were skipped (f05, a03, u06, u07), and the 5 known-gap cases still fail as expected. LLM output varies between runs, so treat this as one sample.

This run fixes a regression that appeared after the catalog split (31/36 at the time): 5 regular cases stopped at `needs_confirmation` because real LLM extraction fragments differ from the ideal fragments in the mocks. The fix has two parts:

- matcher: the query and aliases are plural-folded before exact lookup and scoring, so `product categories` exact-hits `product category` (w01)
- alias table: a new curated alias `orders → order_count` covers extractions that use the bare word `orders` as the metric (s03, t03, f02, f04)

## Current E2E Eval Format

Each case is YAML-driven and may include:

- `setup`
- one or more `steps`
- expected status, turn mode, and resolved-intent fields

Example shape:

```yaml
cases:
  - name: u06_followup_confirmation_flow
    setup:
      last_query_state:
        project_id: 55
        tables: ["orders"]
        metrics: ["revenue"]
        time_range: {type: last_n_days, n: 7}
        group_by: ["orders.channel"]
        filters: []
        turn_type: new_query
    steps:
      - text: Compare with the products table
        extraction:
          table_extractions: ["products table"]
        resolver: low_confidence_table
        expect:
          status: needs_confirmation
          turn_mode: followup_patch
          candidates_contains: tables
      - text: "1"
        expect:
          status: success
          turn_mode: confirmation
          resolved_intent:
            tables: ["products"]
            metrics: ["revenue"]
```

## What the Harness Can Simulate Today

The current runner supports:

- `setup.last_query_state`: a pre-seeded previous query state (multi-turn precondition)
- `setup.project_memory`: pre-seeded project memory entries, paired with `assert_memory_contains` to assert the memory really reached the extraction context
- `extraction`: mocked extraction output (`SQLIntentJson` fragments, i.e. what an ideal extractor emits)
- `resolver` scenarios: `real` (the real `MatcherService` over the three `catalog/` sources) and `low_confidence_table` (real service + fake table matcher that reliably lands in the 55-point confirmation band)
- mocked SQL generation (the harness pins the deterministic core:
  matchers, thresholds, state merging, confirmation flow, persistence)
- multi-step session continuity
- `restart_before: true` for restart simulation (session / task managers are rebuilt and recovered from disk)
- case-level markers: `xfail` (strict known gap) and `live_skip` (skipped by the live eval because it needs a forced mock extraction)
- `expect` assertions:
  - `status` / `status_not`
  - `turn_mode`
  - `message_contains`
  - `candidates_contains`
  - `resolved_intent`: `tables`, `metrics`, `group_by`, `group_by_contains`, `time_n`, `time_type`, `order_direction`, `order_limit`, `window_group`, `window_limit`, `filter_column`, `filter_value`
  - SQL-generation inputs: `time_expr_contains` (time expression) and `sql_intent_contains_join` (inferred join)

## Current Covered Scenarios

See [Current Case Inventory](#current-case-inventory-45-cases-9-groups) for the full list. The most important "agent-like" paths covered are:

- new queries (explicit table; table inferred from a metric) and join inference from schema config
- follow-up patches for time, metric, group-by, filter and grouped-ranking window
- confirmation flow, including confirmation after restart
- ambiguity handling (alias collisions, low-confidence tables) and refusing to guess hallucinated columns
- project memory context injection

This means the harness already protects the most important “agent-like” paths.

## Why YAML-Driven Evals Help

Without a case file, every new scenario becomes another hand-written test function.

With YAML-driven evals:

- adding a case is cheap
- reviewing scenario coverage is easier
- product/behavior changes are easier to discuss in data form
- future replay against real logs becomes more natural

This is especially useful for turn-based systems, where the correctness lives in the sequence, not just one isolated function call.

## Recommended Next Eval Buckets

### 1. More Turn-Based Cases

Examples:

- "Not revenue; use payment amount"
- "Break it down by seller region instead"
- "Compare with yesterday"
- "Continue with order count"

### 2. Memory Cases

Already covered: u09 project memory injected into the extraction context; unit tests cover per-query, entry-level selection and keeping personal preferences out of project memory. Still worth adding:

- project memory changes a default metric or filter mapping
- project memory changes default region behavior
- conflicting memory pieces and relevance selection

### 3. User Preference Cases

Already covered (unit / endpoint tests, not yet in YAML): preference rerank reorders confirmation candidates, and preference cannot boost a candidate over the accept line or the confirm floor. Still worth adding:

- preference remains scoped by `project_id + user_id` (a cross-user isolation e2e case)
- preference does not override obviously better semantic matches
- move these scenarios into YAML cases (needs `user_id` and preference seeding in the harness)

### 4. Restart / Recovery Cases

Already covered: u07 confirmation after restart. Still worth adding:

- follow-up after restart
- session restored but no pending task

## What E2E Eval Is Not Meant To Do

The current harness is not meant to:

- measure real LLM extraction / SQL quality (that is the job of `scripts/live_eval.py`)
- verify actual downstream query correctness against production databases
- replace matcher unit tests

It is mainly a regression harness for agent behavior and orchestration.

## Running Evals

Run only the end-to-end harness (set `EVAL_IGNORE_XFAIL=1` to see the real status of known-gap cases):

```bash
./.venv311/bin/pytest -q tests/test_end_to_end_evals.py
```

Run it together with the main turn-based suite:

```bash
./.venv311/bin/pytest -q \
  tests/test_end_to_end_evals.py \
  tests/test_app_endpoints.py \
  tests/test_followup_resolver.py \
  tests/test_query_state_merger.py \
  tests/test_session_manager.py \
  tests/test_task_manager.py
```

## Expansion Guidelines

When adding a new eval case:

1. Prefer YAML when the new behavior is mostly a scenario, not a new algorithm.
2. Add unit tests as well if you introduce new pure logic.
3. Keep the expected assertion focused on stable fields.
4. Avoid asserting full SQL strings unless necessary.
5. Prefer semantic assertions over surface formatting assertions.

## Long-Term Direction

The long-term goal is to evolve this from a small YAML harness into a richer replay-and-regression layer:

- more golden cases from real query logs
- category labels for cases
- optional offline scoring reports
- comparison between branches or model settings

But even the current lightweight harness already gives strong protection for the project’s most important turn-based and memory-heavy paths.
