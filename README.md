# Learning-Agent v0.1.0

面向个人长期学习的 Agent 系统，核心理念：**最小核心，最大扩展**。

## 架构概览

```
┌─────────────────────────────────────────────┐
│  Presentation Layer  (CLI / Web UI)         │
├─────────────────────────────────────────────┤
│  Application Layer  (Session / Objective)   │
├─────────────────────────────────────────────┤
│  Intelligence Layer  (Agent Loop)           │
├─────────────────────────────────────────────┤
│  Extension Layer  (Hook + Event Bus)        │
├─────────────────────────────────────────────┤
│  Memory Layer  (4-layer memory + KG + SR)   │
├─────────────────────────────────────────────┤
│  Provider Layer  (OpenAI SDK + Streaming)   │
├─────────────────────────────────────────────┤
│  Persistence Layer  (File Store)            │
└─────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置（两种方式任选）

**方式 A：环境变量（推荐用于密钥）**

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export LA_MODEL="gpt-4o"
```

**方式 B：配置文件（推荐用于常规参数）**

创建 `config.yaml`（或 `config.json`）即可：

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，修改 model、base_url 等参数
```

配置文件示例：

```yaml
data_dir: "./learning_agent_data"
observability_dir: "./observability"

provider:
  type: openai
  model: gpt-4o-mini        # 动态切换模型
  api_key: sk-xxx           # 可选，建议优先用环境变量
  base_url: https://api.openai.com/v1
  timeout: 60.0
  max_retries: 3

auto_confirm_knowledge: false
log_level: INFO
```

**配置优先级**：环境变量 > 配置文件 > 代码默认值。

指定自定义配置文件路径：

```bash
python -m learning_agent.main --config /path/to/my_config.yaml
```

查看当前生效的配置：

```bash
python -m learning_agent.main --show-config
```

### 3. 运行

```bash
python -m learning_agent.main
```

### CLI 命令

- 直接输入文字 → 与 Agent 对话（流式输出）
- `/memory` → 查看记忆状态
- `/metrics` → 查看可观测性指标
- `/fork` → 在当前节点分叉会话
- `/confirm <node_id>` → 确认候选知识晋升到长期记忆
- `/save` → 手动保存状态
- `/quit` → 退出

## 项目结构

```
learning_agent/
├── models/           # Pydantic 数据模型
├── core/             # 扩展系统、事件总线、Hook 系统、工具注册表、可观测性
├── provider/         # LLM Provider 层（OpenAI）
├── memory/           # 记忆层（知识图谱、间隔重复、记忆管理器）
├── session/          # 会话层（树形会话管理）
├── persistence/      # 持久化层（文件存储）
├── agent/            # 智能层（Agent 主循环）
├── extensions/       # 内置扩展
├── config.py         # 配置
└── main.py           # 入口
```

## 当前已实现

- ✅ 树形会话管理（创建、追加、fork）
- ✅ 四层记忆体系（L0-L3）读写接口
- ✅ 知识图谱（节点 CRUD、边维护、基础关联查询）
- ✅ 间隔重复引擎（简化 SM-2）
- ✅ 扩展系统（Extension Manager、Hook System、Event Bus、Tool Registry）
- ✅ OpenAI Provider（流式传输 + 重试 + 错误转换）
- ✅ Agent 循环（流式版，含 Hook 触发、上下文组装、工具调用）
- ✅ 可观测性（Trace、Metrics、Snapshot、事件收集）
- ✅ 文件持久化（JSON / JSONL）
- ✅ 内置扩展（可观测性收集、输出倒逼、知识提取、间隔重复调度、文本材料读取）

## 待实现（通过扩展系统）

- ⏳ 意图解析器（core-intent）
- ⏳ 澄清协议（core-clarification）
- ⏳ 向量召回
- ⏳ 高级知识图谱查询
- ⏳ PDF / 网页材料解析
- ⏳ Web UI / 仪表盘
- ⏳ 多人协作
