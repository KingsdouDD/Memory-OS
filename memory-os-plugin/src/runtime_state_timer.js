// Runtime State Timer
//
// 监听 user_request hook，每次用户/Agent 发消息就清零 timer。
// 10 分钟无新消息，timer 归零 → 自动往当前 session 发一条"探查消息"，
// 让主 Agent 自己读 prompt + 处理状态抽取 + 写入 Memory-OS。
//
// 关键设计：
// - timer 是 session 级别的（按 sessionKey 独立计时）
// - 不直接调 LLM，不写死 provider
// - 探查消息走正常 message 通道，自动用当前会话的模型
// - 临时状态文件存到 runtime_active_state/{sessionKey}.json
//
// 监控日志：~/.openclaw/workspace/memory-os/logs/runtime_state_timer.log

import path from "node:path";
import fs from "node:fs";
import os from "node:os";

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
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
  } catch (e) {
    appendLog("ERROR", "STATE_DIR", "failed to create state dir", { dir: STATE_DIR, error: e.message });
  }
}

// ── Timer 管理 ────────────────────────────────────────
// 每个 sessionKey 一个 timer
const _timers = new Map(); // sessionKey -> { timer, lastActivity, timeoutMs }

function _clearTimer(sessionKey) {
  const entry = _timers.get(sessionKey);
  if (entry?.timer) {
    clearTimeout(entry.timer);
    appendLog("DEBUG", "TIMER_CLEAR", "timer cleared", { sessionKey });
  }
}

function _armTimer(sessionKey, timeoutMs, fireCallback) {
  _clearTimer(sessionKey);
  const timer = setTimeout(() => {
    appendLog("INFO", "TIMER_FIRE", "timer fired, executing probe callback", { sessionKey });
    try {
      fireCallback();
    } catch (err) {
      appendLog("ERROR", "TIMER_FIRE", "probe callback failed", { sessionKey, error: err.message });
    } finally {
      _timers.delete(sessionKey);
    }
  }, timeoutMs);
  _timers.set(sessionKey, { timer, lastActivity: Date.now(), timeoutMs });
  appendLog("DEBUG", "TIMER_ARM", "timer armed", { sessionKey, timeoutMs });
}

// ── 构造探查消息 ──────────────────────────────────────
function buildProbeMessage(promptPath, stateFilePath, sessionKey) {
  return [
    "[Memory-OS 自动状态检查]",
    `当前会话（${sessionKey}）已停止活动超过阈值，请按以下流程处理：`,
    "",
    `1. 读取 prompt 文件：${promptPath}`,
    `2. 读取上次临时状态（如果存在）：${stateFilePath}`,
    "3. 按 prompt 里的规则，抽取当前对话的最新任务状态",
    "4. 把'上次状态 vs 当前状态'做对比，判断：",
    "   - status: completed → 提炼成 L0-L3 写入 Memory-OS（用 memory_os_ingest）",
    "   - status: ongoing → 把最新状态覆盖到临时状态文件",
    "   - status: stalled → 标记 stalled 暂不写入",
    "5. 不要在用户频道回复，只在内部处理完即可",
    "",
    "这条消息是插件自动生成的定时探查，由你（主 Agent）处理。"
  ].join("\n");
}

// ── 主入口 ────────────────────────────────────────────
export default {
  /**
   * 在 user_request hook 中调用：每次新消息 → 清零 timer + 重启
   */
  onActivity(sessionKey, event, ctx) {
    ensureStateDir();
    const timeoutMs = Number(process.env.MEMORY_OS_RUNTIME_TIMEOUT_MS || DEFAULT_TIMEOUT_MS);
    _armTimer(sessionKey, timeoutMs, () => _fireProbe(sessionKey, event, ctx));
    appendLog("INFO", "ACTIVITY", "activity detected, timer reset", {
      sessionKey,
      timeoutMs,
    });
  },

  /**
   * 在 agent_end / session_end 时调用：立刻触发探查（不等 timer）
   */
  fireNow(sessionKey, event, ctx) {
    appendLog("INFO", "FIRE_NOW", "fire probe immediately", { sessionKey });
    _clearTimer(sessionKey);
    try {
      _fireProbe(sessionKey, event, ctx);
    } catch (err) {
      appendLog("ERROR", "FIRE_NOW", "fireNow failed", { sessionKey, error: err.message });
    }
  },

  /**
   * 调试用：列出当前所有活跃 timer
   */
  listActive() {
    const result = [];
    for (const [k, v] of _timers.entries()) {
      result.push({ sessionKey: k, lastActivity: v.lastActivity, timeoutMs: v.timeoutMs });
    }
    return result;
  }
};

// ── 内部：timer 归零后真正发探查消息 ────────────────────
function _fireProbe(sessionKey, event, ctx) {
  const promptPath = process.env.MEMORY_OS_RUNTIME_PROMPT_PATH || DEFAULT_PROMPT_PATH;
  const safeKey = String(sessionKey).replace(/[^a-zA-Z0-9_.-]/g, "_");
  const stateFilePath = path.join(STATE_DIR, `${safeKey}.json`);
  const probeMsg = buildProbeMessage(promptPath, stateFilePath, sessionKey);

  appendLog("INFO", "PROBE", "sending probe message to session", {
    sessionKey,
    promptPath,
    stateFilePath,
    msgLen: probeMsg.length,
  });

  // 把探查消息通过 ctx 暴露的 message/send 接口发出去（具体调用方式由 OpenClaw 提供）
  if (typeof ctx?.sendMessage === "function") {
    ctx.sendMessage(sessionKey, probeMsg).catch((err) => {
      appendLog("ERROR", "PROBE_SEND", "sendMessage failed", { sessionKey, error: err.message });
    });
  } else if (typeof ctx?.enqueueMessage === "function") {
    ctx.enqueueMessage(sessionKey, probeMsg);
  } else {
    appendLog("WARN", "PROBE_SEND", "no message-send API available in ctx", {
      sessionKey,
      ctxKeys: Object.keys(ctx || {}),
    });
  }
}