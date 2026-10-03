---
title: "Telegram Testing Guide"
---

[Chinese version](https://github.com/frankzh0330/text2sql-agent/blob/master/docs/TELEGRAM_TEST.zh-CN.md)

## Start The Service

### 1. Set Environment Variables

```bash
# Required: Telegram Bot token
export TELEGRAM_BOT_TOKEN="your_bot_token_here"

# LLM configuration
export LLM_BACKEND="zhipu"  # or "ollama"
export ZHIPU_API_KEY="your_zhipu_key"  # when using zhipu
export OLLAMA_BASE_URL="http://localhost:11434/v1"  # when using ollama
```

### 2. Run

```bash
python server.py
```

## Test Flow

1. Send a message to the Telegram bot, for example:

- `Revenue by region for the last 7 days`
- `Only paid orders`
- `Top 3 per region`

The demo schema only has English aliases, so ask in English.

2. The bot should return the generated SQL and resolved intent:

```text
📄 ClickHouse SQL generated
Table: orders
Metrics: revenue
Group by: users.region
Time: last_n_days n=7
📊 Est. scan ~80M rows · join x1 · ✅ No warnings

SELECT users.region AS region, sum(orders.amount) AS revenue
FROM orders JOIN users ON orders.user_id = users.id
WHERE orders.created_at >= now() - INTERVAL 7 DAY
GROUP BY users.region ORDER BY revenue DESC LIMIT 100
```

Note: query execution and result rendering are intentionally out of scope for
this demo — the bot returns the validated SQL only (see Production Notes in the README).

3. If a table or metric is ambiguous, the bot will ask for confirmation:

```text
Please choose a table:
  1. products (match 55%)
  2. orders (match 48%)
Reply with a number or a name.
```

Reply `1` (or the name) to continue; the pending confirmation survives restarts.

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

## Troubleshooting

1. Telegram bot does not respond.

- Check whether `TELEGRAM_BOT_TOKEN` is correct.
- Check service logs for errors.

2. LLM calls fail.

- Check `LLM_BACKEND`.
- If using zhipu, check `ZHIPU_API_KEY`.

3. Memory files are not visible.

- Runtime memory is stored under the configured `data/` directory.
- Check `data/memory/`, `data/sessions/`, and `data/user_preferences/`.
