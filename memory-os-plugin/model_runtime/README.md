# model_runtime

统一模型运行时，解决 Memory OS 写入去重 bug。

## 目录结构

```
model_runtime/
├── __init__.py      # 统一导出
├── discovery.py     # 获取当前活跃模型
├── caller.py        # 统一调用入口（多后端路由）
├── decide.py        # LLM 驱动的记忆去重决策
└── README.md
```

## 模块概览

### discovery
获取"当前系统正在使用的模型"，与 OpenClaw session 保持一致。系统切模型 → 自动跟随。

```python
from model_runtime import get_active_model
info = get_active_model()
print(info.model_id)  # "minimax/MiniMax-M3"
print(info.backend)   # "minimax"
```

探测优先级：
1. 环境变量 `MEMORY_OS_DECISION_MODEL` / `OPENCLAW_ACTIVE_MODEL`
2. OpenClaw session_status（`sessions.recent[0].selectedModel`）
3. 配置文件 `.openclaw/config.yaml`
4. 兜底 `minimax/MiniMax-M3`

### caller
统一调用入口，自动按 backend 路由到对应实现。

支持后端：
- `ollama`（本地）
- `mlx`（本地）
- `minimax` / `openai` / `api`（云端 OpenAI 兼容）
- `anthropic`（占位，需补 SDK）

```python
from model_runtime import call_with_active_model, call_with_model

# 用当前主模型
text = call_with_active_model("你好")

# 指定模型
text = call_with_model("ollama/qwen2.5:7b", "你好")
```

### decide
LLM 驱动的记忆去重决策（替代原 `_rule_decide_layer_action`）。

```python
from model_runtime import llm_decide_action

action, reason = llm_decide_action(
    new_text="外婆半夜帮我盘猪老二",
    candidates=[{"pid": "...", "score": 0.891, "summary": "外婆与老豆关系亲密", "state": "active"}],
    new_state="historical",
    layer="L3",
)
```

返回值：
- `CREATE`：新建
- `UPDATE`：补充旧记录
- `MERGE`：两条都保留（同一事件不同侧面）
- `SKIP`：完全重复
- `INVALIDATE`：旧记录作废

调用失败时自动降级到原规则（`_fallback_rule_decide`）。

## CLI 调试

```bash
# 查看当前模型
python3 model_runtime/discovery.py

# 列出所有可用模型
python3 model_runtime/discovery.py --list

# 测试一次决策
python3 model_runtime/decide.py
```

## 待办

- [ ] 接入 Anthropic SDK（Claude 后端）
- [ ] `write_4layer.py` 替换 `_rule_decide_layer_action` 为 `llm_decide_action`
- [ ] 决策日志（每次 LLM 决策记入 `logs/decide.log` 用于回溯）
- [ ] 决策性能埋点（命中率 / 调用延迟 / fallback 比例）
