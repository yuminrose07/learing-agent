# AGENTS.testing.md — 真实 E2E 测试模块

本文档定义本仓库在执行“真实数据集驱动的端到端测试”时必须遵守的专项规范。它补充 `AGENTS.md` 中的“文档对齐”“可观测优先”“变更留痕”规则，不替代核心规则。

***

## 模块元信息

- `module_id`: `constraint.testing`
- `version`: `1.0.0`
- `purpose`: 约束真实数据集 E2E 的数据集管理、运行留痕、结果落盘、基线比较与回归流程
- `load_when`: 构造真实数据集、运行真实 Web/API E2E、复现实例问题、做发布前真实验收时
- `inputs`: 测试目标、数据集 suite、运行模式、环境配置、端口、模型、是否走前端
- `outputs`: 数据集文件、运行结果包、证据包、基线比较结论、回归判断
- `depends_on`: `core`
- `load_after`: `AGENTS.md`
- `conflicts_with`: 无
- `fallback`: 如果本次只是脚本化/回放型 e2e，则退回 `docs/TESTING.md` 的通用 e2e 规范，不强行要求真实网络链路

## 使用边界

- 本模块只约束“真实数据集 E2E”，不替代单元测试、集成测试、脚本化 scenario E2E。
- 本模块优先约束“怎么构造、怎么运行、怎么落盘、怎么比较”，不直接规定具体实现脚本。
- 本模块要求测试结果可复现、可比较、可追责。

## 强制规则

- 真实数据集必须落盘到 `tests/e2e/real_datasets/`
- 真实 E2E baseline 必须落盘到 `tests/e2e/real_baselines/`
- 每次真实运行结果与证据包必须落盘到 `.test_artifacts/e2e_real_runs/<suite>/<run_id>/`
- 每条失败 case 都必须保留请求、响应、session、events、截图等证据
- 每次运行都必须产出 `summary.json` 和 `results.jsonl`
- 已修复的真实问题必须进入回归集，不允许只靠口头记忆

## 详细规范

- 完整规则见 `docs/TESTING.md` 第十一节《真实数据集 E2E 规范》。

## 推荐加载方式

当任务命中以下任一条件时，优先加载本模块：

- “构造真实数据集”
- “跑真实端到端测试”
- “验证前端 + Web Server + tools 的真实链路”
- “复现并沉淀某个线上/手工发现问题”
- “把测试结果保存到本地并形成长期可复用规范”
