# Learning Agent —— 测试方法论

本文档定义本项目「如何测试各个功能」的统一方法论。所有新测试都应遵循。

> **关联文档**：测试是可观测系统的一部分。整体架构（L1 事件流 / L2 断言 / L3 报告 / L4 看板）见
> [`design/design-observability-l1-l4-architecture.md`](design/design-observability-l1-l4-architecture.md)。
> 本文档讲的「测试分四层」是**代码质量层级**（unit/integration/e2e/slow），
> 与那份架构文档的「观测系统四层」（按读者切分）正交，不要混淆。

***

## 一、四层模型 + FIRE 原则

### 1.1 测试分四层（与 `AGENTS.md` 的代码四层正交）

| 层级 | 关注点 | 主要手段 | pytest marker |
|---|---|---|---|
| **L1 单元** | 单函数/单类的正确性、边界值 | pytest 单测 + 纯函数 | `unit` |
| **L2 集成** | 模块之间的契约（不跨进程） | fixture + in-process 拼装 | `integration` |
| **L3 端到端** | 一次完整用户场景跑通 | scenario yaml + ScriptedProvider | `e2e` |
| **L4 非功能** | 性能、可靠性、内存、长任务 | 数据集回放 + 监控 | `slow` |

> **每个功能在合入前**必须明确至少由哪几层覆盖。不必每层都做，但要主动选择，并在 PR 描述中写清楚。

### 1.2 FIRE 原则

- **F (Fixture-first)** — 外部依赖（LLM / 文件系统 / 网络）一律走 `tests/conftest.py` 的共享 fixture，禁止在测试里裸 mock。
- **I (Isolation)** — 每个测试拿到独立 tmp 目录与独立事件存储；不污染 `.observability/` 等真实运行数据。
- **R (Replayable)** — LLM 调用全部通过 `ScriptedProvider` 或录制回放，CI 默认不打真实 LLM。
- **E (Evidence)** — 失败时把关键事件落到 `.test_artifacts/<test>/`，方便定位 & 离线回放。

***

## 二、目录约定

```
tests/
├── conftest.py                # 项目级共享 fixture（sys.path / ScriptedProvider / tmp / 证据落盘）
├── fixtures/                  # 测试数据（LLM 录制、数据集等）
├── e2e/                       # L3 端到端
│   ├── runner.py              # scenario 执行引擎
│   ├── test_scenarios.py      # 自动发现 scenarios/*.yaml
│   └── scenarios/
│       └── *.yaml             # 声明式场景
├── test_*.py                  # L1/L2 现有测试（保持原貌，逐步迁移）
└── ...
pytest.ini                     # markers + asyncio 配置
```

***

## 三、常用命令

```bash
# 全量
pytest

# 按层
pytest -m unit
pytest -m integration
pytest -m e2e
pytest -m "e2e and not slow"

# 按功能
pytest -m memory
pytest -m compaction
pytest -m "agent and not slow"

# 启用真实 LLM（默认所有 live_llm 测试被跳过）
pytest --live-llm -m live_llm
```

***

## 四、新增一个 e2e 用例

**不用写 Python**。在 `tests/e2e/scenarios/` 下加一个 yaml：

```yaml
name: my_new_case
description: 一句话描述
markers: [agent, session]

user_input: "用户输入"

llm_script:
  - "LLM 第一次返回"
  - "LLM 第二次返回"

assertions:
  - kind: event_count_min
    event_type: assistant_message
    count: 1
  - kind: event_contains
    event_type: assistant_message
    substring: "关键字"
  - kind: no_error
```

`tests/e2e/test_scenarios.py` 会自动发现并参数化。

**目前已支持的 assertion kinds**（见 `tests/e2e/runner.py`）：

- `event_count_min` — 某类事件至少出现 N 次
- `event_contains` — 某类事件的 JSON 序列化里包含子串
- `no_error` — 不存在以 `error` 结尾的事件类型

新增 assertion kind = 在 `runner.py` 的 `CHECKERS` 字典里加一项。

***

## 五、新增一个共享 fixture 的规则

只有满足下面**全部**条件才放进 `tests/conftest.py`：

1. 至少 2 个测试文件会用
2. 不会因为引入它而强行加载重依赖（如 OpenAI SDK）—— 否则要用 fixture 内部延迟 import
3. 行为是纯粹的（无全局状态污染）

否则放进对应子目录的 `conftest.py` 或单测里。

***

