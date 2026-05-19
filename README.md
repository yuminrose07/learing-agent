# Learning-Agent v0.1.0

面向个人长期学习的 Agent 系统。

**核心理念：最小核心，最大扩展。**

---

## 目录

- [功能概览](#功能概览)
- [架构](#架构)
- [快速开始](#快速开始)
- [配置](#配置)
- [使用方式](#使用方式)
- [API 参考](#api-参考)
- [项目结构](#项目结构)
- [开发](#开发)
- [设计文档](#设计文档)

---

## 功能概览

- **双端入口**：支持 CLI 终端交互与 FastAPI Web 服务
- **流式对话**：基于 SSE 的实时流式聊天，支持工具调用
- **学习目标**：创建与管理长期学习目标，按目标组织会话
- **会话 Fork**：在任意对话节点分叉，探索不同学习路径
- **四层记忆**：L0 瞬态 → L1 候选 → L2 长期 → L3 归档，含知识图谱与间隔重复
- **扩展系统**：基于 Hook + Event Bus 的插件化扩展，内置观测、代码工具、安全审计等
- **工具治理**：Tool Guard 权限控制，支持 allow / ask / deny 三级决策
- **可观测性**：全链路 Trace、Metrics、Events、Audit 记录
- **自愈能力**：工具失败自动重试、降级到 chat-only 模式、异常隔离

---

## 架构

```text
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: Interface Layer                                    │
│   CLI  (python -m learning_agent.learning_agent.main)       │
│   Web  (uvicorn learning_agent.web.web_server:app)          │
├─────────────────────────────────────────────────────────────┤
│ Layer 2: Product / Application Layer                        │
│   LearningAgentSystem  ·  SessionManager  ·  MemoryManager  │
│   ExtensionManager  ·  ToolExecutionServiceImpl  ·  ToolRegistry │
├─────────────────────────────────────────────────────────────┤
│ Layer 3: Agent Runtime Layer                                │
│   AgentLoop → AgentLoopSession                              │
│   HookSystem · EventBus · Observability                     │
├─────────────────────────────────────────────────────────────┤
│ Layer 4: Infrastructure Layer                               │
│   OpenAIProvider / ResilientProvider  ·  FileStore  ·  Models│
└─────────────────────────────────────────────────────────────┘
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

依赖：
- `openai>=1.30.0`
- `pydantic>=2.0.0`
- `fastapi>=0.110.0`
- `uvicorn[standard]>=0.29.0`
- `python-dotenv>=1.0.0`
- `jsonschema>=4.0`

### 2. 配置 API Key

复制示例环境变量文件并填入你的 Moonshot API Key：

```bash
cp .env.example .env
# 编辑 .env
```

```bash
OPENAI_API_KEY=sk-your-moonshot-key-here
LA_MODEL=kimi-2.6
OPENAI_BASE_URL=https://api.moonshot.cn/v1
```

> 程序启动时会**自动读取**当前目录的 `.env` 文件，无需每次 `export`。

### 3. 启动

**Web 模式（推荐）**：

```bash
uvicorn learning_agent.web.web_server:app --reload --port 8000
```

然后打开浏览器访问 `http://localhost:8000`。

**CLI 模式**：

```bash
python -m learning_agent.learning_agent.main
```

---

## 配置

支持三种配置方式，**优先级从高到低**：

**已存在的环境变量 > `.env` 文件 > 配置文件 > 代码默认值**

### 方式 A：`.env` 文件（推荐，最省事）

见 `.env.example`。常用变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `OPENAI_API_KEY` | API 密钥（必填） | `sk-xxx` |
| `LA_MODEL` | 模型名称 | `kimi-2.6` |
| `OPENAI_BASE_URL` | API 基础地址 | `https://api.moonshot.cn/v1` |
| `LA_TIMEOUT` | 请求超时（秒） | `60.0` |
| `LA_MAX_RETRIES` | 最大重试次数 | `3` |
| `LA_DATA_DIR` | 数据存储目录 | `./learning_agent_data` |
| `LA_OBS_DIR` | 可观测性数据目录 | `./observability` |
| `LA_LOG_LEVEL` | 日志级别 | `INFO` |

### 方式 B：配置文件

支持 `config.yaml` 或 `config.json`，启动时自动读取：

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml
```

示例配置见 `config.example.yaml` 与 `config.example.json`。

### 方式 C：环境变量（临时覆盖）

```bash
export OPENAI_API_KEY="sk-xxx"
export LA_MODEL="kimi-2.6"
```

---

## 使用方式

### Web 界面

启动 Web 服务后：

- **首页** (`/`)：会话管理 + 流式聊天
- **观测面板** (`/observability.html`)：Trace、Metrics、Events、Errors、Runtimes 实时查看
- **API 文档** (`/docs`)：FastAPI 自动生成的 Swagger UI

### CLI 命令

启动 CLI 后，在终端输入：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/quit`, `/exit` | 退出程序 |
| `/memory` | 查看记忆状态 |
| `/metrics` | 查看可观测性指标 |
| `/fork [entry_id]` | 在当前节点分叉会话 |
| `/confirm <node_id>` | 确认知识候选晋升到 L2 |
| `/save` | 手动保存状态 |
| `/ask <message>` | Ask 模式（对齐优先） |

普通输入即为与 Agent 对话。

### CLI 启动参数

```bash
python -m learning_agent.learning_agent.main [选项]

  --config, -c    指定配置文件路径
  --show-config   打印加载的配置并退出
  --web           启动 Web 服务而非 CLI
  --host          Web 服务绑定地址（默认 127.0.0.1）
  --port          Web 服务端口（默认 8000）
```

---

## API 参考

Web 服务基于 FastAPI，主要端点：

### 健康检查
- `GET /health`

### 学习目标
- `POST /objectives` — 创建目标
- `GET /objectives` — 列出目标
- `GET /objectives/{id}` — 获取目标

### 会话
- `POST /sessions` — 创建会话
- `GET /sessions` — 列出会话
- `GET /sessions/{id}` — 获取会话
- `PUT /sessions/{id}` — 更新会话标题
- `DELETE /sessions/{id}` — 删除会话（自动清理运行时）
- `POST /sessions/{id}/chat` — 对话（SSE 流式）
- `POST /sessions/{id}/fork` — 分叉会话
- `POST /sessions/{id}/reset-runtime` — 重置运行时

### 记忆与知识
- `GET /memory` — 获取记忆状态
- `POST /knowledge/{node_id}/confirm` — 确认知识晋升

### 可观测性
- `GET /observability/errors` — 错误日志聚合
- `GET /observability/traces` — Trace 列表
- `GET /observability/traces/{trace_id}` — 单条 Trace
- `GET /observability/events` — 事件日志
- `GET /observability/events/stream` — 事件实时 SSE 推送
- `GET /observability/metrics` — 指标摘要
- `GET /observability/runtimes` — 活跃运行时概览
- `GET /observability/flows/{session_id}` — 会话流数据
- `GET /observability/logs` — 日志聚合

---

## 项目结构

```
learning_agent/
├── __init__.py
├── config.py                  # 根级配置兼容（已收敛至 learning_agent/learning_agent/config.py）
│
├── learning_agent/            # Product/Application 层
│   ├── __init__.py
│   ├── main.py                # LearningAgentSystem + CLI 入口
│   ├── config.py              # 配置解析
│   ├── extension_manager.py   # 扩展生命周期管理
│   ├── session_manager.py     # 会话树管理
│   ├── tool_registry.py       # Product 层工具注册与执行
│   ├── tool_execution_service.py # Runtime 所依赖的工具执行适配
│   └── extensions/
│       ├── built_in.py        # 内置扩展工厂
│       ├── code_tools.py      # 代码工具扩展
│       ├── context_compressor.py
│       ├── security_audit.py
│       └── tool_guard.py      # 工具权限守卫
│
├── agent/                     # Agent Runtime 层
│   ├── agent_loop.py          # AgentLoop + AgentLoopSession
│   ├── event_bus.py           # 事件总线
│   ├── hook_system.py         # Hook 系统（5 个标准切点）
│   ├── observability.py       # 可观测性收集器
│   ├── tool_failure_tracker.py
│   ├── tool_validator.py
│   └── runtime_ports.py       # 运行时协议接口
│
├── memory/                    # Memory 领域服务
│   ├── memory_manager.py      # 四层记忆管理
│   ├── knowledge_graph.py     # 知识图谱
│   └── spaced_repetition.py   # 间隔重复引擎
│
├── ai/                        # Infrastructure 层
│   ├── models.py              # Pydantic 数据契约
│   ├── base_provider.py       # Provider 抽象
│   ├── openai_provider.py     # OpenAI SDK 实现
│   ├── resilient_provider.py  # 韧性 Provider（重试+降级）
│   └── file_store.py          # 文件持久化
│
└── web/                       # Interface 层（Web）
    ├── __init__.py
    └── web_server.py          # FastAPI 服务

web/
├── index.html                 # 前端主页面
├── observability.html         # 观测面板
└── static/                    # CSS / JS 静态资源

tests/
├── test_hook_system.py
├── test_tool_execution_reliability.py
└── test_web_adaptation.py

docs/
├── README.md                  # 文档索引与导航
├── design/                    # 技术设计文档
├── research/                  # 调研报告与竞品分析
├── audit/                     # 代码审计与项目诊断
├── bugfix/                    # Bug 报告与修复记录
├── output/                    # 实施报告、验收报告与任务归档
└── prompt/                    # Agent 约束模块软链接入口
```

---

## 开发

### 运行测试

```bash
pytest tests/ -v
```

当前测试覆盖：
- Hook 系统执行顺序与异常隔离
- 工具失败追踪与重试策略
- AgentLoop 会话隔离与并发安全
- Web 端点与生命周期清理

### 添加扩展

扩展通过 `HookSystem` 接入，支持 5 个标准切点：

- `before_agent_run`
- `before_tool_execute`
- `after_tool_execute`
- `on_stream_chunk`
- `after_response`

在 `learning_agent/learning_agent/extensions/built_in.py` 中注册新扩展即可自动加载。

---

## 设计文档

项目的设计与架构文档位于 `docs/design/`：

- `design-current-code-architecture-boundaries.md` — 当前架构骨架与职责边界
- `design-four-layer-convergence-plan.md` — 四层架构收敛方案
- `design-architecture-convergence-implementation.md` — 架构收敛技术实现
- `design-web-adaptation.md` — Web 前后端适配方案
- `design-runtime-protocol-inversion-minimal-implementation.md` — 运行时协议反转
- `resilience-and-validation-design.md` — 韧性与验证设计

Agent 协作约束见 `AGENTS.md`。

---

## License

MIT
