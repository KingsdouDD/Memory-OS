// Plugin entry: Memory OS
// Hooks into OpenClaw lifecycle to inject relevant memories into prompts
// and write dream reports into Neo4j + Qdrant at the scheduled time.
//
// 监控日志：~/.openclaw/workspace/memory-os/logs/hook-trace.md (markdown append，直接在电脑上 cat/less 看)
//   - recall_skipped       (门控跳过)
//   - injection_committed  (记忆块真的拼好即将 prepend 到 system)
//   - recall_failed        (Python 召回失败)
//   - recall_cache_hit     (同会话同 query 去重命中)

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import crypto from "node:crypto";
import * as os from "node:os";
import { Path } from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PYTHON_SCRIPT = path.resolve(__dirname, "../scripts/process_dream.py");
const _expand = (v) => String(v || "").replace(/\${HOME}/g, process.env.HOME || "").replace(/^~/, process.env.HOME || "");
const PYTHON_BIN = process.env.MEMORY_OS_PYTHON || (process.env.HOME + "/.openclaw/workspace/memory-os/venv/bin/python3");
const LOG_PATH = _expand(process.env.MEMORY_OS_HOOK_LOG ||
  "${HOME}/.openclaw/workspace/memory-os/logs/hook-trace.md");
// HOOK_TRACE_ENABLED=1 写日志，HOOK_TRACE_ENABLED=0 关闭（默认开）
const HOOK_TRACE_ENABLED = process.env.MEMORY_OS_HOOK_TRACE_ENABLED !== "0";
// md 日志不像 jsonl 能按行切窗口；这里用字节上限来限大小（默认 256 KB）
const LOG_KEEP_BYTES = Number(process.env.MEMORY_OS_HOOK_LOG_KEEP_BYTES || 256 * 1024);

// ── 服务健康检查 + 自动拉起 ───────────────────────────────
const SERVICE_PORTS = {
  neo4j:    7687,
  qdrant:   6333,
  embed:    8765,
  reranker: 8877,
};

// 2026-09-03 新增：服务修复命令表，供 memory_os_health 工具返回
const SERVICE_FIX_COMMANDS = {
  neo4j:    "brew services start neo4j",
  qdrant:   "brew services start qdrant",
  embed:    "launchctl kickstart gui/501/com.memoryos.embed-daemon",
  reranker: "launchctl kickstart gui/501/com.memoryos.reranker",
};

