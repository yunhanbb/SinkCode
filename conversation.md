### 一、完整 JSON 结构梳理（按类型分类）
#### 1. 会话/轮次控制类
```json
{
  "type": "thread.started",
  "thread_id": "019d0562-1dbc-7122-8fa1-96b2f1fdb9d7"
}
```
```json
{
  "type": "turn.started"
}
```
```json
{
  "type": "turn.completed",
  "usage": {
    "input_tokens": 28071,
    "cached_input_tokens": 22272,
    "output_tokens": 469
  }
}
```

#### 2. 命令执行类（执行中）
```json
{
  "type": "item.started",
  "item": {
    "id": "item_0",
    "type": "command_execution",
    "command": "/bin/zsh -lc 'pwd && ls -la'",
    "aggregated_output": "",
    "exit_code": null,
    "status": "in_progress"
  }
}
```

#### 3. 命令执行类（执行完成）
```json
{
  "type": "item.completed",
  "item": {
    "id": "item_0",
    "type": "command_execution",
    "command": "/bin/zsh -lc 'pwd && ls -la'",
    "aggregated_output": "/Users/m2/Desktop/OpenCodex\n...（命令输出内容）...",
    "exit_code": 0,
    "status": "completed"
  }
}
```

#### 4. 智能总结类
```json
{
  "type": "item.completed",
  "item": {
    "id": "item_2",
    "type": "agent_message",
    "text": "当前目录 `/Users/m2/Desktop/OpenCodex` 文件结构如下（已按用途梳理）：...（结构化总结）..."
  }
}
```

### 二、字段价值判断（按保留优先级）
| 优先级 | 字段/类型                | 核心作用                                  | 是否建议保留 |
|--------|--------------------------|-------------------------------------------|--------------|
| 高     | agent_message 的 text    | 结构化的文件梳理结果（核心价值）          | 是           |
| 高     | command_execution 的 aggregated_output | 原始命令输出（可核对总结准确性）       | 可选（建议保留） |
| 中     | command_execution 的 command | 执行的具体命令（了解梳理逻辑）          | 可选         |
| 低     | thread.started/turn.started | 会话/轮次控制（仅流程标识，无业务价值） | 否           |
| 低     | turn.completed 的 usage  | 令牌消耗（仅计费参考）                    | 否           |
| 低     | item.started 类型        | 命令执行中状态（无最终结果）              | 否           |
| 低     | id/exit_code/status      | 执行状态标识（辅助信息）                  | 可选（仅排查问题时保留） |

### 三、精简后的核心保留格式示例
如果你只需要核心信息，可保留如下结构：
```json
{
  "梳理结果": "当前目录 `/Users/m2/Desktop/OpenCodex` 文件结构如下（已按用途梳理）：\n\n- **核心代码**\n  - `app.py`：主程序文件（约 18KB）\n  - `requirements.txt`：Python 依赖列表\n\n- **说明文档**\n  - `README.md`：项目说明文档（约 2.6KB）\n\n- **运行/缓存产物**\n  - `__pycache__/`：Python 编译缓存目录\n  - `__pycache__/app.cpython-311.pyc`：`app.py` 的字节码缓存\n  - `.bridge_state.json`：运行状态类文件（小型 JSON）\n\n- **环境相关**\n  - `.venv/`：本地虚拟环境目录（含 `bin/`、`include/`、`lib/`、`pyvenv.cfg`）\n  - `.env`：环境变量配置文件（通常包含敏感信息，注意别提交到仓库）",
  "原始命令输出": {
    "pwd && ls -la": "/Users/m2/Desktop/OpenCodex\n...（原始输出）...",
    "find . -maxdepth 2 -mindepth 1 | sort": "./.bridge_state.json\n...（原始输出）..."
  }
}
```

### 总结
1. **核心保留项**：`agent_message` 类型下的 `text` 字段（结构化梳理结果）是最有价值的内容，建议优先保留；
2. **辅助保留项**：命令执行完成后的 `aggregated_output`（原始输出）可用于核对结果准确性，按需保留；
3. **可舍弃项**：所有会话控制类（thread/turn）、执行中状态（item.started）、令牌消耗（usage）等字段无业务价值，可直接剔除。