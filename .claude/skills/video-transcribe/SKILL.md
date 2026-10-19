---
name: video-transcribe
description: Transcribe and summarize a video or podcast from a URL (YouTube, TikTok, Bilibili, Apple Podcasts, SoundCloud, 30+ platforms) or from a local media/.txt file. Also transcribes a Douyin creator page (douyin.com/user/...) into one timestamped collection of all their videos. Use when the user shares a video/podcast link or media file and wants a transcript, summary, translation, or the original video downloaded. Triggers on "transcribe", "summarize this video", "what does this video say", "get the transcript", "转录", "视频摘要".
---

# Video Transcribe

Runs this repo's pipeline headlessly: platform subtitles when available (seconds),
local Whisper as fallback, then optimize → translate → summarize.

## Before running

The command needs the project's virtualenv and `ffmpeg`. Verify once per session:

```bash
ls venv/bin/python && command -v ffmpeg
```

- **No venv** → `./install.sh` (or `python3 -m venv venv && venv/bin/pip install -r requirements.txt`)
- **No ffmpeg** → `brew install ffmpeg` (macOS) / `sudo apt install ffmpeg` (Debian/Ubuntu). Do not proceed without it; audio extraction will fail.
- Douyin creator pages additionally need `curl_cffi` (in requirements.txt) — plain HTTP clients get reset by Douyin's TLS fingerprinting.

## Run it

```bash
venv/bin/python transcribe.py "<URL or file path>" --json
```

Always pass `--json` — it puts machine-readable output on stdout and keeps progress
chatter on stderr. Parse the JSON rather than scraping the log lines.

Useful flags:

| Flag | When to use |
|------|-------------|
| `-l, --summary-language <code>` | Summary language: `en`, `zh`, `es`, `fr`, `de`, `it`, `pt`, `ru`, `ja`, `ko`, `ar`. Default `en` |
| `--no-llm` | **No API key available**, or the user only wants the raw transcript. Skips optimize/translate/summarize |
| `--no-video` | User doesn't want the source video kept (faster, less disk) |
| `--whisper-model small` | Accuracy matters more than speed. `tiny`→`large`, default `base` |
| `-o <dir>` | Write the Markdown somewhere other than `./temp` |

The LLM steps default to the litellm gateway: models come from
`~/.config/opencode/ms.json` (the same file lunwenAgent reads at runtime, so it
stays in sync) with the project-root `ms.json` copy as fallback, and credentials
from
`llm-config.json` (repo root, gitignored; the global copy at
`~/.config/opencode/llm-config.json` wins). Alternatively set `OPENAI_API_KEY`
(and optionally `OPENAI_BASE_URL`). Without either the pipeline still transcribes
but falls back to basic formatting — prefer `--no-llm` in that case and summarize
the transcript yourself, which is usually better anyway since you have the full
context.

Provider settings:

```bash
export OPENAI_API_KEY="your_api_key_here"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_TRANSLATION_MODEL="gpt-4o"  # optional
```

For a one-off run, `transcribe.py` also accepts `--api-key`, `--base-url`, and
`--model` (a model ID from the model list such as `claude-sonnet-5`, or any
name). Prefer
environment variables when possible so API keys are not written to shell history.

## Reading the result

```json
{
  "title": "...",
  "no_speech": false,
  "detected_language": "en",
  "files": { "raw": "...", "transcript": "...", "summary": "...", "translation": "..." },
  "media": { "path": "...", "kind": "video", "size_bytes": 2243691 }
}
```

Read the `transcript` / `summary` paths to get the content. `translation` is present
only when the source language differs from the summary language.

## Douyin creator pages

A Douyin creator page URL (`https://www.douyin.com/user/<sec_uid>`, or
`/user/profile?sec_uid=...`) switches the pipeline into batch mode: it parses the
creator's video list, transcribes every video, and writes ONE collection where each
section is headed `## [YYYY-MM-DD HH:MM] <video title>` (the video's publish time).
No per-video optimize/translate — only a single overall summary — because a creator
may have hundreds of videos.

- Set `DOUYIN_COOKIE` (raw Cookie string from a logged-in browser) to raise the
  success rate. Douyin's 2026 risk control hides the video list from plain HTTP
  clients, so full listing needs Playwright (`pip install playwright &&
  playwright install chromium` — the pipeline drives a real browser in the
  background); without it only whatever the server-rendered fast path yields is
  available.
- Per-video chain: native captions (`cla_info`) extracted directly when present;
  otherwise the lowest-bitrate stream is downloaded, burned-in subtitles are
  OCR'd (creator-typed text; needs `rapidocr-onnxruntime`, auto-falls back to
  Whisper when absent or when a video carries no burned subtitles — OCR wins
  on fast-paced speech where Whisper mis-hears homophones), and audio is
  Whisper-transcribed as the final fallback, with an
  `m.douyin.com/share/video/<id>` fallback when the direct link fails.
  Image posts (photo carousels) are skipped, not transcribed.
- Resume / incremental: per-video progress is persisted under the output dir
  and kept after the run completes. Re-running the same creator URL — after an
  interruption (Ctrl+C, restart) or after the collection finished — reuses
  every finished section and only fetches/transcribes videos published since
  (plus retries of failed ones), then regenerates the summary over the full
  collection. Delete the `channel_progress_*.json` file in the output dir
  first for a from-scratch rerun.
- Result JSON gains `"channel": true`, `"video_count"`, `"ok_count"`,
  `"no_speech_count"`, `"image_count"`, `"failed_count"`, `"failed"` (list of
  per-video failure titles); per-video failures are skipped and listed, they do
  not fail the whole run. `DOUYIN_MAX_VIDEOS` env caps the run for testing.
- `"no_speech": true` here means none of the videos contained speech. Same rule:
  do not invent content.

Failure modes specific to Douyin: `www.douyin.com` is unreachable from some
corporate networks (TLS reset), and parsing may hit a risk-control page. Both
surface as a clear error message — suggest `DOUYIN_COOKIE` or a different network.
A single video URL (`douyin.com/video/<id>`) goes through the normal yt-dlp path.

## Critical: `no_speech`

When `"no_speech": true` the source contains **no speech at all**. There is no
transcript, no summary, no translation — the pipeline deliberately skips the LLM so
that nothing gets invented.

If you see this, tell the user the video has no speech. **Do not** guess at the
content, infer it from the title, or describe what the video "probably" says. This
guard exists because feeding an empty transcript to an LLM previously produced a
confident, entirely fabricated conversation.

## Failure modes

- Exit `2` — bad input (missing file, unsupported extension, empty text file). The message says which; fix the argument.
- Exit `1` — download/transcode failure. Usually an unreachable URL, a region-blocked or private video, or missing ffmpeg. Report the actual error; don't retry blindly.
- First run downloads Whisper weights (~150 MB for `base`), so it can look stalled for a minute.
- Long videos are slow in Whisper mode (a 30-min audio-only podcast can take 15–60 min). Warn the user before starting; subtitle mode is seconds.
