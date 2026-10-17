# AI-Video-Transcriber 仓库须知

yt-dlp 字幕优先 → Faster-Whisper 兜底 → OpenAI 兼容 LLM（优化/翻译/摘要）的视频转录工具。一套管线，四个入口（Web / CLI / MCP / Skills）。

## 常用命令

```bash
# 初始化环境（ffmpeg 必须在 PATH 上，否则音频抽取/转码全挂）
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # 或 ./install.sh

# 启动 Web 服务（FastAPI + SSE，默认 :8000）
python3 start.py          # 开发模式：uvicorn 热重载
python3 start.py --prod   # 生产模式：关热重载，长任务（30-60min）SSE 才稳定

# 无头 CLI（agent/脚本入口）
venv/bin/python transcribe.py "<URL 或本地文件>" --json   # stdout 只输出 JSON，进度在 stderr

# 测试（unittest；仓库没有 pytest/lint/typecheck/formatter 配置）
venv/bin/python -m unittest discover tests                     # 全部
venv/bin/python -m unittest tests.test_task_dedup              # 单个模块
venv/bin/python -m unittest discover tests -k test_task_dedup  # 按名过滤

# MCP 服务器冒烟测试（不启 stdio 协议流）
venv/bin/python mcp_server.py --selftest
```

提交信息用 conventional commits（`fix:` / `feat:` / `docs:`）。

## 架构要点

- **四个入口共享一条管线**：`backend/main.py`（FastAPI Web + SSE）、`transcribe.py`（CLI）、`mcp_server.py`（MCP stdio）、两份 SKILL.md（包装 CLI）。改管线行为时四个入口都要考虑。
- **`backend/` 模块一律平铺导入**（`from pipeline import ...`，不是 `backend.pipeline`）：start.py 会 chdir 到 `backend/` 再启动 uvicorn；根目录脚本把 `backend/` 插进 `sys.path`。
- **`backend/pipeline.py` 只放纯函数**（不依赖 FastAPI/全局状态），是三个代码入口共用的唯一实现。「无语音判定」这类关键逻辑必须放这里，不要在各入口重复。
- **`temp/` 是运行时产物**（gitignored）：Markdown 结果、媒体文件、`temp/tasks.json`（任务持久化状态）。批量清理时不要删 `tasks.json`。
- `TRANSCRIBER_TEMP_DIR` 环境变量可重定向 temp 目录（测试隔离用）。
- main.py 启动时把上次遗留的 `processing` 任务标记为中断；停服有 `SHUTDOWN_FORCE_EXIT_SECONDS`（默认 3s）强退看门狗，测试中置 `None` 关闭。
- 任务编排：同参数的运行中任务会被复用（dedup）；模型或摘要语言变化会取消并取代（supersede）正在跑的任务——`tests/test_task_dedup.py` 全覆盖。
- 设了 `OPENAI_API_KEY` 但未设 `OPENAI_BASE_URL` 时，start.py 会默认用 `https://oneapi.basevec.com/v1`（写死在代码里，README 未提）。

## 测试注意

- 不需要网络/API Key：LLM 客户端全部用假对象 mock（见 `tests/test_empty_llm_content.py` 的 `ScriptedClient`）。
- `test_task_dedup.py` 必须在 import `main` **之前**设 `TRANSCRIBER_TEMP_DIR`，否则会读写真实 `temp/tasks.json`。
- FastAPI TestClient 需要 httpx，随 `mcp` 依赖间接装上——装完整 requirements.txt 才能跑全部测试。

## 关键约束（不要破坏）

- **无语音守卫**：`pipeline.transcribed_speech()` 判空时绝不把空文稿喂给 LLM——空输入会让它编造整段对话。CLI/MCP 结果的 `no_speech: true` 必须如实上报，不得臆测内容。
- **stdout 纯净**：`--json` 与 MCP stdio 模式下 stdout 只能有 JSON，日志/进度全走 stderr（mcp_server.py 用 `logging.basicConfig(force=True)` 把 handler 钉在 stderr）。
- **改 CLI 参数需同步四处文档**：`skills/video-transcribe/SKILL.md`、`.claude/skills/video-transcribe/SKILL.md`、`mcp_server.py` 工具 schema、README——描述的是同一套 CLI。
- `static/` 由 main.py 的 no-cache 中间件下发（有测试断言），保证 index.html 与 app.js 版本一致，别去掉。
- 代码注释/docstring 以中文为主，新增代码保持一致。