// 端口占用检测：返回占用进程的描述字符串，没有则返回空字符串
async function detectPortConflict(port) {
  return new Promise((resolve) => {
    const child = spawn("lsof", ["-i", `:${port}`], { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (d) => (out += d.toString()));
    child.on("close", (code) => {
      // lsof 找到占用时 code=0，输出格式：COMMAND PID USER ...
      if (code === 0 && out.trim()) {
        const lines = out.trim().split("\n");
        const header = lines[0]; // "COMMAND   PID USER   FD   TYPE ..."
        const parts = (lines[1] || "").split(/\s+/);
        const procName = parts[0] || "?";
        const pid      = parts[1] || "?";
        resolve(`${procName} (PID ${pid}) 占用端口 ${port}`);
      } else {
        resolve("");
      }
    });
    child.on("error", () => resolve(""));
  });
}

function checkService(name, port) {
  return new Promise((resolve) => {
    const child = spawn("lsof", ["-i", `:${port}`], { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (d) => (out += d.toString()));
    child.on("error", () => resolve(false));
    child.on("close", (code) => {
      const up = code === 0 && out.includes(String(port));
      console.log(`[memory-os] checkService ${name}: port=${port} code=${code} up=${up}`);
      resolve(up);
    });
  });
}

// 轮询端口就绪，带超时
async function waitForPort(port, timeoutMs = 30000) {
  const interval = 500;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const up = await checkService("_tmp", port);
    if (up) return true;
    await new Promise((r) => setTimeout(r, interval));
  }
  return false;
}

async function ensureServicesRunning() {
  const up = {};
  for (const [name, port] of Object.entries(SERVICE_PORTS)) {
    up[name] = await checkService(name, port);
  }
  console.log(`[memory-os] ensureServicesRunning: neo4j=${up.neo4j} qdrant=${up.qdrant} embed=${up.embed} reranker=${up.reranker}`);

  for (const [name, port] of Object.entries(SERVICE_PORTS)) {
    if (up[name]) continue; // 已在线，跳过

    console.log(`[memory-os] 尝试拉起 ${name} (端口 ${port})...`);

    // 启动命令各服务不同
    // 2026-09-02 修复：原来是 fire-and-forget，launchd 拉不起进程时没法发现。
    // 改成：kickstart + 等端口就绪；30s 超时则自己 spawn python fallback。
    let cmd, args, fallback;
    if (name === "neo4j") {
      cmd  = "brew";
      args = ["services", "start", "neo4j"];
    } else if (name === "qdrant") {
      cmd  = "brew";
      args = ["services", "start", "qdrant"];
    } else if (name === "embed") {
      cmd  = "launchctl";
      args = ["kickstart", `gui/501/com.memoryos.embed-daemon`];
      // fallback: 直接 spawn python 跑 daemon（launchd 拉不起时的兜底）
      fallback = {
        cmd: process.env.HOME + "/.openclaw/workspace/venv/bin/python3",
        args: [
          process.env.HOME + "/.openclaw/workspace/memory-os-plugin/scripts/embed_daemon.py",
          "--host", "127.0.0.1", "--port", "8765",
          "--model", (process.env.HOME + "/.openclaw/workspace/memory-os/models/bge-m3-mlx-8bit"),
          "--idle-timeout", "180",
        ],
      };
    } else if (name === "reranker") {
      cmd  = "launchctl";
      args = ["kickstart", `gui/501/com.memoryos.reranker`];
      fallback = {
        cmd: process.env.HOME + "/.openclaw/workspace/venv/bin/python3",
        args: [
          process.env.HOME + "/.openclaw/workspace/memory-os-plugin/scripts/reranker_daemon.py",
          "--host", "127.0.0.1", "--port", "8877",
          "--model", (process.env.HOME + "/.openclaw/workspace/memory-os/models/Qwen3-Reranker-0.6B-4bit"),
          "--unload-after", "180",
        ],
      };
    }

    // 1. 先尝试 launchctl kickstart（如果有 fallback 配置）
    let kickstartOk = false;
    if (cmd) {
      try {
        const code = await new Promise((resolve) => {
          const child = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
          let err = "";
          child.stderr.on("data", (d) => (err += d.toString()));
          child.on("close", (code) => {
            console.log(`[memory-os] ${name} ${cmd}: code=${code} err=${err.slice(0, 200)}`);
            resolve(code);
          });
          child.on("error", (e) => {
            console.log(`[memory-os] ${name} spawn error: ${e}`);
            resolve(-1);
          });
        });
        // kickstart exit 0 或服务本来就没在跑都算 OK
        kickstartOk = (code === 0);
      } catch (e) {
        console.log(`[memory-os] ${name} kickstart exception: ${e}`);
      }
    }

    // 2. 等端口就绪（最多 30s）
    const ready = await waitForPort(port, 30000);
    if (ready) {
      console.log(`[memory-os] ${name} (端口 ${port}) 已就绪`);
      logEvent("service_started", { service: name, port, method: "kickstart", success: true });
      continue;
    }

    // 3. 端口还没起来：kickstart 没用 / launchd 没拉起进程 -> 走 fallback
    console.log(`[memory-os] ${name} kickstart 后端口 ${port} 仍未就绪，尝试 fallback 直接 spawn python`);
    if (fallback) {
      try {
        const fbChild = spawn(fallback.cmd, fallback.args, {
          stdio: ["ignore", "pipe", "pipe"],
          detached: true,  // 让进程脱离父进程，自己活下去
        });
        fbChild.unref();
        let fbErr = "";
        fbChild.stderr.on("data", (d) => (fbErr += d.toString()));
        fbChild.on("error", (e) => console.log(`[memory-os] ${name} fallback spawn error: ${e}`));
        fbChild.on("close", (code) => {
          console.log(`[memory-os] ${name} fallback exited code=${code} err=${fbErr.slice(0, 200)}`);
        });
      } catch (e) {
        console.log(`[memory-os] ${name} fallback spawn exception: ${e}`);
      }

      // 等 fallback 拉起端口
      const fbPortReady = await waitForPort(port, 30000);
      if (fbPortReady) {
        console.log(`[memory-os] ${name} fallback 拉起成功 (端口 ${port})`);
        logEvent("service_started", { service: name, port, method: "fallback_spawn", success: true });
      } else {
        console.log(`[memory-os] ${name} fallback 30s 内端口仍未就绪，请检查模型路径/手动启动`);
        logEvent("service_start_failed", { service: name, port, method: "fallback_spawn" });
      }
    } else {
      // 没有 fallback（比如 neo4j/qdrant 走 brew services）只能记录
      console.log(`[memory-os] ${name} 30s 内端口仍未就绪，无 fallback 可用，请手动: brew services start ${name}`);
      logEvent("service_start_failed", { service: name, port, method: "kickstart" });
    }
  }
}

// ── 自检函数（插件启动时调用）──────────────────────────────
async function selfCheck() {
  const results = [];
  const LEVEL = { OK: "✅", WARN: "⚠️", FAIL: "❌", INFO: "ℹ️" };

  function add(status, label, detail = "") {
    results.push({ status, label, detail });
  }

  // 1. Python 环境
  add(LEVEL.INFO, "Python 环境");
  try {
    const child = spawn(PYTHON_BIN, ["-c", "import sys; print(sys.version.split()[0])"], {
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 8000,
    });
    const ver = await new Promise((res) => {
      child.stdout.on("data", (d) => res(d.toString().trim()));
      child.on("error", () => res(""));
      setTimeout(() => res(""), 8000);
    });
    if (ver) add(LEVEL.OK, "  Python 版本", ver);
    else add(LEVEL.FAIL, "  Python 可执行文件", `找不到或无法运行: ${PYTHON_BIN}`);
  } catch (e) {
    add(LEVEL.FAIL, "  Python 可执行文件", e.message);
  }

  // 2. 关键 Python 包
  const requiredPkgs = ["neo4j", "qdrant_client", "jieba"];
  for (const pkg of requiredPkgs) {
    try {
      const child = spawn(PYTHON_BIN, ["-c", `import ${pkg}; print(${pkg}.__version__ if hasattr(${pkg}, '__version__') else 'ok')`], {
        stdio: ["ignore", "pipe", "pipe"],
        timeout: 8000,
      });
      const out = await new Promise((res) => {
        child.stdout.on("data", (d) => res(d.toString().trim()));
        child.on("error", () => res(""));
        setTimeout(() => res(""), 8000);
      });
      add(out ? LEVEL.OK : LEVEL.FAIL, `  包: ${pkg}`, out || "未找到");
    } catch (e) {
      add(LEVEL.FAIL, `  包: ${pkg}`, e.message.split("\n")[0]);
    }
  }

  // 3. 关键脚本文件
  const scripts = [
    path.resolve(__dirname, "../scripts/write_4layer.py"),
    path.resolve(__dirname, "../scripts/recall_4layer.py"),
    path.resolve(__dirname, "../scripts/process_dream.py"),
  ];
  for (const s of scripts) {
    const exists = fs.existsSync(s);
    add(exists ? LEVEL.OK : LEVEL.FAIL, `  脚本: ${path.basename(s)}`, exists ? "存在" : "不存在");
  }

  // 4. Embedding 模型文件
  const modelPath = process.env.MEMORY_OS_EMBEDDING_MODEL || (process.env.HOME + "/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf");
  const modelExists = fs.existsSync(modelPath);
  add(modelExists ? LEVEL.OK : LEVEL.FAIL, "  Embedding 模型", modelExists ? path.basename(modelPath) : `不存在: ${modelPath}`);

  // 5. Token 目录（可写）
  const tokenDir = Path.home() / ".openclaw" / "workspace" / "memory-os" / "tokens";
  try {
    fs.mkdirSync(tokenDir, { recursive: true });
    const testFile = path.join(tokenDir, ".write-test");
    fs.writeFileSync(testFile, "test");
    fs.unlinkSync(testFile);
    add(LEVEL.OK, "  Token 目录", `可写: ${tokenDir}`);
  } catch (e) {
    add(LEVEL.FAIL, "  Token 目录", `无法写入: ${e.message}`);
  }

  // 6. Neo4j 服务
  const neo4jUp = await checkService("neo4j", 7687);
  if (neo4jUp) {
    add(LEVEL.OK, "  Neo4j 服务", "端口 7687 在线");
  } else {
    add(LEVEL.FAIL, "  Neo4j 服务", "端口 7687 未监听，请运行: brew services start neo4j");
  }

  // 7. Qdrant 服务
  const qdrantUp = await checkService("qdrant", 6333);
  if (qdrantUp) {
    add(LEVEL.OK, "  Qdrant 服务", "端口 6333 在线");
  } else {
    add(LEVEL.FAIL, "  Qdrant 服务", "端口 6333 未监听，请运行: brew services start qdrant");
  }

  // 8. Embed Daemon
  const embedUp = await checkService("embed", 8765);
  if (embedUp) {
    add(LEVEL.OK, "  Embed Daemon", "端口 8765 在线");
  } else {
    add(LEVEL.FAIL, "  Embed Daemon", "端口 8765 未监听，请运行: launchctl kickstart gui/501/com.memoryos.embed-daemon");
  }

  // 9. Reranker Daemon
  const rerankerUp = await checkService("reranker", 8877);
  if (rerankerUp) {
    add(LEVEL.OK, "  Reranker Daemon", "端口 8877 在线");
  } else {
    add(LEVEL.FAIL, "  Reranker Daemon", "端口 8877 未监听，请运行: launchctl kickstart gui/501/com.memoryos.reranker");
  }

  // 10. Neo4j 连通性（bolt 协议层面）
  if (neo4jUp) {
    try {
      const child = spawn(PYTHON_BIN, [
        "-c",
        "from neo4j import GraphDatabase; "
          + "d=GraphDatabase.driver('bolt://127.0.0.1:7687',auth=('neo4j','openclaw')); "
          + "d.verify_connection(); d.close(); print('ok')",
      ], { stdio: ["ignore", "pipe", "pipe"], timeout: 10000 });
      const out = await new Promise((res) => {
        child.stdout.on("data", (d) => res(d.toString().trim()));
        child.on("error", () => res(""));
        setTimeout(() => res(""), 10000);
      });
      add(out === "ok" ? LEVEL.OK : LEVEL.FAIL, "  Neo4j 连通性", out === "ok" ? "认证成功" : out || "连接失败");
    } catch (e) {
      add(LEVEL.FAIL, "  Neo4j 连通性", e.message.split("\n")[0]);
    }
  }

  // 11. Qdrant REST API 连通性
  if (qdrantUp) {
    try {
      const child = spawn("curl", ["-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:6333/readyz"], {
        stdio: ["ignore", "pipe", "pipe"],
        timeout: 5000,
      });
      const code = await new Promise((res) => {
        child.stdout.on("data", (d) => res(d.toString().trim()));
        child.on("error", () => res(""));
        setTimeout(() => res(""), 5000);
      });
      add(code === "200" ? LEVEL.OK : LEVEL.FAIL, "  Qdrant REST API", code === "200" ? "正常" : `HTTP ${code}`);
    } catch (e) {
      add(LEVEL.FAIL, "  Qdrant REST API", e.message.split("\n")[0]);
    }
  }

  // 汇总打印
  console.log("\n" + "═".repeat(60));
  console.log("🍊 Memory OS 自检报告");
  console.log("═".repeat(60));
  for (const { status, label, detail } of results) {
    const detailStr = detail ? `  ← ${detail}` : "";
    console.log(`  ${status} ${label}${detailStr}`);
  }
  const fails = results.filter((r) => r.status === LEVEL.FAIL);
  const warns = results.filter((r) => r.status === LEVEL.WARN);
  console.log("═".repeat(60));
  if (fails.length > 0) {
    console.log(`  ${LEVEL.FAIL} 共 ${fails.length} 项不合格，请修复后再使用`);
  } else if (warns.length > 0) {
    console.log(`  ${LEVEL.WARN} 共 ${warns.length} 项警告，正常可用`);
  } else {
    console.log(`  ${LEVEL.OK} 所有检查项通过，环境正常`);
  }
  console.log("═".repeat(60) + "\n");

  return {
    ok: fails.length === 0,
    fails: fails.length,
    warns: warns.length,
    results,
  };
}

// 同会话 query 去重缓存：Map<sessionKey, Set<queryHash>>
// "用户"说过："不需要做那么复杂的记忆召回对比，你就做，触发的用户问题就可以了"
// 所以只对用户问题的 md5 做去重，不对比 memory 内容
const sessionRecallCache = new Map();

// TTL=0 表示仅本次进程内有效（重启 gateway 清空）
const SESSION_CACHE_TTL = Number(process.env.MEMORY_OS_SESSION_CACHE_TTL || 0);
const SESSION_CACHE_ENABLED = (process.env.MEMORY_OS_SESSION_CACHE_ENABLED || "true").toLowerCase() !== "false";

function hashQuery(text) {
  return crypto.createHash("md5").update(text.trim()).digest("hex");
}

function isCacheHit(sessionKey, queryHash) {
  if (!SESSION_CACHE_ENABLED) return false;
  const set = sessionRecallCache.get(sessionKey);
  if (!set) return false;
  // TTL=0 不查过期时间，永远在本次进程内有效
  return set.has(queryHash);
}

function rememberQuery(sessionKey, queryHash) {
  if (!SESSION_CACHE_ENABLED) return;
  let set = sessionRecallCache.get(sessionKey);
  if (!set) {
    set = new Set();
    sessionRecallCache.set(sessionKey, set);
  }
  set.add(queryHash);
  // 可选 TTL：定期清理过期会话（0 = 不过期）
  if (SESSION_CACHE_TTL > 0) {
    set._addedAt = Date.now();
  }
}

// 注入块前缀：把召回的记忆包装成"我和用户之间本来就有的共同记忆"，不要让模型觉得是外部注入
// 2026-08-11 反馈：旧的"哪条不相关就跳过"给了模型太多偷懒空间，导致明明相关的记忆（比如"用户带去河边玩" vs 用户说"小时候无忧无虑"）也被丢弃。
// 改成更主动的引导：相关就呼应，完全无关才跳过。
const INJECTION_HEADER = `【以下是你和用户之间的共同记忆】
这是你们之间真实发生过的往事，回答相关话题时自然想起来用。
请根据当前对话语境，结合记忆来回答
严禁编造、不用"根据记忆"等机械化表达
真实性永远高于"真人感"。`;

function logEvent(event_type, fields = {}) {
  if (!HOOK_TRACE_ENABLED) return;
  try {
    const _expandedLogPath = LOG_PATH.replace(/\$\{HOME\}/g, process.env.HOME || "");
    fs.mkdirSync(path.dirname(_expandedLogPath), { recursive: true });
    const ts = new Date();
    // 开发者 2026-08-11 反馈：用本地时间（HH:MM:SS）而不是 UTC，
    // 否则日志里的时间和人眼看到的时间差 8 小时，排查问题很懵。
    const pad = (n) => String(n).padStart(2, "0");
    const tsShort = `${pad(ts.getHours())}:${pad(ts.getMinutes())}:${pad(ts.getSeconds())}`;
    const lines = [`### ${tsShort} ${event_type}`, ""];
    for (const [k, v] of Object.entries(fields)) {
      if (v === null || v === undefined) continue;
      if (typeof v === "object") {
        const pretty = JSON.stringify(v, null, 2);
        lines.push(`- **${k}**:`);
        lines.push("");
        lines.push("```json");
        lines.push(pretty);
        lines.push("```");
      } else {
        const s = String(v);
        if (s.includes("\n") || s.length > 80) {
          lines.push(`- **${k}**:`);
          lines.push("");
          lines.push("```");
          lines.push(s);
          lines.push("```");
        } else {
          lines.push(`- **${k}**: ${s}`);
        }
      }
      lines.push("");
    }
    fs.appendFileSync(LOG_PATH, lines.join("\n") + "\n\n", "utf-8");
    // 按字节滚动：超限就从头砍掉，保留后半段（md 按事件块分割，不会破坏内容）
    // LOG_KEEP_BYTES=0 表示不滚动（想看完整历史就设 0）
    try {
      if (LOG_KEEP_BYTES <= 0) return;
      const stat = fs.statSync(LOG_PATH);
      if (stat.size > LOG_KEEP_BYTES) {
        const raw = fs.readFileSync(LOG_PATH, "utf-8");
        const half = Math.floor(raw.length / 2);
        let cutIdx = raw.indexOf("\n### ", half);
        if (cutIdx < 0) cutIdx = half;
        else cutIdx += 1; // 跳过开头的换行
        const trimmed = raw.slice(cutIdx);
        fs.writeFileSync(LOG_PATH, trimmed, "utf-8");
      }
    } catch {}
  } catch (e) {
    // 日志失败不能影响主流程
  }
}

async function runPython(args, options = {}) {
  // 2026-09-03 修复：彻底去掉 runPython 前的健康检查
  // 原逻辑：每次调 Python 都先 await ensureServicesRunning()，
  //        包含 ping 4 个端口 + 端口挂了等 30s 拉起，整条召回延迟 60s+
  // 新逻辑：runPython 直接 spawn Python 子进程，不做任何端口检查。
  //        端口挂了 → Python 脚本自己报错，错误会进 hook-trace，方便排查。
  //        想查服务状态：用 memory_os_health 工具（按需调用，不阻塞召回）。
  // 保留 options.skipHealth 参数仅为向后兼容（现在啥也不做）
  void options.skipHealth;
  // 支持自定义脚本路径（默认 process_dream.py，可被 options.script 覆盖）
  const script = options.script || PYTHON_SCRIPT;
  const timeoutMs = options.timeoutMs || 0;
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON_BIN, [script, ...args], {
      cwd: options.cwd || path.dirname(script),
      env: { ...process.env, NO_PROXY: '127.0.0.1,localhost,::1', no_proxy: '127.0.0.1,localhost,::1', ...(options.env || {}) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", reject);
    let timer;
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        child.kill();
        reject(new Error(`python timed out after ${timeoutMs}ms`));
      }, timeoutMs);
    }
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      if (code === 0) resolve({ stdout, stderr });
      else {
        const isRealError = /^(Error|Exception|Traceback|Traceback |SyntaxError|KeyError|TypeError|NameError)/m.test(stderr);
        if (isRealError) reject(new Error(`python exit ${code}: ${stderr}`));
        else resolve({ stdout, stderr });
      }
    });
  });
}

