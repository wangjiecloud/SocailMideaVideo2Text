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
- **抖音博主合集链**（`backend/douyin.py` + `backend/channel_processor.py`）：列表获取 = SSR 快路径 → Playwright 浏览器兜底（2026 风控下唯一能拿全量列表的方式，页面内 fetch 由 SDK 自动签名）；单视频 = `cla_info` 字幕直提 → 最低码率直链 → `m.douyin.com/share/video/<id>` 分享页兜底 → yt-dlp 最后兜底。图文（`images` 或 `aweme_type==150`）跳过不算失败。`DOUYIN_COOKIE`（.env）与 `DOUYIN_MAX_VIDEOS`（限量测试）都从环境读取，三个代码入口都加载 .env（python-dotenv，不覆盖已导出变量）。
- **博主合集的暂停/续转**：逐视频进度落盘在 `temp/channel_progress_<sha1(sec_uid)[:20]>.json`（`backend/progress_store.py`，按博主独立、与 tasks.json 解耦）；合集完成或删除任务时清理，批量清理 temp/ 时不要删。Web 暂停 = `POST /api/task/{id}/pause`（不打断进行中的视频），重跑同一博主自动复用已完成小节；重启遗留的 channel 任务标记为 `paused` 而非 error（前端「未完成的博主任务」面板可续转）。`channel_processor.process_creator` 通过 `pause_check`/`on_video_record`/`on_list`/`resume_sections` 四个回调参数保持纯编排，不做进度文件 I/O。
- **依赖红线**：faster-whisper 1.2.1 与 PyAV ≥16 不兼容（`av.open(metadata_errors=...)` 参数被移除，转录直接 TypeError），requirements.txt 钉了 `av>=14,<16`，升级 faster-whisper 前先确认它支持的 PyAV 版本。
- **HF 模型下载**：huggingface.co 直连会超时，需 `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`（Xet CAS 401）；模型缓存后转录离线可用。

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
