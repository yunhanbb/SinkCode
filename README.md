# OpenCodex

OpenCodex 是一个运行在本地机器上的飞书机器人桥接服务，用来把“飞书消息”“本地持久 shell”“Codex CLI”连到一起。

你可以在飞书里：

- 用 `/cmd` 远程执行本地终端命令
- 用 `/codex` 进入 Codex 对话模式，按任务查看执行状态与回答
- 用 `教我做题` 进入数学辅导模式，支持图片题、多轮追问、文档沉淀

整个项目当前是一个单文件服务（`app.py`），使用飞书 Stream Mode 收消息，不需要自建公网回调地址。

## 适合什么场景

- 在手机或飞书聊天窗口里远程驱动本地开发机
- 把 Codex CLI 的执行过程实时推送到飞书卡片
- 用固定会话把终端、问答、历史记录统一沉淀下来
- 给学生或家人做数学题讲解，并把题目与答案自动汇总到飞书文档

## 当前能力总览

### 1. 普通终端模式

- `/cmd <command>` 会把命令发送到本地持久 shell，会话默认是 `zsh -li`
- shell 是常驻的，所以上一条命令留下来的当前目录、环境变量、shell 上下文会继续保留
- `/ctrlc` 会向当前 shell 发送 Ctrl+C
- 普通模式下的终端输出会直接回推到绑定会话

### 2. Codex 对话模式

- `/codex` 进入模式后，每条自然语言消息都会触发一次独立的 Codex 任务
- 实际执行命令为：`codex exec --json --color never --skip-git-repo-check '<你的请求>'`
- 每个请求都有自己的任务卡片：
  - 状态区展示命令执行轨迹、完成/中断状态
  - 回答区累积展示模型输出
- 卡片内容按节流频率更新，并带有“打字机”式逐步展示效果
- `/exitcodex` 退出模式；若此时仍有任务在跑，会先中断当前请求
- `/history` 系列命令可查看 Codex 任务历史

### 3. 数学辅导模式

- `教我做题` 进入模式，支持文字题和图片题
- 图片会先下载到本地 `history/math_assets/`，再通过 `codex exec --image ...` 交给 Codex 分析
- 同一道题会复用同一张卡片；追问会继续写入该卡片
- `下一题` 会结束当前题目的沉淀，并为新题创建新卡片
- `结束做题` / `退出做题` / `退出数学辅导` 会退出模式
- 支持自定义数学辅导提示词：
  - `设置数学辅导提示词 <内容>`
  - `查看数学辅导提示词`
  - `清空数学辅导提示词`
- 支持把每道题同步到飞书文档：
  - `创建数学总结文档 [标题]`
  - `绑定数学总结文档 <doc_id 或链接>`
  - `查看数学总结文档`
  - `关闭数学总结文档`
- 数学总结文档使用机器人/应用身份创建与写入；创建后会尽量保持租户内链接可访问，并返回文档直达链接
- 如果还没绑定文档，系统会在首次同步时自动创建一个固定文档，后续题目持续追加到同一份文档
- 同一道题会持续更新文档里的同一段内容；`下一题` 会在同一文档里追加新的题目小节

### 4. 会话绑定与授权

- `/bind` 会把当前 chat 设为推送目标，同时把当前发送者设为授权账号
- 绑定后，其他飞书账号发来的控制命令会被拒绝
- 绑定信息持久化在 `.bridge_state.json`，服务重启后仍会保留
- 服务启动时，如果之前已经绑定过 chat，会尝试向该会话发一条启动提示

### 5. 卡片、降级与消息体验

- 优先使用飞书模板卡（Schema 2.0）发送和更新消息
- 若未配置模板卡，或模板卡发送失败，会自动降级为 `post` 或普通文本消息
- 普通系统反馈会尽量复用同一张“系统消息卡片”，避免刷屏
- Codex 模式按“每个请求一张卡片”组织
- 数学辅导模式按“每道题一张卡片”组织

### 6. 历史记录与落盘

- Codex 任务会写入 `history/tasks/<task_id>.json`
- 历史内容包括：请求、状态、执行过的命令、模型回答、事件流、卡片消息 ID、时间戳
- 数学辅导图片会写入 `history/math_assets/`
- 运行日志写入 `bridge.log`

## 运行方式概览

```text
飞书消息
  -> Stream Mode 事件
  -> OpenCodex (`app.py`)
  -> 授权校验 / 模式分发
     -> `/cmd` -> 本地持久 shell
     -> `/codex` -> `codex exec --json ...`
     -> 数学辅导 -> `codex exec --image ...` / `codex exec resume ...`
  -> 卡片更新 / 普通消息回推 / 历史落盘 / 文档同步
```

## 项目结构

