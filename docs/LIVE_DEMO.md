---
title: "Live Demo"
---

[Chinese version](https://github.com/frankzh0330/text2sql-agent/blob/master/docs/LIVE_DEMO.zh-CN.md)

Five messages in one Telegram chat show the main behaviours: the table is inferred from the metric, follow-ups
patch the previous query instead of starting over, and an ambiguous question gets a short choice instead of a guess.
Each answer takes about 8–11 seconds (two LLM calls). The bot returns validated ClickHouse SQL only; running the
query is out of scope for this demo.

The demo schema only has English aliases, so ask in English.

## Walkthrough

### 1. Revenue by region for the last 7 days

The question names no table. The table (`orders`) is inferred from the metric `revenue`, the join to `users` is
added for `region`, and the 7-day time filter is applied.

![Step 1: Revenue by region for the last 7 days, and the generated SQL](images/demo/step1-revenue-by-region.webp)

### 2. What about yesterday?

A follow-up: only the time range changes. The metric, the table and the grouping are inherited from step 1.

![Step 2: What about yesterday? Only the time range changes](images/demo/step2-yesterday.webp)

### 3. Only gold members

Another follow-up: a filter (`users.vip_level = 'gold'`) is added on top of the previous query.

![Step 3: Only gold members adds a vip_level filter](images/demo/step3-gold-members.webp)

### 4. Top 3 per region

Window ranking: the query becomes `LIMIT 3 BY users.region`, ClickHouse's grouped top-N syntax.

![Step 4: Top 3 per region](images/demo/step4-top3-per-region.webp)

### 5. Total amount by channel last month

A new question with an ambiguous word: "amount" could mean the order amount (`revenue`) or the payment amount
(`payment_amount`). Both scores are close and below the acceptance line, so the bot lists the candidates and asks
instead of guessing. Reply `payment`; it then writes SQL on `payments`, joined to `orders` for the channel.

Reply by name rather than by number: the order of the options can change between runs.

![Step 5: the bot asks which metric 'amount' means; replying payment produces SQL on payments](images/demo/step5-total-amount-confirm.webp)

### Tips for running it live

- Send all five messages within an hour: a session idle for more than 60 minutes is cleaned up, and follow-ups then
  have no previous query to build on.
- Confirming teaches the system: after step 5, the memory writer may save a rule such as "amount → payment_amount" in
  `data/memory/project_55/auto_learned.md`, and the next run may skip the question. Check that file before a demo.
- To see which rule produced each result (for example `table (not given) → orders · inferred_from_metric revenue`),
  start the server with `TRACE_IN_REPLY=true`; the trace is appended to every reply.

## Run It Yourself

### 1. Set Environment Variables

```bash
# Required: Telegram Bot token
export TELEGRAM_BOT_TOKEN="your_bot_token_here"

# LLM configuration
export LLM_BACKEND="zhipu"  # or "ollama"
export ZHIPU_API_KEY="your_zhipu_key"  # when using zhipu
export OLLAMA_BASE_URL="http://localhost:11434/v1"  # when using ollama

# Optional: append the resolution trace to each Telegram reply
export TRACE_IN_REPLY=true
```

### 2. Run

```bash
python server.py
```

Then send the five messages above to your bot, in one chat.

## Debugging

### API Docs

```text
http://localhost:8000/docs
```

### List Sessions

```bash
curl http://localhost:8000/sessions
```

### Test The Text2SQL API Directly

```bash
curl -X POST http://localhost:8000/nl2sql \
  -H "Content-Type: application/json" \
  -d '{"text": "Revenue by region for the last 7 days", "project_id": 55}'
```

The response includes `explain.trace`, the same resolution trace as above.

## Troubleshooting

1. Telegram bot does not respond.

- Check whether `TELEGRAM_BOT_TOKEN` is correct.
- Check service logs for errors.

2. LLM calls fail.

- Check `LLM_BACKEND`.
- If using zhipu, check `ZHIPU_API_KEY`.

3. A follow-up such as "What about yesterday?" returns "You have not entered any table, metric or query condition".

- The previous turn is gone, usually because the session was idle for more than 60 minutes. Ask the full question
  again. The trace shows `turn new_query (no_previous_state)` in this case.

4. Memory files are not visible.

- Runtime memory is stored under the configured `data/` directory.
- Check `data/memory/`, `data/sessions/`, and `data/user_preferences/`.