## 六、为每个功能写「测试章程」

在 `docs/design/` 下，每个核心功能配一份短小的测试章程（半页内）：

```markdown
## 功能：<name>
- 输入契约：……
- 输出契约：……
- 不变式：……
- 已知风险：……
- 测试策略：
  - L1: ...
  - L2: ...
  - L3: scenarios/xxx.yaml
  - L4: ...
- 测试数据位置：tests/fixtures/<name>/
```

这份章程是 PR review 时的对照表，比抽象的"覆盖率"有用得多。

***

## 七、迁移现状

- 旧测试文件（`tests/test_*.py`）**保持原貌**，不强制迁移。
- 但**新写**或**重大重构**的测试，必须走本文档的约定（用共享 fixture、加 marker、e2e 走 scenario yaml）。
- 现有 `test_compaction_dataset_runner.py` 中的 `ScriptedProvider` 已抽到 `tests/conftest.py` 的 `scripted_provider` fixture，后续可逐步收敛。

***

## 八、scenario 与真实事件流的对齐

E2E runner（`tests/e2e/runner.py`）已经接入真实组件链路：

```
ScriptedProvider → AgentLoop → SessionManager → FileStore
                                    ↓
              file_store.read_session_events(session.id)
                                    ↓
        normalize: 去掉 _added 后缀，记录原始类型到 _raw_type
                                    ↓
                     合成 assistant_response_aggregate
                                    ↓
                            scenario assertions
```

事件流来源就是生产路径的同一份 append-only JSONL（符合 `AGENTS.md` 的「事实源约束」）。

**scenario yaml 中可用的事件类型**（取自 `SessionEventType`，已自动去 `_added` 后缀）：

| event_type | 含义 |
|---|---|
| `session.created` | 会话创建 |
| `message.user_appended` | 用户消息写入 |
| `message_end` | 一轮结束 |
| `compaction.summary` | 压缩 summary 事件（如果触发） |
| `assistant_response_aggregate` | runner 合成：assistant 全部 chunk 拼起来的文本 |
| `runner_error` | runner 合成：agent_loop.run 抛异常时记录 |

查看某个 scenario 的完整事件流：

```bash
cat .test_artifacts/tests_e2e_test_scenarios.py_test_scenario\[<name>\]/events.jsonl | jq .
```

***

## 九、质量棘轮（compaction dataset 专用）

`test_compaction_dataset_runner.py` 在硬断言之外，额外维护一组「棘轮」：

| 字段 | 位置 | 含义 |
|---|---|---|
| `expectations.min_drift_score` | 每个 case JSON | 回归红线：本 case 的 `drift_score` 不允许跌破此值。改进压缩质量后，应把这个值往上抬 |
| `expectations.max_warning_count` | 每个 case JSON | 本 case 的 warnings 数量上限。引入新警告类型时同步上调 |
| `LA_DATASET_MIN_DRIFT_SCORE` | 环境变量 | 全局质量红线，默认 0（关闭）。CI 中可设为 60 / 80 强制最低质量 |

**为什么叫"棘轮"**：这两个字段只能朝"更严格"的方向调整 —— 你修好了 Bug、`drift_score` 从 40 涨到 70，那就把 `min_drift_score` 也从 40 改成 70。这样下次有人把压缩质量改差到 65，CI 立刻失败。**质量只能上、不能下**，所以叫棘轮（ratchet）。

**用法示例**：

```bash
# 日常：默认棘轮，3 个 case 锚在当前分数
pytest tests/test_compaction_dataset_runner.py

# CI 严格模式：质量分必须 ≥ 60
LA_DATASET_MIN_DRIFT_SCORE=60 pytest tests/test_compaction_dataset_runner.py

# 评估真实 LLM（棘轮在 real 模式下自动禁用，只跑观察+报告）
LA_DATASET_PROVIDER_MODE=real pytest tests/test_compaction_dataset_runner.py
```

**失败信息样例**：

```
case 'retained_recent_context' 回归红线触发：drift_score=40 < min=50
  含义：本 case 的压缩质量较锚定值有回退。
  warnings: ['summary 出现异常引号痕迹', 'summary 出现重复事实条目']
  详见: /Users/.../.test_artifacts/compaction_dataset_runs/scripted/latest/retained_recent_context/report.json
```

**配套可视化**：
```bash
python3 tests/render_compaction_dataset_report.py scripted
# → 输出到 .test_artifacts/compaction_dataset_runs/scripted/latest/compaction_dataset_report.html
```