function extractUserText(event) {
  if (!event || typeof event !== "object") return "";
  // before_prompt_build 的 event：用户文本在 prompt 字段（字符串）
  if (typeof event.prompt === "string" && event.prompt.trim()) return event.prompt;
  // message_received 的 event：用户文本在 content 字段
  if (typeof event.content === "string" && event.content.trim()) return event.content;
  // 旧版 userMessage 字段
  if (typeof event.userMessage === "string") return event.userMessage;
  // messages 数组：取最后一条 user 消息
  if (Array.isArray(event.messages)) {
    for (let i = event.messages.length - 1; i >= 0; i--) {
      const m = event.messages[i];
      if (m && m.role === "user" && typeof m.content === "string") {
        return m.content;
      }
    }
  }
  return "";
}

function getSessionKey(event, ctx) {
  return event?.sessionKey || ctx?.sessionKey || event?.sessionId || ctx?.sessionId || "unknown";
}

function buildEnv(cfg) {
  if (!cfg) return {};
  return {
    NO_PROXY: '127.0.0.1,localhost,::1',
    no_proxy: '127.0.0.1,localhost,::1',
    MEMORY_OS_NEO4J_URI: cfg.neo4jUri || "",
    MEMORY_OS_NEO4J_USER: cfg.neo4jUser || "",
    MEMORY_OS_NEO4J_PASSWORD: cfg.neo4jPassword || "",
    MEMORY_OS_QDRANT_HOST: cfg.qdrantHost || "",
    MEMORY_OS_QDRANT_PORT: String(cfg.qdrantPort || 6333),
    MEMORY_OS_EMBEDDING_MODEL: cfg.embeddingModel || "",
    MEMORY_OS_DEDUP_THRESHOLD: String(cfg.dedupThreshold || 0.95),
  };
}

