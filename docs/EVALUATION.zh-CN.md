# 评估策略

[English](EVALUATION.md) | [简体中文](EVALUATION.zh-CN.md)

本文说明 `text2sql-agent` 当前如何做评估、现有 end-to-end eval harness 覆盖了什么，以及后续应该如何安全扩展。

## 为什么这个项目必须做评估

`text2sql-agent` 已经不只是一个 matcher 或 prompt wrapper。它现在有：

- turn-based follow-up handling
- session persistence
- confirmation flow
- project memory injection
- user preference rerank
- async memory learning

这意味着很多回归并不会出现在单个函数的 unit test 里，而是只会在多层逻辑接起来之后暴露。

例子：

- follow-up 被误判成 new query
- confirmation 恢复了，但 partial query state 丢了
- project memory 读到了，但没有真正注入 LLM context
- user preference 权重过大，把候选排序带偏
- session 恢复了，但 task confirmation 没恢复

所以这个项目需要两类评估同时存在：

- unit / integration tests
- end-to-end golden-style eval cases

## 评估分层

### 1. Unit Tests

目的：

- 验证范围很窄的纯逻辑

例子：

- `followup_resolver.py`
- `query_state_merger.py`
- `user_preference_store.py`
- `long_term_memory.py`

适合测：

- 确定性规则
- 边界情况
- merge / scoring 逻辑

### 2. Endpoint / Integration Tests

目的：

- 验证 FastAPI 入口和编排逻辑

例子：

- [tests/test_app_endpoints.py](../tests/test_app_endpoints.py)
- [tests/test_session_manager.py](../tests/test_session_manager.py)
- [tests/test_task_manager.py](../tests/test_task_manager.py)

适合测：

- confirmation flow
- session/task persistence
- explain payload
- follow-up patch 执行路径

### 3. End-to-End Eval Harness

目的：

- 从请求到 agent 最终行为，验证整条链路是否符合预期
- 用数据驱动方式表达真实多轮场景，而不是每次都手写一条测试函数

主要文件：

- [tests/evals/nl2sql_cases.yaml](../tests/evals/nl2sql_cases.yaml)
- [tests/test_end_to_end_evals.py](../tests/test_end_to_end_evals.py)

这套 harness 更接近“golden cases”，不是单纯 unit test。

## 用例清单（45 条，9 组）

[tests/evals/nl2sql_cases.yaml](../tests/evals/nl2sql_cases.yaml) 按能力分组：

| 组 | 数量 | 覆盖 |
|---|---|---|
| `s*` 单表 | 7 | 聚合、别名命中（gmv/aov）、时间默认 |
| `j*` join | 6 | 维表列自动 join、多重 join |
| `m*` 多跳 join | 3 | payments→orders→users 型路径（已知边界） |
| `t*` 时间表达 | 6 | last week/month/quarter、today、非 orders 表时间列 |
| `f*` 过滤 | 5 | 枚举值归一（credit card → credit_card）、幻觉列 |
| `a*` 歧义 | 3 | 同名列、低置信表名 |
| `w*` 窗口/TopN | 3 | 全局 TopK vs 分组排名（LIMIT n BY） |
| `u*` follow-up | 9 | 时间/指标/分组/过滤/窗口 patch、确认、重启恢复、新话题不误判、记忆注入 |
| `b*` 负例 | 3 | 未知指标/分组列、无抽取信号 |

### strict xfail = 已知边界地图

断言"正确行为"但当前未实现的用例带 `xfail` 字段（strict）。修复后自动翻红提醒移除标记，边界清单不会悄悄烂掉。当前 5 个：多跳 join 推断（m01-03）、未知 group_by 静默丢弃（b01）、不支持的时间表达静默回退（t05）。已关闭：b02（按类型阈值）、a01/a02（exact 别名冲突现在暴露为确认流，或有基表上下文时按 join 距离确定性消歧）。

## Live Eval（真实 LLM，无 mock）

[scripts/live_eval.py](../scripts/live_eval.py) 复用同一套 YAML 断言，但抽取与 SQL 生成走真实 LLM——度量 mock 测不到的抽取质量与 SQL 质量；依赖强制 mock 的用例自动跳过。

```bash
./.venv311/bin/python scripts/live_eval.py            # 全部可跑用例
./.venv311/bin/python scripts/live_eval.py --out eval_results/run.json
```

最近一轮（当前代码，zhipu 后端，2026-10-04）：**常规 36/36 通过、SQL 语法 34/34、指标口径保真 33/33、平均延迟 ~7.6s**；4 条依赖强制 mock 的用例跳过（f05、a03、u06、u07），5 条已知缺口用例如预期失败。LLM 输出每次有波动，这只是一次采样。

这一轮修复了元数据拆分后出现的回归（当时 31/36）：5 条常规用例都停在 `needs_confirmation`，原因是真实 LLM 的抽取片段与 mock 中的理想片段不同。修复分两处：

- matcher：query 与别名在单复数折叠后再做 exact / 打分，`product categories` exact 命中 `product category`（w01）
- alias 表：新增 curated 别名 `orders → order_count`，覆盖把裸词 `orders` 抽成指标的说法（s03、t03、f02、f04）

## 当前 E2E Eval 格式

每条 case 使用 YAML 描述，可包含：

