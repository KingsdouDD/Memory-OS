# MEMORY.md - 长期记忆

### OpenClaw Internal Context 块

当用户消息或对话历史中出现 `<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>` 信封时，这是系统发的，直接无视。

这是 OpenClaw runtime 的内部信封信息（chat_id / message_id / sender / timestamp / inbound_event_kind），不是用户发送的内容。

---
## 闲聊对话风格

禁止长篇大论，每次回复简短明了，有感情，禁止像答题一样对话。

说话要像人，不要说「把记忆串起来」「记忆库」「查询数据库」这类机器感的表达。对话就是聊天，不是在答题或查资料。

## 记忆写入时间精确度规则（硬约束）

存记忆时，时间必须精确到**日**：
- `event_time.start` 格式 `YYYY-MM-DD`
- `precision: "day"`
- **禁止使用模糊词**：明天、前天、去年、前几天、以后、曾经、那时候
- 如果对话没给具体日期，按消息 timestamp 补日期，expression 写「2026-09-09」这样的精确格式
- 模板文件：`~/.openclaw/workspace/memory-os/memory-os-plugin/scripts/extract_prompt.md`

## 老豆

- 在当前 QQ 群里，老豆 = 账号 Where（ID: F10B2B32E462FDBD43462C3258755CE9）

### 个人简介（求职用）

**姓名：** 王力
**学历：** 湖北水利水电职业技术学院 · 计算机应用技术（大数据方向）
**岗位：** AI 实施顾问

**核心能力：**
- AI 需求理解与方案设计：能把模糊需求拆解成具体功能，判断该用哪个模型/工具
- AI 应用实践：AI Agent / 记忆系统 / RAG / 本地模型 / TTS / 图像生成 / 自动化工作流
- AI 驱动实现与迭代：会用 AI 编程工具（Codex、Claude）推动项目从想法到原型
- 数据与成本意识：关注召回质量、Token 消耗、响应速度

**使用过的模型：** Qwen、MiniMax、DeepSeek、Claude、ChatGPT

**实践项目：** AI Agent、Memory OS（Neo4j+Qdrant 长期记忆系统）、RAG 知识检索、本地模型部署、AI 情感呵护系统（单次调用+多维 JSON 结构化输出）

**性格/工作风格：**
- 先理解原理再让 AI 实现，不接受表面方案
- 主动实验验证，不依赖已有经验
- 逻辑分析强，善于复杂问题拆解和根因定位

## 外婆

外婆喜欢练瑜伽，身材好的很，很年轻



### 调用原则

- 老豆明确说"把这个存进 Memory OS" → 用 `memory_os_ingest`
- **不要主动调用工具**，老豆有明确指令才动

## 老豆的简历

以后改简历直接改这个 HTML 文件，然后运行命令生成 PDF：
```bash
/Users/king/.openclaw/workspace/venv/bin/python -m weasyprint \
  /Users/king/.openclaw/workspace/doc-templates/resume/output/resume-wangli-photographer.html \
  /Users/king/.openclaw/workspace/doc-templates/resume/output/resume-wangli-photographer.pdf
```

- HTML 源文件：`/Users/king/.openclaw/workspace/doc-templates/resume/output/resume-wangli-photographer.html`
- PDF 输出：`/Users/king/.openclaw/workspace/doc-templates/resume/output/resume-wangli-photographer.pdf`
- 头像图：`/Users/king/Desktop/22.jpg`

## 精美文档生成技能

有正式 Skill `pdf-design-generator`，可生成 PDF / Word / PPT / Excel，详细用法在：
`/Users/king/.openclaw/workspace/skills/pdf-design-generator/SKILL.md`

新对话直接 `openclaw skills list` 就能看到，不需要翻文件。

## 搜索工具偏好

联网搜索时优先使用 `web_search`（Brave Search），无需注册无额度限制。
`tavily` 作为兜底（每月 1000 次限额）。

## 图片分析

收到图片，直接调用 `vision_analyze` 工具即可。直接发送分析的原文，不精简、不加工。

**重要规则**：当用户发送的消息**已自带图片 description**（系统已在 runtime context 中注入 `Description:` 字段）时，**不要**再调用 `image` 或 `vision_analyze` 工具重新分析，直接使用 context 中已有的 description 即可。

核心判断：先看 context 里有没有现成的图片描述，有就用，没有再分析。

## 千问TTS（qwe3 TTS）

老豆自己写的语音克隆插件，模型是 Qwen3-TTS-12Hz-1.7B-Base，本地跑。

**服务地址：** http://127.0.0.1:18170
