# Codex JSON 事件解析逻辑说明

本文说明 `app.py` 中把 `codex exec --json` 输出转换为手机可读进度的逻辑。

## 1. 入口与触发条件

- 入口函数：`FeishuCodexBridge._format_for_mobile(text, flush_tail=False)`
- 仅在 `codex_mode=True` 时启用 JSON 包装；非 Codex 模式直接返回原始文本。
- 上游调用点：`_forward_terminal_output()`，在每次终端输出刷新的时候调用。

## 2. 分片拼接（处理半截 JSON）

`codex exec --json` 的输出可能被拆成多段，单次读到的 `text` 不一定是完整行。

- 使用成员变量：`self._mobile_line_carry`
- 处理流程：
  1. `data = self._mobile_line_carry + text`
  2. 按 `\n` 切分为 `lines`
  3. 如果本次文本未以换行结束，则把最后一段缓存到 `self._mobile_line_carry`
  4. 下次输出到来时再拼接，确保尽量按“完整行”解析 JSON
- 在 `/ctrlc` 和 `/exitcodex` 时会清空缓存，避免脏数据串行。

## 3. 行级处理总流程

对每一行（去空白后）依次处理：

1. 先尝试当作 JSON：调用 `_format_codex_json_line(line)`
2. 若是 JSON 且成功解析：输出包装后的可读文本
3. 若不是 JSON：走降级逻辑
   - 过滤噪声（如 `codex`、shell 提示符 `(base) ...`、`% ...`）
   - `Error:` 前缀转为 `[错误] ...`
   - 其余文本原样保留

## 4. JSON 行解析规则

函数：`_format_codex_json_line(line)`

### 4.1 判断是否 JSON 行

- 必须同时满足：`line.startswith('{')` 且 `line.endswith('}')`
- 再用 `json.loads(line)` 解析；失败则返回 `None`，交给降级逻辑处理

### 4.2 提取事件类型与关键信息

- 事件类型字段按优先级读取：`type` -> `event` -> `kind`
- 文本字段通过递归抽取（`_pick_first_text`）：
  - 优先键：`delta`, `text`, `content`, `message`, `summary`, `status`, `result`, `output`
- 命令字段优先抽取：`command`, `cmd`

### 4.3 文本包装映射

按顺序匹配：

- 有命令：`[执行] <command>`
- 事件类型含 `error`：`[错误] ...`
- 事件类型含 `completed/finished`：`[完成] ...`
- 事件类型含 `start/begin`：`[开始] ...`
- 事件类型含 `tool` 且有文本：`[工具] ...`
- 事件类型含 `reason` 且有文本：`[思考] ...`
- 有文本：直接输出文本
- 仅有事件类型：`[事件] <event_type>`
- 都没有：返回空字符串

## 5. 递归抽取文本细节

函数：`_pick_first_text(obj, keys)` + `_to_short_text(value)`

- 支持 `dict` / `list` 深度遍历
- 命中目标键后，把值转换为短文本
- 文本截断到 300 字，防止单条过长
- 数值/布尔会转字符串
- 复杂对象（如 dict/list 本身）不直接转字符串，避免噪声

## 6. 最终发送策略

- `_format_for_mobile()` 把可读行汇总后以换行拼接
- `_forward_terminal_output()` 仅当结果非空时才调用 `_send_bound_chat()`
- 发送前仍受 `_chunk_text()` 分片上限控制（避免超长消息失败）

## 7. 设计目标

- 手机端：可读的进度流（而不是原始 JSON）
- 电脑端：保留原始终端输出，方便调试
- 兼容流式分段输出，减少“半截 JSON”污染消息