```text
.
├── app.py                 # 主服务，包含配置、状态、飞书事件处理、shell/Codex/math 逻辑
├── README.md              # 项目说明
├── JSON_PARSE_LOGIC.md    # Codex JSON 输出的解析说明
├── requirements.txt       # Python 依赖
├── .bridge_state.json     # 绑定状态、授权用户、数学辅导配置（运行后生成）
├── bridge.log             # 运行日志（运行后生成）
└── history/
    ├── tasks/             # Codex 任务历史
    └── math_assets/       # 数学辅导下载的图片题素材
```

## 环境要求

- Python 3.10+
- 本机可执行 `codex` 命令
- 本机可正常连接飞书开放平台（如公司网络使用私有 CA，可配证书路径）
- 已创建飞书应用，并启用机器人与 Stream Mode

## 飞书侧准备

### 1. 创建企业自建应用

至少需要让机器人具备以下能力：

- 接收消息事件
- 发送消息到会话
- 如果要用图片题，需要读取图片资源
- 如果要用数学总结文档，需要创建/读取/写入飞书文档

权限项的精确名称会随飞书控制台展示方式变化，实际以你当前控制台为准；只要覆盖消息、图片资源、文档三类能力即可。

### 2. 开启 Stream Mode

本项目通过长连接方式接收 `im.message.receive_v1` 事件，不依赖公网回调 URL。

### 3. 可选：准备飞书模板卡

如果希望获得更好的卡片复用与流式更新体验，建议在飞书 CardKit 中创建并发布一个模板卡。

推荐模板变量：

- `status_content`
- `answer_content`

然后把模板 ID 填到 `.env` 的 `CARD_TEMPLATE_ID`。

如果你只想用单变量模板，也可以：

- 清空 `CARD_STATUS_VAR_NAME`
- 清空 `CARD_ANSWER_VAR_NAME`
- 把 `CARD_TEMPLATE_VAR_NAME` 设为模板里的那个单变量名

不过当前项目默认是“双区块卡片”用法，更适合 Codex 状态和回答分开展示。

## 安装与启动

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

依赖很少，只有：

- `lark-oapi`
- `pexpect`
- `python-dotenv`

### 2. 配置 `.env`

最小必填：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
```

一个更完整的示例：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_ENCRYPT_KEY=

SHELL_PATH=/bin/zsh
STATE_FILE=.bridge_state.json
LOG_FILE=bridge.log
HISTORY_DIR=history

FLUSH_INTERVAL_SECONDS=1.2
CODEX_FLUSH_INTERVAL_SECONDS=0.35
CARD_UPDATE_INTERVAL_SECONDS=0.8
CARD_STATUS_MAX_LINES=18
CARD_ANSWER_MAX_CHARS=2600
TYPEWRITER_STATUS_CHARS_PER_TICK=8
TYPEWRITER_ANSWER_CHARS_PER_TICK=24
MAX_MESSAGE_CHARS=1200

ALLOWED_COMMAND_PREFIXES=

FEISHU_CA_CERT_PATH=
FEISHU_INSECURE_SKIP_VERIFY=false

CARD_TEMPLATE_ID=
CARD_TEMPLATE_VAR_NAME=content
CARD_STATUS_VAR_NAME=status_content
CARD_ANSWER_VAR_NAME=answer_content

MATH_TUTOR_SYSTEM_PROMPT=
MATH_TUTOR_DOC_FOLDER_TOKEN=
MATH_TUTOR_DOC_TITLE=OpenCodex 数学辅导总结
```

### 3. 启动服务

```bash
python app.py
```

启动成功后，程序会：

- 创建本地持久 shell
- 启动飞书 Stream Mode 长连接
- 若之前已经绑定过 chat，则向该会话推送一条启动消息

## 配置项说明

### 必填配置

| 变量 | 说明 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `FEISHU_VERIFICATION_TOKEN` | 飞书事件校验 Token |

### 常用运行配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FEISHU_ENCRYPT_KEY` | 空 | 事件加密开启时使用；不开启可留空 |
| `SHELL_PATH` | `/bin/zsh` | 本地持久 shell 路径 |
| `STATE_FILE` | `.bridge_state.json` | 绑定状态与数学辅导配置文件 |
| `LOG_FILE` | `bridge.log` | 运行日志文件 |
| `HISTORY_DIR` | `history` | 历史任务与数学图片目录 |
| `ALLOWED_COMMAND_PREFIXES` | 空 | 命令前缀白名单，逗号分隔；为空表示不限制 |

