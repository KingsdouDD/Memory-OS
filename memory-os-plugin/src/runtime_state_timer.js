// Runtime State Timer (v2 - LLM-direct)
//
// 设计：
// - 监听 message_received hook，每次新消息就清零 timer
// - 无活动超过 timeoutMs → timer fire
// - timer fire 时：插件自己直接调 LLM（不伪造消息、不发用户输入）
//   1. 读 state file 拿到 last_snapshot_time
//   2. 用 plugin SDK 的 readSessionTranscriptEvents() 读完整对话
//   3. 过滤 timestamp >= last_snapshot_time 的消息
//   4. 拼 prompt（system: 你是状态对比器, user: 上次状态 + 当前对话片段）
//   5. 调 completeSimple() → 拿到 status
//   6. 按 status 分流：
//      - completed → memory_os_ingest()
//      - ongoing   → 写 state file (覆盖旧的 + 更新 last_snapshot_time)
//      - stalled   → 标记 stalled 暂存
// - 所有 LLM 调用走 OpenClaw plugin SDK（completeSimple），不写死 model/provider
//
// 监控日志：~/.openclaw/workspace/memory-os/logs/runtime_state_timer.log

import path from "node:path";
import fs from "node:fs";

// ── 配置 ──────────────────────────────────────────────
const PLUGIN_ROOT = path.resolve(new URL(import.meta.url).pathname, "..", "..");
const DEFAULT_TIMEOUT_MS = 3 * 60 * 1000; // 3 分钟
const DEFAULT_PROMPT_PATH = path.join(PLUGIN_ROOT, "prompts", "runtime_state_diff_extract.md");
const STATE_DIR = process.env.MEMORY_OS_RUNTIME_STATE_DIR ||
  path.join(process.env.HOME, ".openclaw", "workspace", "memory-os", "runtime_active_state");
const LOG_PATH = process.env.MEMORY_OS_TIMER_LOG ||
  path.join(process.env.HOME, ".openclaw", "workspace", "memory-os", "logs", "runtime_state_timer.log");

// ── 工具函数 ──────────────────────────────────────────
function ts() { return new Date().toISOString().replace("T", " ").slice(0, 19); }

function appendLog(level, tag, msg, extra = {}) {
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    const line = `[${ts()}] [${level}] [${tag}] ${msg} ${JSON.stringify(extra)}\n`;
    fs.appendFileSync(LOG_PATH, line);
  } catch (e) {
    console.error("[runtime_state_timer] log write failed:", e.message);
  }
}

function ensureStateDir() {
  try { fs.mkdirSync(STATE_DIR, { recursive: true }); }
  catch (e) { appendLog("ERROR", "STATE_DIR", "mkdir failed", { dir: STATE_DIR, error: e.message }); }
}