function makeMemoryInjectionBlock(memories) {
  if (!memories || memories.length === 0) return "";
  // 不再加 [relation] 这种数据库标签，直接放正文，看起来更像自然回忆
  const lines = memories.map((m) => {
    // 兼容两种格式：字符串（recall_for_hook 返回格式化后的文本）或对象（{summary, text}）
    if (typeof m === "string") return m;
    return m.summary || m.text || "";
  }).filter(Boolean);
  if (lines.length === 0) return "";
  return `\n\n${INJECTION_HEADER}\n${lines.join("\n")}\n`;
}

export default definePluginEntry({
  id: "memory-os",
  name: "Memory OS",
  description: "Neo4j + Qdrant 长期记忆系统插件",
  register(api) {
    // 2026-08-19 调试：插件有没有被加载
    try { fs.appendFileSync("/tmp/hook-debug.log", `register called ${Date.now()}\n`, "utf8"); } catch {}

    // ── 插件启动自检 ───────────────────────────────────────────
    // 2026-09-03 改造：自检不再阻塞 plugin register
    // 原逻辑：selfCheck() 会跑 11 项检查（每项单独 spawn Python 子进程，8-10s timeout），
    //        最坏情况跑 60+ 秒，阻塞插件加载；启动后还会被 ensureServicesRunning 每次召回前再跑一次。
    // 新逻辑：自检改成后台 fire-and-forget，1s 后启动，不阻塞 plugin register。
    //        想看体检结果：用 memory_os_health 工具（按需调用）。
    setTimeout(() => {
      selfCheck().catch((e) => console.error("[memory-os] 自检异常:", e.message));
    }, 1000);

    let config = api.pluginConfig || {};
    try {
      const live = api.runtime?.config?.current?.();
      if (live && live.plugins && live.plugins.entries && live.plugins.entries["memory-os"]) {
        config = live.plugins.entries["memory-os"].config || config;
      }
    } catch {}
    // ── 记忆召回：message_received ─────────────────────────────────
    // QQ 消息通道触发时，从 event.content 取用户文本，调 recall 并注入记忆
    // ⚠️ 已禁用（2026-08-20）：与 before_prompt_build 重复触发，导致日志写两遍
    api.on("message_received", async (event, ctx) => {
      return; // 2026-08-20 20:50 禁用：跟 before_prompt_build 重复触发，改用 before_prompt_build 注入当前轮
      const sessionKey = event?.sessionKey || ctx?.sessionKey || "unknown";
      // message_received 事件文本提取顺序（从多个通道的实际 event 结构归纳）：
      // 1. event.metadata.body（QQ 频道、webchat 等）
      // 2. event.content 字符串（某些通道直接给字符串）
      // 3. event.messages[N].content（webchat 等把消息放数组）
      // 4. event.prompt（某些 before_prompt_build 兼用路径）
      const rawContent = event?.content;
      const rawBody = event?.metadata?.body;
      let userText = typeof rawBody === "string" && rawBody.trim()
                ? rawBody.trim()
                : typeof rawContent === "string" && rawContent.trim()
                ? rawContent.trim()
                : Array.isArray(rawContent) ? rawContent.map(c => c?.text || c?.content || "").join(" ").trim()
                : typeof rawContent === "object" && rawContent !== null ? (rawContent.content || rawContent.text || rawContent.body || "").trim()
                : "";
      // 兜底：从 messages 数组取最后一条 user 消息（webchat 通道）
      if (!userText && Array.isArray(event?.messages)) {
        for (let i = event.messages.length - 1; i >= 0; i--) {
          const m = event.messages[i];
          if (m && m.role === "user" && typeof m.content === "string" && m.content.trim()) {
            userText = m.content.trim();
            break;
          }
        }
      }
      if (!userText) {
        // 调试：content 取不到时写日志（正式写 hook-trace，不用翻 tmp 文件）
        logEvent("userText_empty", {
          session: sessionKey,
          content_type: typeof rawContent,
          content_preview: String(typeof rawContent === 'string' ? rawContent.slice(0, 80) : JSON.stringify(rawContent).slice(0, 80)),
          body_type: typeof rawBody,
          body_preview: String((rawBody || '').slice(0, 80)),
          // 也尝试从 messages 数组取
          messages_hint: Array.isArray(event?.messages) ? `len=${event.messages.length}` : typeof event?.messages,
        });
        return;
      }

      if (!userText || userText.length < 3) return;

      const queryHash = hashQuery(userText);
      if (isCacheHit(sessionKey, queryHash)) return;

      try {
        const recall = await runPython(
          ["recall", "--query", userText, "--top-k", "8", "--hook"],
          { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/recall_4layer.py") }
        );
        let payload;
        try { payload = JSON.parse(recall.stdout); } catch {
          logEvent("recall_parse_failed", { session: sessionKey, query: userText.slice(0,200), stderr: recall.stderr.slice(-300) });
          return;
        }

        if (payload.skipped) {
          logEvent("recall_skipped", { session: sessionKey, query: userText.slice(0,200), reason: payload.reason });
          rememberQuery(sessionKey, queryHash);
          return;
        }

        const memories = payload.memories || [];
        const block = makeMemoryInjectionBlock(memories);
        if (!block) { rememberQuery(sessionKey, queryHash); return; }

        if (memories.length > 0) {
          logEvent("injection_committed", {
            session: sessionKey,
            query: userText.slice(0,200),
            n_memories: memories.length,
            summaries: memories.map((m) => (typeof m === "string" ? m : (m.summary || "")).slice(0, 60)),
            block,
            channels: payload.channels || {},
          });
        }

        rememberQuery(sessionKey, queryHash);

        if (api.runtime?.enqueueNextTurnInjection) {
          await api.runtime.enqueueNextTurnInjection({ sessionKey, text: block, placement: "prepend_context" });
        }
        // else: enqueue 不可用是底层问题，不影响召回结果，不写日志
      } catch (err) {
        logEvent("recall_failed", { session: sessionKey, query: userText.slice(0,200), error: String(err) });
      }
    });

    // ── 记忆召回：before_prompt_build ─────────────────────────────
    // user_request 类型（私聊直接对话）不走 message_received，补这个钩子覆盖
    api.on("before_prompt_build", async (event, ctx) => {
      // 调试：钩子有没有被触发
      try { fs.appendFileSync("/tmp/before-pb-debug.log", `BPB fired at ${new Date().toISOString()} session=${ctx?.sessionKey} prompt_type=${typeof event?.prompt}\n`, "utf8"); } catch {}
      // 动态拿当前会话的 sessionKey：从 ctx.sessionKey 直接拿
      const sessionKey = ctx?.sessionKey;
      const chatId = ctx?.chatId;
      if (!sessionKey || !chatId) {
        logEvent("before_prompt_build_no_ctx", {
          sessionKey, chatId,
          ctxKeys: ctx ? Object.keys(ctx) : [],
        });
        return { prependContext: "" };
      }

      // before_prompt_build：用户文本可能在 event.prompt（字符串）或 event.messages 数组
      const rawPrompt = event?.prompt;
      let userText = typeof rawPrompt === "string" && rawPrompt.trim() ? rawPrompt.trim() : "";
      if (!userText && Array.isArray(event?.messages)) {
        for (let i = event.messages.length - 1; i >= 0; i--) {
          const m = event.messages[i];
          if (m && m.role === "user" && typeof m.content === "string" && m.content.trim()) {
            userText = m.content.trim();
            break;
          }
        }
      }

      if (!userText) {
        logEvent("before_prompt_build_empty", {
          session: sessionKey,
          prompt_type: typeof rawPrompt,
          prompt_val: String(rawPrompt || "").slice(0, 50),
          messages_len: event?.messages ? event.messages.length : 0,
          messages_roles: Array.isArray(event?.messages) ? event.messages.map(m => m?.role) : [],
        });
        return { prependContext: "" };
      }
      if (userText.length < 3) return { prependContext: "" };

      const queryHash = hashQuery(userText);
      if (isCacheHit(sessionKey, queryHash)) return { prependContext: "" };

      try {
        const recall = await runPython(
          ["recall", "--query", userText, "--top-k", "8", "--hook"],
          { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/recall_4layer.py") }
        );
        let payload;
        try { payload = JSON.parse(recall.stdout); } catch {
          logEvent("recall_parse_failed", { session: sessionKey, query: userText.slice(0,200), stderr: recall.stderr.slice(-300) });
          return { prependContext: "" };
        }

        if (payload.skipped) {
          logEvent("recall_skipped", { session: sessionKey, query: userText.slice(0,200), reason: payload.reason });
          rememberQuery(sessionKey, queryHash);
          return { prependContext: "" };
        }

        const memories = payload.memories || [];
        const block = makeMemoryInjectionBlock(memories);
        if (!block) { rememberQuery(sessionKey, queryHash); return { prependContext: "" }; }

        if (memories.length > 0) {
          logEvent("injection_committed", {
            session: sessionKey,
            query: userText.slice(0,200),
            n_memories: memories.length,
            channels: payload.channels || {},
          });
        }

        rememberQuery(sessionKey, queryHash);

        return { prependContext: block };
      } catch (err) {
        logEvent("recall_failed", { session: sessionKey, query: userText.slice(0,200), error: String(err) });
        return { prependContext: "" };
      }
    });

    // ── 观察钩子：llm_input（看模型实际收到的输入里有没有记忆块）────
    // 2026-08-26 调试：只写日志不改行为，验证 prependContext 是否真的进了模型输入
    api.on("llm_input", async (event, ctx) => {
      try {
        const sysText = typeof event?.systemPrompt === "string" ? event.systemPrompt
          : Array.isArray(event?.systemPrompt) ? event.systemPrompt.map(s => typeof s === "string" ? s : (s?.text || "")).join("\n")
          : JSON.stringify(event?.systemPrompt || "");
        const promptText = typeof event?.prompt === "string" ? event.prompt
          : Array.isArray(event?.prompt) ? event.prompt.map(p => typeof p === "string" ? p : (p?.text || p?.content || "")).join("\n")
          : JSON.stringify(event?.prompt || "");
        const historyLen = Array.isArray(event?.messages) ? event.messages.length : (Array.isArray(event?.history) ? event.history.length : 0);
        const combined = (sysText || "") + "\n" + (promptText || "");
        const hasMemoryHeader = combined.includes("共同记忆");
        const hasMacau = combined.includes("澳门");
        logEvent("llm_input_observed", {
          session: ctx?.sessionKey || "?",
          hasMemoryHeader,
          hasMacau,
          sys_len: (sysText || "").length,
          history_len: historyLen,
        });
      } catch (e) {}
    });

    // ── 工具：memory_os_ingest（存记忆 - 4 层架构）─────────────
    // 设计：模型（agent）按 extract_prompt.md 4 层抽取规范抽出 JSON 传入本工具。
    // 优先传 `memory`（4 层完整结构 {l0, l1, l2, l3}）。
    // 老格式 `kos` 数组仍接受（自动当 L1 处理，向后兼容）。
    // 插件只负责写库（调 write_4layer.py），不碰 LLM。
    api.registerTool({
      name: "memory_os_ingest",
      description: "把一段对话存入 Memory OS 长期记忆（Neo4j 知识图谱 + Qdrant 向量库，4 层架构 L0/L1/L2/L3）。抽取规范见 ${HOME}/.openclaw/workspace/memory-os-plugin/scripts/extract_prompt.md。**推荐用 `memory_json` 参数**传 4 层 JSON 字符串（避免 MCP 嵌套数组被展平）。也可以用 `memory` 对象参数（不推荐）。兼容老格式：传 `kos: [...]` L1 数组。",
      parameters: {
        type: "object",
        properties: {
          memory: {
            type: "object",
            description: "4 层完整抽取结构 {l0: {scene_summary, source}, l1: {kos: [...]}, l2: {scenario: {...} | null}, l3: {persona: [...] | []}}",
          },
          memory_json: {
            type: "string",
            description: "【推荐】4 层结构的 JSON 字符串，避免 MCP 嵌套数组被展平。例：'{\"l0\":{...},\"l1\":{...},\"l2\":{...},\"l3\":{...}}'",
          },
          kos: {
            type: "array",
            description: "【兼容】老格式：纯 L1 KO 数组。自动转成 l1.kos。",
            items: { type: "object" },
          },
          source: {
            type: "string",
            description: "【兼容】老格式的来源说明",
          },
        },
        required: [],
      },
      async execute(_id, params) {
        // 构造 4 层 payload
        let payload;
        // 优先用 memory_json（字符串）避免 MCP 嵌套数组被展平
        if (params.memory_json && typeof params.memory_json === "string") {
          try {
            payload = JSON.parse(params.memory_json);
          } catch (e) {
            return { content: [{ type: "text", text: JSON.stringify({ error: "memory_json 解析失败: " + e.message }) }] };
          }
        } else if (params.memory && typeof params.memory === "object") {
          // 新格式：直接用
          payload = params.memory;
        } else if (Array.isArray(params.kos) && params.kos.length > 0) {
          // 老格式：kos 数组自动当 L1，L0 从 memory 对象里取（如果有的话）
          const l0_from_memory = (params.memory && params.memory.l0) || {};
          payload = {
            l0: {
              scene_summary: l0_from_memory.scene_summary || params.source || "",
              source: l0_from_memory.source || params.source || "",
            },
            l1: { kos: params.kos },
          };
        } else {
          return { content: [{ type: "text", text: JSON.stringify({ error: "memory_json / memory / kos 至少传一个" }) }] };
        }
        const tmpFile = `/tmp/memory-os-tool-4layer-${Date.now()}.json`;
        try {
          fs.writeFileSync(tmpFile, JSON.stringify(payload), "utf-8");
          // 调 write_4layer.py 写 4 层
          const res = await runPython(["ingest", "--file", tmpFile], { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/write_4layer.py") });
          const last = res.stdout.trim().split(/\n/).filter(Boolean).pop() || "{}";
          let report;
          try { report = JSON.parse(last); } catch { report = { raw: res.stdout.slice(-500) }; }
          return { content: [{ type: "text", text: JSON.stringify(report) }] };
        } finally {
          try { fs.unlinkSync(tmpFile); } catch {}
        }
      },
    });

    // ── 工具：memory_os_recall（查记忆 - 4 层融合）─────────────
    // 4 层默认全开：L3 persona（最高优先）→ L2 scenario → L1 atom → L0 raw（BM25 全文）
    // 默认返回分层结构（persona/scenario/atom/raw），LLM 可分别注入不同位置。
    // 想只查某层时用 layers 参数手控，默认全开。
    api.registerTool({
      name: "memory_os_recall",
      description: "从 Memory OS 长期记忆查询（4 层融合召回 L0/L1/L2/L3）。返回分层结构：persona(L3 画像) / scenario(L2 场景) / atom(L1 事实) / raw(L0 原文)。默认全开 4 层。可用 layers 指定召回顺序（如 \"L3,L2\"），用 include_persona / include_scenario 控制开关。",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "查询文本" },
          top_k: { type: "integer", description: "每层返回条数，默认 5", default: 5 },
          include_persona: { type: "boolean", description: "是否召回 L3 画像（默认 true）", default: true },
          include_scenario: { type: "boolean", description: "是否召回 L2 场景（默认 true）", default: true },
          layers: { type: "string", description: "手控召回顺序，逗号分隔，如 \"L3,L2,L1,L0\"。不传则默认全开" },
        },
        required: ["query"],
      },
      async execute(_id, params) {
        const topK = Number(params.top_k || 5);
        const includePersona = params.include_persona !== false;
        const includeScenario = params.include_scenario !== false;
        // 构造 layers 参数
        let layers;
        if (params.layers && typeof params.layers === "string") {
          layers = params.layers.split(",").map(s => s.trim()).filter(Boolean);
        } else {
          // 默认全开，按优先级
          layers = ["L3", "L2", "L1", "L0"];
          if (!includePersona) layers = layers.filter(l => l !== "L3");
          if (!includeScenario) layers = layers.filter(l => l !== "L2");
        }
        const layersArg = layers.join(",");
        const res = await runPython([
          "recall", "--query", String(params.query), "--top-k", String(topK),
        ], { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/recall_4layer.py") });
        let payload;
        try { payload = JSON.parse(res.stdout.trim()); } catch { payload = { raw: res.stdout.slice(-500) }; }
        return { content: [{ type: "text", text: JSON.stringify(payload) }] };
      },
    });

    // ── 工具：memory_os_delete（4 层两阶段删除）────────────────────
    // 第一阶段：传 query，返回候选清单 + delete_token（5 分钟过期）
    // 第二阶段：传 query + confirm=true + token + selected_pids（可选），真删
    api.registerTool({
      name: "memory_os_delete",
      description: "从 Memory OS 4 层记忆（L0/L1/L2/L3）删除。两阶段：\n- confirm=false（默认）：召回候选 + 返回 token\n- confirm=true：带 token 真删\n快捷模式：传 target_pid + target_collection + target_layer + confirm=true，直接删除（跳过召回）\n支持按 layer 限定召回，支持 selected_pids 限定删哪些候选。",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "召回目标记忆的 query（快捷模式下可选）" },
          top_k: { type: "integer", description: "召回候选数，默认 5", default: 5 },
          layer: { type: "string", enum: ["L0","L1","L2","L3"], description: "限定只召回某一层" },
          confirm: { type: "boolean", description: "是否确认执行删除（第二阶段传 true）", default: false },
          token: { type: "string", description: "第一阶段返回的 token（第二阶段必传，快捷模式不需要）" },
          selected_pids: { type: "array", description: "限定只删哪些候选 pid（第二阶段可选）",
                           items: { type: "string" } },
          target_pid: { type: "string", description: "直接指定要删除的 PID（快捷模式）" },
          target_collection: { type: "string", description: "target_pid 所在的 collection（快捷模式）" },
          target_layer: { type: "string", enum: ["L0","L1","L2","L3"], description: "target_pid 的层（快捷模式）" },
        },
        required: [],
      },
      async execute(_id, params) {
        const topK = Number(params.top_k || 5);
        const layer = params.layer || null;
        const confirm = params.confirm === true;

        // 第二阶段：真删
        if (confirm) {
          const args = ["confirm", "--token", String(params.token || "")];
          // 快捷模式：直接传 pid + collection + layer，跳过 token
          const hasDirect = params.target_pid && params.target_collection && params.target_layer;
          if (hasDirect) {
            args.splice(0, args.length, "delete",
              "--direct-pid", String(params.target_pid),
              "--direct-collection", String(params.target_collection),
              "--direct-layer", String(params.target_layer),
              "--query", String(params.query || ""));
          } else if (!params.token) {
            return { content: [{ type: "text", text: JSON.stringify({ error: "confirm=true 时必须传 token" }) }] };
          }
          if (Array.isArray(params.selected_pids) && params.selected_pids.length > 0 && !hasDirect) {
            args.push("--selected-pids", params.selected_pids.join(","));
          }
          const res = await runPython(
            args,
            { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/write_4layer.py") }
          );
          const last = res.stdout.trim().split(/\n/).filter(Boolean).pop() || "{}";
          let payload;
          try { payload = JSON.parse(last); } catch { payload = { raw: res.stdout.slice(-500) }; }
          return { content: [{ type: "text", text: JSON.stringify(payload) }] };
        }

        // 第一阶段：召回 + 生成 token
        const args = ["delete", "--query", String(params.query), "--top-k", String(topK)];
        if (layer) args.push("--layer", layer);
        const res = await runPython(
          args,
          { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/write_4layer.py") }
        );
        const last = res.stdout.trim().split(/\n/).filter(Boolean).pop() || "{}";
        let payload;
        try { payload = JSON.parse(last); } catch { payload = { raw: res.stdout.slice(-500) }; }
        return { content: [{ type: "text", text: JSON.stringify(payload) }] };
      },
    });

    // ── 工具：memory_os_update（4 层两阶段更新）────────────────────
    // 第一阶段：传 query + 新内容（4 层结构），召回候选 + 生成 token
    // 第二阶段：传 query + confirm=true + token，真更新（覆盖）
    api.registerTool({
      name: "memory_os_update",
      description: "更新 Memory OS 4 层记忆。两阶段：\n- confirm=false（默认）：召回候选 + 返回 token\n- confirm=true：带 token 真更新\n新内容用 4 层结构（l0/l1/l2/l3），工具自动按目标层分发写入。",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "召回目标记忆的 query" },
          memory: {
            type: "object",
            description: "新内容（4 层结构 {l0, l1, l2, l3}）",
            properties: {
              l0: { type: "object" },
              l1: { type: "object" },
              l2: { type: "object" },
              l3: { type: "object" },
            },
          },
          kos: {
            type: "array",
            description: "【兼容】老格式：纯 L1 KO 数组，自动转 memory.l1.kos",
            items: { type: "object" },
          },
          top_k: { type: "integer", description: "召回候选数，默认 5", default: 5 },
          confirm: { type: "boolean", description: "是否确认执行更新（第二阶段传 true）", default: false },
          token: { type: "string", description: "第一阶段返回的 token（第二阶段必传）" },
          target_pid: { type: "string", description: "直接指定要更新的 PID（跳过召回，直接更新）" },
          target_collection: { type: "string", description: "target_pid 所在的 collection 名" },
          target_layer: { type: "string", enum: ["L0","L1","L2","L3"], description: "target_pid 的层" },
        },
        required: ["query"],
      },
      async execute(_id, params) {
        const topK = Number(params.top_k || 5);
        const confirm = params.confirm === true;

        // 第二阶段：真更新
        if (confirm) {
          // 构造新内容（必传）
          let payload4;
          if (params.memory && typeof params.memory === "object") {
            payload4 = params.memory;
          } else if (Array.isArray(params.kos) && params.kos.length > 0) {
            payload4 = { l1: { kos: params.kos } };
          } else {
            return { content: [{ type: "text", text: JSON.stringify({ error: "confirm 阶段 memory 或 kos 至少传一个" }) }] };
          }
          const tmpFile = `/tmp/memory-os-tool-confirm-${Date.now()}.json`;
          fs.writeFileSync(tmpFile, JSON.stringify(payload4), "utf-8");

          // 快捷模式：直接传 target_pid + collection + layer，跳过 token 召回
          let res;
          if (params.target_pid && params.target_collection && params.target_layer) {
            res = await runPython(
              ["update", "--target-pid", String(params.target_pid),
               "--target-collection", String(params.target_collection),
               "--target-layer", String(params.target_layer),
               "--file", tmpFile],
              { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/write_4layer.py") }
            );
          } else if (params.token) {
            res = await runPython(
              ["confirm", "--token", String(params.token), "--file", tmpFile],
              { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/write_4layer.py") }
            );
          } else {
            try { fs.unlinkSync(tmpFile); } catch {}
            return { content: [{ type: "text", text: JSON.stringify({ error: "confirm=true 时必须传 token 或 target_pid+target_collection+target_layer" }) }] };
          }
          try { fs.unlinkSync(tmpFile); } catch {}
          const last = res.stdout.trim().split(/\n/).filter(Boolean).pop() || "{}";
          let payload;
          try { payload = JSON.parse(last); } catch { payload = { raw: res.stdout.slice(-500) }; }
          return { content: [{ type: "text", text: JSON.stringify(payload) }] };
        }

        // 构造 4 层 payload
        let payload4;
        if (params.memory && typeof params.memory === "object") {
          payload4 = params.memory;
        } else if (Array.isArray(params.kos) && params.kos.length > 0) {
          payload4 = { l1: { kos: params.kos } };
        } else {
          return { content: [{ type: "text", text: JSON.stringify({ error: "memory 或 kos 至少传一个" }) }] };
        }

        const tmpFile = `/tmp/memory-os-tool-update-${Date.now()}.json`;
        try {
          fs.writeFileSync(tmpFile, JSON.stringify(payload4), "utf-8");
          const res = await runPython(
            ["update", "--query", String(params.query), "--file", tmpFile, "--top-k", String(topK)],
            { env: buildEnv(config), script: path.resolve(__dirname, "../scripts/write_4layer.py") }
          );
          const last = res.stdout.trim().split(/\n/).filter(Boolean).pop() || "{}";
          let payload;
          try { payload = JSON.parse(last); } catch { payload = { raw: res.stdout.slice(-500) }; }
          return { content: [{ type: "text", text: JSON.stringify(payload) }] };
        } finally {
          try { fs.unlinkSync(tmpFile); } catch {}
        }
      },
    });

    // ── 工具：memory_os_health（服务体检）─────────────
    // 2026-09-03 新增：把原来插件启动时的 11 项自检 + runPython 前的 ensureServicesRunning
    //   都搬到这里做成按需调用的工具，不在插件启动/召回路径上自动跑。
    // 调用方：LLM（agent）/ 用户主动调。
    // 默认只查 4 个服务端口的在线状态（快速 < 2s），可选传 deep=true 跑 11 项完整自检。
    api.registerTool({
      name: "memory_os_health",
      description: "检查 Memory OS 依赖服务的健康状态（4 个端口 + 可选 11 项深度自检）。\n\n【默认模式】只检查服务端口：Neo4j 7687 / Qdrant 6333 / Embed 8765 / Reranker 8877，耗时 < 2s。返回每个服务是否在线 + 修复命令。\n\n【深度模式】传 deep=true 跑完整 11 项自检：Python 环境 / 关键包 / 脚本文件 / 模型 / Token 目录 / 4 个服务端口 / Neo4j 认证 / Qdrant API。耗时 5-30s。\n\n【什么时候调】\n- 召回明显变慢 / 报错 / 结果不准\n- 启动时看到服务异常提示\n- 任何时候想确认服务状态\n\n【为什么需要这个工具】\n之前这些检查都跑在 plugin 启动 + 每次召回前，最坏延迟 60s+。现在改成按需调，不阻塞召回。",
      parameters: {
        type: "object",
        properties: {
          deep: {
            type: "boolean",
            description: "是否跑完整 11 项自检（默认 false，只查端口）",
            default: false,
          },
        },
      },
      async execute(_id, params) {
        const deep = !!params.deep;

        // 快速模式：只查 4 个端口
        const portChecks = [];
        for (const [name, port] of Object.entries(SERVICE_PORTS)) {
          const t0 = Date.now();
          const up = await checkService(name, port).catch(() => false);
          portChecks.push({
            name,
            port,
            up,
            latency_ms: Date.now() - t0,
            fix_command: SERVICE_FIX_COMMANDS[name] || "(无修复命令)",
          });
        }

        const result = {
          mode: deep ? "deep" : "fast",
          timestamp: new Date().toISOString(),
          ports: portChecks,
          all_up: portChecks.every((c) => c.up),
        };

        if (deep) {
          // 深度模式：调 selfCheck() 拿 11 项结果
          try {
            const sc = await selfCheck();
            result.deep = {
              ok: sc.ok,
              fails: sc.fails,
              warns: sc.warns,
              checks: sc.results.map((r) => ({
                status: r.status,
                label: r.label,
                detail: r.detail,
              })),
            };
          } catch (e) {
            result.deep_error = String(e);
          }
        }

        // 写一行到 hook-trace，方便排查
        try {
          logEvent("memory_os_health_check", {
            mode: deep ? "deep" : "fast",
            all_up: result.all_up,
            down: portChecks.filter((c) => !c.up).map((c) => c.name),
          });
        } catch {}

        return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
      },
    });

  },
});