### 刷新与展示相关配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLUSH_INTERVAL_SECONDS` | `1.2` | 普通 shell 输出回推节流时间 |
| `CODEX_FLUSH_INTERVAL_SECONDS` | `0.35` | Codex 模式输出处理节流时间 |
| `CARD_UPDATE_INTERVAL_SECONDS` | `0.8` | 卡片最短更新间隔 |
| `CARD_STATUS_MAX_LINES` | `18` | 卡片状态区最多保留的行数 |
| `CARD_ANSWER_MAX_CHARS` | `2600` | 卡片回答区最多保留的字符数 |
| `TYPEWRITER_STATUS_CHARS_PER_TICK` | `8` | 状态区每次“打字机推进”的字符数 |
| `TYPEWRITER_ANSWER_CHARS_PER_TICK` | `24` | 回答区每次“打字机推进”的字符数 |
| `MAX_MESSAGE_CHARS` | `1200` | 预留参数，当前版本未实际消费 |

### TLS 与企业网络配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FEISHU_CA_CERT_PATH` | 空 | 自定义 CA 证书路径；适合公司代理或私有根证书场景 |
| `FEISHU_INSECURE_SKIP_VERIFY` | `false` | 关闭 TLS 校验，仅建议临时排障使用 |

### 模板卡配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CARD_TEMPLATE_ID` | 空 | 飞书模板卡 ID；为空时自动降级为 post/text |
| `CARD_TEMPLATE_VAR_NAME` | `content` | 单变量模板模式下使用 |
| `CARD_STATUS_VAR_NAME` | `status_content` | 双变量模板里的状态区变量 |
| `CARD_ANSWER_VAR_NAME` | `answer_content` | 双变量模板里的回答区变量 |

### 数学辅导配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MATH_TUTOR_SYSTEM_PROMPT` | 空 | 默认数学辅导提示词；也可以在飞书中动态覆盖 |
| `MATH_TUTOR_DOC_FOLDER_TOKEN` | 空 | 自动创建数学总结文档时使用的目标文件夹 |
| `MATH_TUTOR_DOC_TITLE` | `OpenCodex 数学辅导总结` | 自动创建文档时的默认标题 |

## 飞书命令清单

### 基础命令

| 命令 | 说明 |
| --- | --- |
| `/bind` | 绑定当前会话，并把当前发送者记为授权账号 |
| `/help` | 查看帮助 |
| `/status` / `/ps` | 查看 bridge、模式、当前任务、卡片 ID 等状态 |
| `/ctrlc` | 向当前 shell 或当前数学辅导进程发送 Ctrl+C |

### 终端与 Codex

| 命令 | 说明 |
| --- | --- |
| `/cmd <command>` | 在本地持久 shell 里执行命令 |
| `/codex` | 进入 Codex 对话模式 |
| `/exitcodex` | 退出 Codex 对话模式 |
| `/history` | 查看最近 10 个 Codex 任务 |
| `/history <task_id>` | 查看指定 Codex 任务详情 |
| `/history clear` | 清空 Codex 历史任务 |

### 数学辅导

| 命令 | 说明 |
| --- | --- |
| `教我做题` | 进入数学辅导模式 |
| `下一题` | 结束当前题目并准备新题 |
| `结束做题` / `退出做题` / `退出数学辅导` | 退出数学辅导模式 |
| `设置数学辅导提示词 <内容>` | 设置当前持久化提示词 |
| `查看数学辅导提示词` | 查看当前生效提示词 |
| `清空数学辅导提示词` | 恢复默认数学辅导提示词 |
| `创建数学总结文档 [标题]` | 在飞书创建总结文档并绑定 |
| `绑定数学总结文档 <doc_id 或链接>` | 绑定已有文档 |
| `查看数学总结文档` | 查看当前绑定的文档 ID |
| `关闭数学总结文档` | 关闭文档同步 |

## 推荐使用流程

### 远程终端 / Codex

1. 在飞书里发送 `/bind`
2. 发送 `/status` 确认服务在线
3. 如需执行 shell 命令，直接发送 `/cmd pwd`、`/cmd git status` 等
4. 如需进入 Codex 模式，发送 `/codex`
5. 直接发送自然语言需求，例如“帮我分析当前项目的 README 还缺什么”
6. 如需结束，发送 `/exitcodex`

### 数学辅导

1. 发送 `教我做题`
2. 发送文字题目，或直接发图片
3. 继续追问时，直接发后续问题即可，仍然会写入同一题卡片
4. 切到下一题时发送 `下一题`
5. 结束辅导时发送 `结束做题`
6. 每轮讲解结束后，题干和答案会自动写入固定飞书文档；同题追问会更新原小节

## 模式差异说明

| 模式 | 触发方式 | 执行后端 | 卡片策略 | 是否记入 `/history` |
| --- | --- | --- | --- | --- |
| 普通终端模式 | `/cmd` | 本地持久 shell | 复用系统消息卡片或普通消息 | 否 |
| Codex 对话模式 | `/codex` 后发送自然语言 | `codex exec --json ...` | 每个请求一张任务卡片 | 是 |
| 数学辅导模式 | `教我做题` 后发送题目/图片 | `codex exec --image ...` / `codex exec resume ...` | 每道题一张卡片 | 否 |