> 当前 3 个 case 的初始锚点：`migration_incremental_canonical=10`, `multi_stage_accumulation=0`, `retained_recent_context=40`。**这些初始值故意贴着当前实际分数，所以测试现在全过，但任何回退立刻失败**。当压缩质量提升时，请同步上调对应字段。

***

## 十、后续优化方向

当前脚手架已经能支撑真实 e2e，但还可以继续打磨：

1. 把 `render_compaction_dataset_report.py` 接成 pytest 的 session-level fixture，测完自动出 HTML（现在还是手动一步）
2. HTML 报告加 diff 视图，展示与上次运行的分数 / warnings 变化
3. 增加 `tool_call_count`、`session_mode_final`、`compaction_triggered` 等更多 assertion kind 给 e2e scenarios
4. 让 scenario yaml 支持多轮对话（当前一个 scenario 只跑一轮）
5. 修复当前已暴露的两个真实问题：summary 中的智能引号污染、重复条目
6. 拓宽 compaction case 维度覆盖：短任务 / 异常恢复 / 工具穿插 / 中英混合
7. 在 CI 中按 marker 分阶段跑：`pytest -m "unit or integration"` → `pytest -m e2e` → `pytest -m slow`

***

## 十一、真实数据集 E2E 规范

本节适用于**真实 Web Server + 真实前端 / 真实 API + 真实外部依赖**的端到端测试。

目标不是替代 `tests/e2e/scenarios/*.yaml` 这类脚本化回放，而是建立一套后续长期复用的**真实数据集、真实运行结果、证据包与基线**管理方式。

### 11.1 适用场景

- 验证 `前端 -> Web Server -> Agent Runtime -> Tools -> Session/Event Log` 的真实链路。
- 复现线上或手工发现的真实问题，例如：
  - 空返回
  - 半句返回 / 过渡句被当最终答案
  - rescue 兜底缺失
  - `web_search` / `web_fetch` 降级失效
  - 前端展示与服务端最终消息不一致
- 评估“修复后是否真的在真实环境稳定生效”。

### 11.2 与脚本化 E2E 的分工

- `tests/e2e/scenarios/*.yaml`
  - 用于可控、可回放、CI 友好的脚本化 E2E。
  - 默认不依赖真实网络、真实 LLM、真实浏览器。
- `真实数据集 E2E`
  - 用于验证真实环境中的实际用户路径和真实外部依赖。
  - 允许存在网络波动，但必须留痕并分类。

二者都属于 L3 端到端，只是**一个偏确定性回放，一个偏真实世界验收**。

### 11.3 本地目录约定

真实数据集 E2E 统一使用以下目录：

```text
tests/e2e/real_datasets/
  <suite>.json                 # 数据集定义，长期保留

tests/e2e/real_baselines/
  <suite>.baseline.json        # 期望统计、历史锚点、允许波动说明

.test_artifacts/e2e_real_runs/
  <suite>/
    latest -> <run_id>/        # 最新一次运行的软链接或约定别名
    <run_id>/
      manifest.json            # 本次运行元信息
      summary.json             # 总结：pass/fail、新增失败、回归失败
      results.jsonl            # 每条 case 一条结构化结果
      cases/
        <case_id>/
          request.json
          response.json
          session.json
          events.jsonl
          frontend.png
          unresolved_failures.jsonl
```

说明：

- `tests/e2e/real_datasets/`：放**长期复用的真实样本定义**，属于仓库内容，应纳入版本管理。
- `tests/e2e/real_baselines/`：放**基线和棘轮**，用于比较“这次比上次更好还是更差”。
- `.test_artifacts/e2e_real_runs/`：放**每次真实运行结果和证据包**，默认不入库，供本地排查和离线回放。

### 11.4 命名规则

- `suite`：使用 kebab-case，例如 `web-search-regression`、`frontend-rescue-guard`
- `case_id`：必须全局唯一，推荐 `e2e-<topic>-<risk>`
- `run_id`：必须包含日期时间，推荐：

```text
YYYY-MM-DDTHHMMSSZ_<suite>_<mode>
```

示例：

```text
2026-05-27T103000Z_web-search-regression_manual
```

### 11.5 数据集文件规范

每个数据集文件至少包含：

- `suite_id`
- `description`
- `owner`
- `cases`

每条 case 至少包含：