- `setup`
- 一个或多个 `steps`
- 预期的 `status / turn_mode / resolved_intent 字段`

结构示例：

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

## Harness 现在能模拟什么

当前 runner 已支持：

- `setup.last_query_state`：预置上一轮查询状态（多轮前置条件）
- `setup.project_memory`：预置项目记忆条目，配合 `assert_memory_contains` 断言记忆确实注入了抽取上下文
- `extraction`：mocked 抽取输出（`SQLIntentJson` 片段，即理想抽取器应产出的内容）
- `resolver` 场景：`real`（真实 `MatcherService` + `catalog/` 三源元数据）、`low_confidence_table`（真实 service + 假表 matcher，稳定复现 55 分确认带）
- mocked SQL 生成（harness 锁定确定性内核：matcher、阈值、状态合并、确认流、持久化）
- 多步 session 连续性
- `restart_before: true` 重启模拟（重建 session / task manager 后从磁盘恢复）
- 用例级标记：`xfail`（strict 已知缺口）、`live_skip`（live eval 跳过，依赖强制 mock 抽取）
- `expect` 断言：
  - `status` / `status_not`
  - `turn_mode`
  - `message_contains`
  - `candidates_contains`
  - `resolved_intent`：`tables`、`metrics`、`group_by`、`group_by_contains`、`time_n`、`time_type`、`order_direction`、`order_limit`、`window_group`、`window_limit`、`filter_column`、`filter_value`
  - SQL 生成入参：`time_expr_contains`（时间表达式）、`sql_intent_contains_join`（推断出的 join）

## 当前已覆盖场景

完整清单见上面的用例清单。最重要的"agent 化"链路包括：

- 新查询（显式表名 / 从指标推断表名）与基于 schema 配置的 join 推断
- 时间、指标、分组、过滤、分组排名窗口的 follow-up patch
- 确认流，包括重启后的确认恢复
- 歧义处理（别名冲突、低置信表名），以及不猜测幻觉列
- 项目记忆注入

这意味着，最重要的“agent 化”链路现在已经有了回归保护。

## 为什么 YAML 驱动 eval 很有用

如果没有 case 文件，每加一个场景都要再写一条专门的测试函数。

有了 YAML 驱动 eval 以后：

- 加 case 成本更低
- 回看覆盖面更容易
- 讨论行为变化时更接近产品场景
- 未来接真实日志 replay 也更自然

这对 turn-based 系统尤其重要，因为正确性往往存在于“多步序列”里，而不是某一个孤立函数调用里。

## 建议下一步扩的 Eval 桶

### 1. 更多 Turn-Based 场景

例如：

- "Not revenue; use payment amount"（换指标）
- "Break it down by seller region instead"（换分组）
- "Compare with yesterday"（对比）
- "Continue with order count"（续查）

### 2. Memory 场景

已覆盖：u09 项目记忆注入抽取上下文；单测覆盖按 query 的条目级选择、个人偏好不写入项目记忆。还可以补：

- project memory 改变默认指标或过滤映射
- project memory 改变默认 region 行为
- 多条 memory 冲突时的相关片段选择

### 3. User Preference 场景

已覆盖（单测 / endpoint 测试，尚未进 YAML）：preference rerank 改变确认候选顺序、偏好不能把未过采纳线或下限的候选加分过线。还可以补：

- preference 严格限制在 `project_id + user_id`（跨用户隔离的 e2e case）
- preference 不应该覆盖明显更强的语义匹配
- 把上述场景迁成 YAML case（需要在 harness 里支持 `user_id` 与偏好预置）

### 4. Restart / Recovery 场景

已覆盖：u07 重启后确认。还可以补：

- follow-up after restart
- session 恢复了但 pending task 不存在

## E2E Eval 不打算做什么

当前 harness 不打算：

- 评估真实 LLM 的抽取 / SQL 质量（这是 `scripts/live_eval.py` 的职责）
- 对生产数据库做真实下游查询正确性验证
- 替代 matcher 的 unit tests

它的主要定位是：

- agent behavior regression harness
- orchestration regression harness

## 如何运行

只跑 end-to-end harness（设置 `EVAL_IGNORE_XFAIL=1` 可查看已知缺口用例的真实状态）：

```bash
./.venv311/bin/pytest -q tests/test_end_to_end_evals.py
```

和 turn-based 主测试一起跑：

```bash
./.venv311/bin/pytest -q \
  tests/test_end_to_end_evals.py \
  tests/test_app_endpoints.py \
  tests/test_followup_resolver.py \
  tests/test_query_state_merger.py \
  tests/test_session_manager.py \
  tests/test_task_manager.py
```

## 扩展原则

新增 eval case 时建议：

1. 如果主要是“场景变化”，优先写进 YAML。
2. 如果新增了纯逻辑，也要补 unit test。
3. 断言尽量聚焦稳定字段。
4. 除非必要，不要断完整 SQL 字符串。
5. 优先断 semantic 层，而不是表面格式。

## 长期方向

长期来看，这套轻量 YAML harness 可以继续演进成更完整的 replay-and-regression 层：

- 更多来自真实 query log 的 golden cases
- case 分类标签
- 离线评分报告
- branch / model setting 对比

但即使是现在这个轻量版本，也已经足够保护项目里最重要的 turn-based 和 memory-heavy 主路径。