function safeSessionKey(sessionKey) {
  return String(sessionKey).replace(/[^a-zA-Z0-9_.-]/g, "_");
}
function statePath(sessionKey) {
  return path.join(STATE_DIR, `${safeSessionKey(sessionKey)}.json`);
}
function readState(sessionKey) {
  try {
    const p = statePath(sessionKey);
    if (!fs.existsSync(p)) return null;
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch (e) {
    appendLog("WARN", "STATE_READ", "failed to read state", { sessionKey, error: e.message });
    return null;
  }
}
function writeState(sessionKey, state) {
  try {
    ensureStateDir();
    fs.writeFileSync(statePath(sessionKey), JSON.stringify(state, null, 2), "utf8");
  } catch (e) {
    appendLog("ERROR", "STATE_WRITE", "failed to write state", { sessionKey, error: e.message });
  }
}

// ── Timer 管理 ────────────────────────────────────────
const _timers = new Map(); // sessionKey -> { timer, lastActivity, timeoutMs, lastSnapshotTime, sessionId }

function _clearTimer(sessionKey) {
  const entry = _timers.get(sessionKey);
  if (entry?.timer) {
    clearTimeout(entry.timer);
    appendLog("DEBUG", "TIMER_CLEAR", "timer cleared", { sessionKey });
  }
}

function _armTimer(sessionKey, timeoutMs, fireCallback, lastSnapshotTime) {
  _clearTimer(sessionKey);
  const timer = setTimeout(() => {
    appendLog("INFO", "TIMER_FIRE", "timer fired", { sessionKey });
    try { fireCallback(); }
    catch (err) {
      appendLog("ERROR", "TIMER_FIRE", "fire callback failed", { sessionKey, error: err.message });
    } finally {
      _timers.delete(sessionKey);
    }
  }, timeoutMs);
  _timers.set(sessionKey, {
    timer,
    lastActivity: Date.now(),
    timeoutMs,
    lastSnapshotTime: lastSnapshotTime || Date.now(),
  });
  appendLog("DEBUG", "TIMER_ARM", "timer armed", { sessionKey, timeoutMs, lastSnapshotTime });
}

// ── 拿 session 对话内容 ──────────────────────────────
// 用 plugin SDK 读 transcript，按 lastSnapshotTime 过滤
async function _fetchSessionMessages(sessionKey, sessionId, lastSnapshotTime) {
  try {
    // 必须有 sessionId SDK 才能走，没有就 fallback 返回空数组
    if (!sessionId || typeof sessionId !== "string" || !sessionId.trim()) {
      appendLog("WARN", "FETCH_MSGS", "no sessionId, SDK requires it; falling back to empty messages", {
        sessionKey,
        lastSnapshotTime,
        note: "hook event 里 event.sessionId/ctx.sessionId 都是 undefined；只能让 LLM 仅看上次的 state file",
      });
      return [];
    }

    // 尝试从 plugin SDK 加载
    let readEvents;
    try {
      const sdk = await import("openclaw/plugin-sdk/session-transcript-runtime");
      readEvents = sdk.readSessionTranscriptEvents;
    } catch (e1) {
      try {
        const sdk = await import("/opt/homebrew/lib/node_modules/openclaw/dist/plugin-sdk/session-transcript-runtime.js");
        readEvents = sdk.readSessionTranscriptEvents;
      } catch (e2) {
        appendLog("ERROR", "SDK_LOAD", "failed to load session-transcript-runtime", {
          e1: e1.message,
          e2: e2.message,
        });
        return null;
      }
    }
    if (typeof readEvents !== "function") {
      appendLog("ERROR", "SDK_API", "readSessionTranscriptEvents not found");
      return null;
    }

    // 解析 sessionKey 拿 agentId
    let agentId = "main";
    if (typeof sessionKey === "string" && sessionKey.startsWith("agent:")) {
      const parts = sessionKey.split(":");
      if (parts[1]) agentId = parts[1];
    }

    const events = await readEvents({
      agentId,
      sessionKey,
      sessionId,
    });

    appendLog("DEBUG", "FETCH_MSGS", "session events fetched", {
      sessionKey,
      eventCount: Array.isArray(events) ? events.length : 0,
      sinceMs: lastSnapshotTime,
    });

    // 过滤 lastSnapshotTime 之后的消息
    // events 格式: { role, content, timestamp?, ... } (按 SDK 实际结构)
    const cutoffMs = lastSnapshotTime || 0;
    const messages = (events || [])
      .filter((ev) => {
        const ts = ev?.timestamp || ev?.ts || ev?.created_at || 0;
        // ts 可能是 ms、ISO 字符串、或者秒
        let ms = ts;
        if (typeof ms === "string") ms = new Date(ms).getTime();
        else if (typeof ms === "number" && ms < 1e12) ms = ms * 1000; // 秒 → 毫秒
        return ms >= cutoffMs;
      })
      .map((ev) => ({
        role: ev?.role || (ev?.type === "assistant" ? "assistant" : "user"),
        content: _extractText(ev),
        timestamp: ev?.timestamp || ev?.ts || ev?.created_at,
      }));

    appendLog("INFO", "FETCH_MSGS", "filtered messages", {
      sessionKey,
      totalEvents: Array.isArray(events) ? events.length : 0,
      sinceCutoff: messages.length,
    });

    return messages;
  } catch (err) {
    appendLog("ERROR", "FETCH_MSGS", "fetch session messages failed", {
      sessionKey,
      error: err.message,
      stack: err.stack,
    });
    return null;
  }
}

function _extractText(ev) {
  const c = ev?.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((part) => {
        if (typeof part === "string") return part;
        if (part?.type === "text") return part.text || "";
        if (part?.text) return part.text;
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  if (c?.text) return c.text;
  return "";
}

// ── 调 LLM（OpenClaw plugin SDK completeSimple）───────
async function _callLLM(systemPrompt, userPrompt) {
  let completeSimple;
  try {
    const sdk = await import("openclaw/plugin-sdk/llm");
    completeSimple = sdk.completeSimple;
  } catch (e1) {
    try {
      const sdk = await import("/opt/homebrew/lib/node_modules/openclaw/dist/plugin-sdk/llm.js");
      completeSimple = sdk.completeSimple;
    } catch (e2) {
      appendLog("ERROR", "SDK_LOAD", "failed to load llm sdk", {
        e1: e1.message,
        e2: e2.message,
      });
      return null;
    }
  }
  if (typeof completeSimple !== "function") {
    appendLog("ERROR", "SDK_API", "completeSimple not found");
    return null;
  }
  try {
    const result = await completeSimple({
      // 不传 model → 用 OpenClaw 当前默认
      system: systemPrompt,
      messages: [{ role: "user", content: userPrompt }],
    });
    appendLog("DEBUG", "LLM_CALL", "completeSimple returned", {
      hasResult: !!result,
      keys: result ? Object.keys(result) : null,
    });
    // 提取 content
    const content = result?.content ?? result?.text ?? result?.message?.content ?? "";
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content.map((p) => p?.text || "").filter(Boolean).join("\n");
    }
    return "";
  } catch (err) {
    appendLog("ERROR", "LLM_CALL", "completeSimple failed", {
      error: err.message,
      stack: err.stack,
    });
    return null;
  }
}

function _tryParseJSON(s) {
  if (!s || typeof s !== "string") return null;
  let text = s.trim();
  // 去掉 markdown code fence
  if (text.startsWith("```")) {
    const lines = text.split("\n");
    if (lines[0].startsWith("```")) lines.shift();
    if (lines[lines.length - 1].startsWith("```")) lines.pop();
    text = lines.join("\n").trim();
  }
  try { return JSON.parse(text); } catch { return null; }
}

// ── 加载 prompt 模板 ──────────────────────────────────
function _loadPromptTemplate() {
  const promptPath = process.env.MEMORY_OS_RUNTIME_PROMPT_PATH || DEFAULT_PROMPT_PATH;
  try {
    return fs.readFileSync(promptPath, "utf8");
  } catch (e) {
    appendLog("ERROR", "PROMPT_LOAD", "failed to load prompt", { promptPath, error: e.message });
    return null;
  }
}

// ── timer fire 时真正干活 ──────────────────────────────
async function _onTimerFire(sessionKey, sessionId, lastSnapshotTime, ingestFn) {
  const now = Date.now();
  const sinceMs = lastSnapshotTime || 0;
  appendLog("INFO", "FIRE_START", "begin state diff extract", {
    sessionKey,
    sinceMs,
    elapsedMs: now - sinceMs,
  });

  // 1. 读上次状态
  const prevState = readState(sessionKey);

  // 2. 读从上次到现在的对话
  const newMessages = await _fetchSessionMessages(sessionKey, sessionId, sinceMs);
  if (newMessages === null) {
    appendLog("ERROR", "FIRE", "fetch messages failed, skip");
    return;
  }
  // 原本这里 if (newMessages.length === 0) 提前 return，现在改为继续走 LLM 让它判断。
  // 如果之前已经有 prevState 也一起送过去，让模型决定 stalled 还是其他状态。
  if (newMessages.length === 0) {
    appendLog("INFO", "FIRE", "no new messages, still call LLM with prev state to decide", {
      sessionKey,
      sinceMs,
    });
  }

  // 3. 加载 prompt 模板
  const promptTemplate = _loadPromptTemplate();
  if (!promptTemplate) return;

  // 4. 拼 user prompt：上次状态 + 当前对话
  const userPrompt = JSON.stringify({
    previous_state: prevState || { note: "首次检查，无历史状态" },
    new_messages: newMessages,
    snapshot_window: {
      from_ms: sinceMs,
      to_ms: now,
      from_iso: new Date(sinceMs).toISOString(),
      to_iso: new Date(now).toISOString(),
    },
  }, null, 2);

  // 5. 调 LLM
  const llmOutput = await _callLLM(promptTemplate, userPrompt);
  if (!llmOutput) {
    appendLog("ERROR", "FIRE", "LLM returned no output, skip");
    return;
  }

  // 6. 解析结果
  const result = _tryParseJSON(llmOutput);
  if (!result) {
    appendLog("ERROR", "FIRE", "LLM output not JSON, raw kept", {
      rawPreview: llmOutput.slice(0, 500),
    });
    return;
  }
  appendLog("INFO", "FIRE_LLM", "LLM result parsed", {
    status: result.status,
    action: result.action,
    summary: (result.current_summary || "").slice(0, 200),
  });

  // 7. 按 status 分流
  const status = result.status || "ongoing";
  const action = result.action || "update_state";

  // 7a. completed → 调 ingestFn 写 Memory-OS
  if (status === "completed" && action === "ingest_to_memory" && typeof ingestFn === "function") {
    try {
      const ingestPayload = result.memory_payload || {
        l0: { scene_summary: result.current_summary || "", source: `runtime:YYYY-MM-DD` },
        l1: { kos: result.last_kos || [] },
        l2: { scenario: null },
        l3: { persona: [] },
      };
      // 修正 source date
      if (ingestPayload.l0 && ingestPayload.l0.source) {
        ingestPayload.l0.source = `runtime:${new Date(now).toISOString().slice(0, 10)}`;
      }
      const ingestResult = await ingestFn(ingestPayload);
      appendLog("INFO", "INGEST", "memory ingested via plugin ingestFn", {
        ok: !!ingestResult,
      });
    } catch (e) {
      appendLog("ERROR", "INGEST", "ingestFn failed", { error: e.message });
    }
    // 清空 state file（任务完成）
    try { fs.unlinkSync(statePath(sessionKey)); } catch {}
    return;
  }

  // 7b. ongoing / stalled → 写 state file
  const nextState = {
    last_snapshot_time: now,
    last_status: status,
    last_summary: result.current_summary || "",
    last_kos: result.last_kos || [],
    next_step: result.next_step || null,
    blocked_reason: result.blocked_reason || null,
    diff_notes: result.diff_notes || [],
    updated_at: new Date(now).toISOString(),
  };
  writeState(sessionKey, nextState);
  appendLog("INFO", "STATE_UPDATED", "state file written", {
    sessionKey,
    status,
    nextStep: nextState.next_step,
  });
}

// ── 主入口 ────────────────────────────────────────────
export default {
  /**
   * 在 message_received hook 中调用：清零 timer + 重启
   * 必须传 ingestFn (async function that takes the memory payload)
   */
  onActivity({ sessionKey, sessionId, ingestFn }) {
    if (!sessionKey) return;
    ensureStateDir();
    const timeoutMs = Number(process.env.MEMORY_OS_RUNTIME_TIMEOUT_MS || DEFAULT_TIMEOUT_MS);

    // 取上次的 last_snapshot_time，作为下次 timer fire 的截止起点
    const prev = readState(sessionKey);
    const lastSnapshotTime = prev?.last_snapshot_time || Date.now();

    _armTimer(
      sessionKey,
      timeoutMs,
      () => _onTimerFire(sessionKey, sessionId, lastSnapshotTime, ingestFn),
      lastSnapshotTime,
    );
    appendLog("INFO", "ACTIVITY", "activity detected, timer reset", {
      sessionKey,
      timeoutMs,
      lastSnapshotTime,
    });
  },

  fireNow(params) {
    const { sessionKey, sessionId, ingestFn } = params || {};
    if (!sessionKey) return;
    _clearTimer(sessionKey);
    const prev = readState(sessionKey);
    const lastSnapshotTime = prev?.last_snapshot_time || Date.now();
    return _onTimerFire(sessionKey, sessionId, lastSnapshotTime, ingestFn);
  },

  listActive() {
    const result = [];
    for (const [k, v] of _timers.entries()) {
      result.push({
        sessionKey: k,
        lastActivity: v.lastActivity,
        timeoutMs: v.timeoutMs,
        lastSnapshotTime: v.lastSnapshotTime,
      });
    }
    return result;
  },

  // 暴露给 plugin entry 注入 ingestFn
  _onTimerFire,
};