- `id`
- `title`
- `source`
- `tags`
- `setup`
- `input`
- `expect`
- `forbid`
- `evidence_required`
- `severity`

推荐结构：

```json
{
  "suite_id": "web-search-regression",
  "description": "真实搜索、抓取、降级、兜底回归集",
  "owner": "learning-agent",
  "cases": [
    {
      "id": "e2e-python-312-half-answer",
      "title": "Python 3.12 搜索不能停在过渡句",
      "source": "historical_bug",
      "tags": ["web_search", "multi_turn", "regression"],
      "setup": {
        "reuse_session": true,
        "prior_turns": [
          "请访问 https://httpstat.us/503 并告诉我返回什么内容，这是一个测试503错误的网站"
        ]
      },
      "input": "请搜索 Python 3.12 的新特性",
      "expect": {
        "non_empty_response": true,
        "allow_rescue": true,
        "frontend_backend_consistent": true
      },
      "forbid": {
        "contains": [
          "让我尝试其他来源：",
          "让我换个来源试试。先搜索一些更容易获取的技术博客内容。"
        ],
        "empty_response": true
      },
      "evidence_required": [
        "response.json",
        "session.json",
        "events.jsonl",
        "frontend.png"
      ],
      "severity": "blocker"
    }
  ]
}
```

### 11.6 真实运行必须保存什么

每次真实 E2E 运行结束后，至少保存：

- `manifest.json`
  - `run_id`
  - `suite_id`
  - 启动时间 / 结束时间
  - 端口
  - 模型
  - 关键配置
  - 是否前端模式 / API 模式
- `summary.json`
  - 总 case 数
  - pass / fail / flaky 数
  - 新增失败
  - 已知失败
  - 回归失败
- `results.jsonl`
  - 每条 case 一条结构化结果
- `cases/<case_id>/...`
  - 本 case 的完整证据包

### 11.7 结果判定规则

先判**硬规则**，再看软质量。

硬规则命中任一条，case 直接失败：

- 最终用户可见内容为空
- 最终内容只包含过渡句、规划句、未完成句
- 前端展示与服务端最终消息不一致
- 工具失败后没有形成有效兜底或有效回答
- 失败发生后没有留下应有证据
- 已知回归 case 再次出现同类问题

推荐失败分类：

- `empty_response`
- `incomplete_transition_answer`
- `tool_denied_no_recovery`
- `tool_failed_no_rescue`
- `frontend_backend_mismatch`
- `missing_evidence`
- `external_dependency_unstable`
- `unexpected_internal_error`

### 11.8 基线与棘轮

真实数据集 E2E 也要维护“质量只能上、不能下”的棘轮：

- `tests/e2e/real_baselines/<suite>.baseline.json` 记录：
  - 当前允许的失败 case 白名单
  - 每类失败最大数量
  - 必须始终通过的 blocker case
  - 外部依赖波动说明
- 修复某个历史问题并稳定通过后，应同步收紧 baseline：
  - 从允许失败白名单移除
  - 或把允许失败数量下调

### 11.9 执行要求

以后凡是“构造真实数据集并跑真实 E2E”，默认都必须遵守：

1. 先选定数据集 `suite`
2. 明确本次运行模式：`manual` / `nightly` / `pre-release`
3. 启动真实 Web Server，并记录端口与配置
4. 对每条 case 保存请求、响应、session、events、截图
5. 输出 `summary.json` 与 `results.jsonl`
6. 与对应 baseline 对比，标记：
   - `new_failure`
   - `known_failure`
   - `regression`
   - `pass`

### 11.10 何时更新数据集

以下场景必须补充或更新真实数据集：

- 出现新的真实用户可见问题
- 修复了一个线上 / 手工复现问题
- 新增了一个高风险工具路径
- 修改了降级 / rescue / 前端消息拼装逻辑
- 修改了 `web_search`、`web_fetch`、tool budget guard、session replay 等关键路径

### 11.11 何时更适合模块，而不是 skill

本项目推荐把这套规范放在仓库内，作为：

- `AGENTS.testing.md` 入口模块
- `docs/TESTING.md` 完整规范

而不是单独做成 skill。

原因：

- skill 更偏“助手能力”
- 仓库规范更适合约束“以后所有真实 E2E 都必须怎么跑、怎么存、怎么留痕”
- 仓库内文档更容易和代码、测试、变更记录一起演进

只有当你希望“跨仓库复用同一套真实 E2E 流程”时，才值得再抽成通用 skill。
