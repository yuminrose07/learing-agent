# 2026-05-30 Phase 1A hardening: forge-state baseline 实跑回填

## 运行环境 / 时间窗

- Worktree: `/Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening`
- Branch: `feature/learning-phase-1a-hardening`
- 时间窗：`2026-05-30T18:04:37Z` 至 `2026-05-30T18:05:14Z`
- Dev server: `http://127.0.0.1:8000`
- Server command: `LA_DATA_DIR=/Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening/.learning_agent_data_phase1a_baseline LA_WEB_SEARCH_PROVIDER=builtin python3 -m uvicorn learning_agent.web.web_server:app --host 127.0.0.1 --port 8000`
- Provider model: `qwen-max`
- Baseline version: `2026-05-30-v1`

## 执行命令

```bash
python3 tests/e2e/real_runner.py \
  --dataset tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json \
  --output tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real \
  --base-url http://127.0.0.1:8000 \
  --data-dir /Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening/.learning_agent_data_phase1a_baseline
```

## Evidence 包

- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/manifest.json`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/summary.json`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/results.jsonl`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/cases/<case_id>/request.json`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/cases/<case_id>/response.json`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/cases/<case_id>/session.json`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/cases/<case_id>/learning_unit.json`
- `tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real/cases/<case_id>/events.jsonl`

## 结果

| 指标 | 实跑值 |
| --- | --- |
| total_cases | 4 |
| passed | 4 |
| failed | 0 |
| pass_rate | 1.0 |
| status | `pass` |
| learning_unit_creation_rate | 1.0 |
| non_empty_response_rate | 1.0 |
| forge_stage_metadata_rate | 1.0 |
| temperature_state_metadata_rate | 1.0 |
| learning_unit_payload_forge_stage_rate | 1.0 |
| frontend_backend_stage_consistency_rate | 1.0 |
| chat_session_false_forge_stage_count | 0 |
| ask_alignment_stage_advance_count | 0 |
| first_value_case_pass_count | 3 |
| forge_stage_changed_event_count | 3 |

`tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` 已从 `not_yet_executed` 回填为 `pass`，`baseline_version=2026-05-30-v1`，`implementation_gap=[]`。

## 核查矩阵

| Case | session | 结果 |
| --- | --- | --- |
| `lm-p1a-fuzzy-index-stage` | `sess-f0b25583` | `session.json` assistant metadata `mode=study`；`learning_unit.json` 最终 `forge_stage=collision`；events 含 `first_value_delivered` 与 `forge_stage_changed` |
| `lm-p1a-light-compaction-stage` | `sess-2caa9583` | `mode=study`；最终 `forge_stage=collision`；events 含 `forge_stage_changed` |
| `lm-p1a-ask-does-not-advance-stage` | `sess-065bc386` | `mode=ask`；最终 `forge_stage=entry`；events 无 `forge_stage_changed`；`ask_alignment_stage_advance_count=0` |
| `lm-p1a-stage-event-after-first-value` | `sess-c503dcb5` | `mode=study`；最终 `forge_stage=collision`；events 含 `first_value_delivered` 与 `forge_stage_changed` |

## 对齐事件补充探针

doc1 §5 #10 要求 suggested 卷里能稳定看到 `SUGGESTED` 或 `RATE_LIMITED`。本次 4 case baseline 的 `lm-p1a-ask-does-not-advance-stage` 实际是 C 档 active ASK，不是 B 档 suggested，因此主 baseline 的 `alignment_suggested_or_rate_limited_count=0` 不代表裂缝 4 失效。

为验证 B 档事件路径，额外用同一 dev server 发起探针：

- input: `我想学习整个项目`
- session: `sess-6442c29c`
- unit: `lu-34c70b0b`
- result: events 含 `learning_unit.alignment_suggested`
- cleanup: 已 `POST /learning-units/lu-34c70b0b/stop`，reason=`e2e_probe_cleanup_after_parser_error`

该探针不纳入 baseline 4 case 统计，只作为裂缝 4 可观测路径的真实补证。

## 四层职责分类

| 文件 | 层级 | 说明 |
| --- | --- | --- |
| `tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` | 测试数据 | 用真实 evidence 回填 baseline 状态与指标 |
| `docs/changes/2026-05-30-phase-1a-hardening-baseline-rerun.md` | 文档留痕 | 记录运行环境、证据路径、结果与补充探针 |

产品代码本提交不改。runner 通过 Interface 层 HTTP API 执行真实路径；JSONL 仍是 append-only 事实源。

## 验证

- `curl -fsS http://127.0.0.1:8000/health` -> `{"status":"ok","version":"0.1.0"}`
- `python3 tests/e2e/real_runner.py ...` -> `4/4 passed; status=pass`
- 额外 B 档探针 -> `learning_unit.alignment_suggested`
