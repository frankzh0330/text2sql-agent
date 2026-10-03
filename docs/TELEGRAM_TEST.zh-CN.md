# Telegram 测试说明

[English](TELEGRAM_TEST.md) | [简体中文](TELEGRAM_TEST.zh-CN.md)

## 启动服务

### 1. 设置环境变量

```bash
# 必需：Telegram Bot Token
export TELEGRAM_BOT_TOKEN="your_bot_token_here"

# LLM 配置
export LLM_BACKEND="zhipu"  # 或 "ollama"
export ZHIPU_API_KEY="your_zhipu_key"  # 如果用 zhipu
export OLLAMA_BASE_URL="http://localhost:11434/v1"  # 如果用 ollama
```

### 2. 启动服务

```bash
python server.py
```

## 测试流程

1. **向 Telegram Bot 发送消息**，例如：
   - `Revenue by region for the last 7 days`
   - `Only paid orders`
   - `Top 3 per region`

   demo schema 只有英文别名，请用英文提问。

2. **Bot 会返回生成的 SQL 与解析意图**：

   ```
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

   说明：查询执行与结果渲染刻意不在本 Demo 范围内——Bot 只返回校验后的 SQL
   （见 README 的 Production Notes）。

3. **表名或指标有歧义时，Bot 会发起确认**：

   ```
   Please choose a table:
     1. products (match 55%)
     2. orders (match 48%)
   Reply with a number or a name.
   ```

   回复 `1`（或名称）即可继续；待确认任务支持重启后恢复。

## 调试

### API 文档

```text
http://localhost:8000/docs
```

### 查看会话列表

```bash
curl http://localhost:8000/sessions
```

### 直接测试 Text2SQL API

```bash
curl -X POST http://localhost:8000/nl2sql \
  -H "Content-Type: application/json" \
  -d '{"text": "Revenue by region for the last 7 days", "project_id": 55}'
```

## 常见问题

1. Telegram Bot 无响应。

- 检查 `TELEGRAM_BOT_TOKEN` 是否正确。
- 检查服务日志中的错误信息。

2. LLM 调用失败。

- 检查 `LLM_BACKEND`。
- 如果用 zhipu，检查 `ZHIPU_API_KEY`。

3. 记忆文件不可见。

- 运行时数据存储在配置的 `data/` 目录下。
- 检查 `data/memory/`、`data/sessions/`、`data/user_preferences/`。