## Codex 与数学模式的实现细节

### Codex 模式如何解析输出

项目会解析 `codex exec --json` 的结构化输出，只重点抽取几类事件：

- `item.started` + `command_execution`：记录“正在执行的命令”
- `item.completed` + `agent_message`：累积模型回答
- `turn.completed`：标记任务完成

因此卡片里的“状态区”和“回答区”是分开维护的，不是简单把终端全文直接贴过去。

更细的解析说明可参考 `JSON_PARSE_LOGIC.md`。

### 数学辅导如何维持多轮上下文

- 第一轮会创建一个新的 Codex 会话
- 后续追问会复用该会话的 `thread_id`
- 如果附带图片，会先下载图片到本地再传给 Codex
- 讲解内容会先做 Markdown/公式规范化，以便在飞书卡片中更稳定展示

## 持久化数据说明

### `.bridge_state.json`

保存以下状态：

- 当前授权用户 `authorized_open_id`
- 当前绑定会话 `bound_chat_id`
- 当前数学辅导提示词
- 当前绑定的数学总结文档 ID 和标题

### `history/tasks/*.json`

每个文件对应一个 Codex 任务，包含：

- `task_id`
- `prompt`
- `status`
- `commands`
- `answer_parts`
- `events`
- `card_message_id`
- `started_at` / `updated_at` / `finished_at`

### `history/math_assets/*`

保存数学辅导模式下载的图片题文件。该目录中的素材可能包含敏感内容，建议按需清理。

### `bridge.log`

运行日志会记录：

- 收到的飞书消息
- 下发到 shell 或 Codex 的命令
- shell / 数学辅导的原始输出
- 消息发送与卡片更新结果
- 证书与 TLS 相关日志

## 安全建议

- 这个项目本质上可以从飞书远程控制本地机器，只建议运行在你完全信任的设备上
- 强烈建议先发送 `/bind`，避免未授权账号误操作
- 对 `/cmd` 和 `/codex` 都建议配置 `ALLOWED_COMMAND_PREFIXES` 限制高风险命令
- `.env`、`.bridge_state.json`、`bridge.log`、`history/` 都可能包含敏感信息，不要提交到公共仓库
- 数学辅导模式会把图片写到本地磁盘，注意素材留存风险
- `FEISHU_INSECURE_SKIP_VERIFY=true` 只应用于临时排障，不要长期启用

## 常见问题

### 1. 飞书里发送 `/cmd` 或 `/codex` 后提示“命令被策略拒绝”

检查 `ALLOWED_COMMAND_PREFIXES`。

如果你配置了白名单，那么：

- `/cmd <command>` 必须以前缀白名单开头
- `/codex` 内部实际要执行的是 `codex exec ...`，所以白名单里至少要允许 `codex exec`

### 2. 卡片发送失败或看不到流式更新

优先检查：

- `CARD_TEMPLATE_ID` 是否为空
- 模板是否已发布
- 模板变量名是否与 `.env` 中配置一致
- 如果不想折腾模板卡，留空 `CARD_TEMPLATE_ID` 也能工作，只是会退回普通消息体验

### 3. 收不到机器人回复

检查：

- 服务是否真的在运行：`python app.py`
- 是否已经在目标会话里发送过 `/bind`
- 飞书应用是否启用了 Stream Mode 和消息事件订阅
- 你的飞书账号是否在应用可用范围内

### 4. 图片题无法处理

检查：

- 当前是否已经进入数学辅导模式
- 飞书应用是否具备读取图片资源的权限
- `history/math_assets/` 是否可写

### 5. 文档同步失败

检查：

- 飞书应用是否具备文档创建/写入权限
- 如果是自动创建文档，`MATH_TUTOR_DOC_FOLDER_TOKEN` 是否有效
- 已绑定的 `doc_id` 是否来自你当前可访问的文档

### 6. TLS/证书报错

优先方案：

- 配置 `FEISHU_CA_CERT_PATH=/path/to/your-ca.pem`

仅临时排障时可使用：

- `FEISHU_INSECURE_SKIP_VERIFY=true`

## 开发说明

### 本地快速自检

修改完代码后，至少可以做一次语法检查：

```bash
python -m py_compile app.py
```

### 关键文件

- `app.py`：主逻辑入口
- `README.md`：项目使用说明
- `JSON_PARSE_LOGIC.md`：Codex JSON 输出解析补充说明

## License

当前仓库未提供单独的许可证文件；如果你准备公开发布，建议补一个明确的 `LICENSE`。
