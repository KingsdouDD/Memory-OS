#!/bin/bash
# Memory OS 梦境入库 cron 脚本（command 模式，绕过 recall hook）
# 逻辑：算昨天日期 → 读梦境文件 → agent 抽取 KO（用默认模型）→ process_dream.py 写库 → 发 QQ 通知

set -e

VENV="/Users/king/.openclaw/workspace/memory-os/venv/bin/python3"
SCRIPT="/Users/king/.openclaw/workspace/memory-os-plugin/scripts/process_dream.py"
DREAM_DIR="/Users/king/.openclaw/workspace/memory/dreaming"
EXTRACT_PROMPT="/Users/king/.openclaw/workspace/memory-os-plugin/scripts/extract_prompt.md"
DATE=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d "yesterday" +%Y-%m-%d)
LIGHT="$DREAM_DIR/light/$DATE.md"
REM="$DREAM_DIR/rem/$DATE.md"
OUT="/tmp/memory-os-kos-$DATE.json"
INPUT_FILE="/tmp/memory-os-input-$DATE.json"

echo "[$(date)] Starting dream ingestion for $DATE"

# 检查文件
if [ ! -f "$LIGHT" ] && [ ! -f "$REM" ]; then
    echo "No dream files for $DATE, skipping."
    exit 0
fi

# 读取梦境文件内容（合并 light + rem）
CONTENT=""
if [ -f "$LIGHT" ] && [ $(wc -c < "$LIGHT") -gt 100 ]; then
    CONTENT="$CONTENT\n=== LIGHT ===\n$(cat "$LIGHT")"
fi
if [ -f "$REM" ] && [ $(wc -c < "$REM") -gt 100 ]; then
    CONTENT="$CONTENT\n=== REM ===\n$(cat "$REM")"
fi

if [ -z "$CONTENT" ]; then
    echo "Dream files empty, skipping."
    exit 0
fi

# 构造输入：system prompt + 梦境内容（openclaw agent 没有 --system 参数，只能放 message 里）
cat > "$INPUT_FILE" << EOF
你是 Memory OS 知识抽取器。从梦境文本里抽取 KO，按 extract_prompt.md 规范输出 JSON。只输出 JSON 数组，不要任何其他内容。

extract_prompt.md 路径：$EXTRACT_PROMPT

=== 梦境内容 ===
$(echo -e "$CONTENT")
EOF

# 调用 agent 抽取 KO（用默认模型，不写死具体模型）
# --local 表示用本地 embedded agent（走 workspace 的模型配置）
# --session-key 保证会话稳定
# 注意：openclaw agent 的输出混有日志行，需要过滤出 JSON 数组部分
KO_RAW=$(openclaw agent --local --message-file "$INPUT_FILE" --thinking off --session-key agent:memory-os:dream-ingest 2>/dev/null)

# 清理临时输入文件
rm -f "$INPUT_FILE"

# 从混杂日志的输出中提取纯 JSON 数组
KO_JSON=$(python3 -c "
import sys, json, re
text = sys.stdin.read()
# 找所有 JSON 数组匹配，取最后一个（通常是模型输出的最终结果）
matches = re.findall(r'\[[\s\S]*\]', text)
if matches:
    # 尝试解析每个匹配，取有效的 JSON 数组
    for m in reversed(matches):
        try:
            parsed = json.loads(m)
            if isinstance(parsed, list):
                print(m)
                sys.exit(0)
        except:
            continue
print('[]')
" <<< "$KO_RAW" 2>/dev/null)

if [ -z "$KO_JSON" ] || [ "$KO_JSON" = "[]" ]; then
    echo "No KO extracted, skipping write."
    echo "梦境 $DATE 无有效内容，跳过。"
    exit 0
fi

# 写临时文件
echo "$KO_JSON" > "$OUT"
echo "Extracted $(echo $KO_JSON | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))') KOs"

# 写入数据库
RESULT=$($VENV $SCRIPT ingest-kos --file "$OUT" --source "dream:$DATE" 2>&1)
echo "Write result: $RESULT"

# 解析结果并发 QQ 通知
CREATE=$(echo "$RESULT" | python3 -c "import sys,json,re; d=json.loads(sys.stdin.read()); r=d.get('write_report',{}); print(f\"create:{r.get('create',0)} update:{r.get('update',0)} errors:{r.get('errors',0)} neo4j:{r.get('neo4j',{}).get('entities',0)}e/{r.get('neo4j',{}).get('relations',0)}r qdrant:{r.get('qdrant_written',0)}w\")" 2>/dev/null || echo "parse failed")
MSG="🍊 Memory OS 梦境入库完成 ($DATE)
$CREATE"
echo "$MSG"

# 发 QQ 通知
openclaw tell qqbot:c2c:F10B2B32E462FDBD43462C3258755CE9 "$MSG" 2>/dev/null || true

echo "[$(date)] Done"
