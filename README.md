# Feishu <-> 本地终端 Codex Bridge

这个项目提供双向桥接能力：

1. 把本地终端（含 Codex）输出实时推送到飞书。
2. 在手机飞书发送消息，远程控制本地终端/Codex。

---

## 功能总览

- 飞书事件订阅 **Stream Mode**（无需公网回调）
- 本地持久 shell 会话（默认 `zsh -li`）
- `/cmd` 执行本地命令
- `/codex` 进入 Codex 对话模式（内部使用 `codex exec --json`）
- **Schema 2.0 模板卡片流式更新**（同一任务同一张卡片）
  - 上半区：命令执行状态（`status_content`）
  - 下半区：最终回答累积（`answer_content`）
- 本地完整历史记录系统（任务级落盘、查询、清理）
- 运行日志落盘（`bridge.log`）

---

## 架构说明

- 飞书 -> 本地：通过 `im.message.receive_v1` 接收用户消息。
- 本地 -> 飞书：发送/更新消息卡片。
- Codex 模式：每条自然语言启动一次 `codex exec --json`。
- JSON 事件解析后分流：
  - `command_execution` -> 状态栏
  - `agent_message` -> 回答区
- 卡片更新频率可配置，避免过于频繁刷新。

---

## 1) 飞书平台配置

在飞书开放平台创建企业自建应用并启用机器人能力：

### 必要权限（至少）

- `im:message`
- `im:message:send_as_bot`
- `im:message:receive`

### 事件订阅

- 订阅事件：`im.message.receive_v1`
- 模式：**长连接 Stream Mode**

### CardKit 模板

你需要创建并发布一个 Schema 2.0 模板，至少包含以下变量：

- `status_content`（string）
- `answer_content`（string）

然后记录模板 ID（例如你当前使用的 `AAqK4drHMTtX4`）。

参考文档：
- https://open.feishu.cn/document/home/index
- https://open.feishu.cn/document/server-docs/im-v1/message/create
- https://open.feishu.cn/document/cardkit-v1/feishu-card-resource-overview

---

## 2) 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env
python app.py
```

---

## 3) `.env` 配置项（完整）

```env
# Feishu app credentials
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_ENCRYPT_KEY=

# Runtime
SHELL_PATH=/bin/zsh
STATE_FILE=.bridge_state.json
LOG_FILE=bridge.log
HISTORY_DIR=history

# Output flush
FLUSH_INTERVAL_SECONDS=1.2
CODEX_FLUSH_INTERVAL_SECONDS=0.35
CARD_UPDATE_INTERVAL_SECONDS=0.8
CARD_STATUS_MAX_LINES=18
CARD_ANSWER_MAX_CHARS=2600
MAX_MESSAGE_CHARS=1200

# Command allow-list (optional)
ALLOWED_COMMAND_PREFIXES=

# TLS options
FEISHU_CA_CERT_PATH=
FEISHU_INSECURE_SKIP_VERIFY=false

# Schema 2.0 template card
CARD_TEMPLATE_ID=AAqK4drHMTtX4
CARD_TEMPLATE_VAR_NAME=content
CARD_STATUS_VAR_NAME=status_content
CARD_ANSWER_VAR_NAME=answer_content
```

说明：
- 当前代码优先使用 `CARD_STATUS_VAR_NAME` + `CARD_ANSWER_VAR_NAME`。
- `CARD_TEMPLATE_VAR_NAME` 仅用于兼容单变量模板。

---

## 4) 手机端命令

- `/bind`：绑定当前会话为推送目标
- `/help`：查看帮助
- `/status` 或 `/ps`：查看运行状态
- `/cmd <command>`：执行本地 shell 命令
- `/codex`：进入 Codex 对话模式
- `/exitcodex`：退出 Codex 对话模式
- `/ctrlc`：向终端发送 Ctrl+C

### 历史记录命令

- `/history`：列出最近任务
- `/history <task_id>`：查看任务详情
- `/history clear`：清空历史

---

## 5) 卡片流式更新行为

每个 Codex 任务会创建一张卡片，并持续更新：

- 上半区（状态栏）
  - 任务 ID / 状态 / 用户请求
  - 已执行命令轨迹（最近若干条）
- 下半区（回答区）
  - `agent_message` 文本持续累积
- 两区都有限制：
  - 状态栏最多显示最近 `CARD_STATUS_MAX_LINES` 条
  - 回答区最多显示最近 `CARD_ANSWER_MAX_CHARS` 字符
  - 更早内容会折叠，并提示使用 `/history` 查看完整

任务完成后状态改为“已完成”；中断则为“已中断”。

---

## 6) 历史记录与日志

### 历史记录

- 目录：`history/tasks/*.json`
- 每个任务包含：
  - prompt
  - status
  - commands
  - answer_parts
  - events
  - card_message_id
  - 时间戳

### 运行日志

- 文件：`bridge.log`
- 包含：
  - 收到的飞书消息
  - 本地下发命令
  - shell 输出
  - 卡片发送/更新结果

---

## 7) 常见问题

### 1) 卡片发送成功但手机不显示预期内容

先看 `bridge.log`：
- `send ok(schema2-template-card)`：模板卡片发送成功
- 若仍显示异常，多数是模板变量名不匹配

检查：
- `CARD_TEMPLATE_ID` 是否为已发布模板
- 模板内变量名是否与 `.env` 一致（`status_content` / `answer_content`）

### 2) 证书报错 `CERTIFICATE_VERIFY_FAILED`

优先方案：
- 配置 `FEISHU_CA_CERT_PATH=/path/to/corp-root.pem`

临时排障（不安全）：
- `FEISHU_INSECURE_SKIP_VERIFY=true`

### 3) 收不到消息

检查：
- 是否已 `/bind`
- 应用可用范围是否包含你
- `im.message.receive_v1` 是否已订阅并生效
- `app.py` 是否在运行

---

## 8) 安全建议

- 机器人尽量只放在可控会话。
- 建议配置 `ALLOWED_COMMAND_PREFIXES` 限制高风险命令。
- `.env` 含敏感信息，务必加入 `.gitignore`，不要提交到仓库。
