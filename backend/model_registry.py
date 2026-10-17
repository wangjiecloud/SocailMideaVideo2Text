"""
litellm 模型注册表。

把「用哪个模型、用什么凭据、连哪个网关」三件事收敛到一个模块：
- ms.json：模型列表（对象格式，~/.config/opencode/ms.json），与 lunwenAgent
  运行时读取的是同一份 —— lunwenAgent 更新模型时本项目自动跟随；
- m.json：兜底模型列表（数组格式，项目根维护，与 lunwenAgent 仓库根同格式），
  ms.json 不可用或无有效条目时使用；
- llm-config.json：litellm 网关的 Bearer 凭据与用户名。
  优先读 ~/.config/opencode/llm-config.json（lunwenAgent 全局位置，最新鲜），
  fallback 到项目根目录。

只放纯函数（读文件、无副作用），Web/CLI/MCP 三个入口共用，
避免「默认模型」「凭据优先级」这类逻辑在各入口漂移。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# litellm 网关的默认地址（模型条目缺失网关地址时兜底）
DEFAULT_LITELLM_BASE_URL = "https://apiprod.midea.com/llm/f-devops-python-litellm/v1"

# llm-config.json 候选位置（按优先级）：lunwenAgent 全局位置 → 项目根
_LLM_AUTH_CANDIDATES = [
    lambda root: Path(os.path.expanduser("~/.config/opencode/llm-config.json")),
    lambda root: root / "llm-config.json",
]

# ms.json 候选位置：lunwenAgent 运行时真正读取的模型列表（对象格式）
_MS_MODELS_CANDIDATES = [
    lambda root: Path(os.path.expanduser("~/.config/opencode/ms.json")),
]

# m.json 固定放项目根（与 lunwenAgent/m.json 同格式，拷贝维护，作为兜底）
_MODELS_FILE = "m.json"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_llm_auth() -> tuple[str, str]:
    """读取 litellm 网关凭据。

    Returns:
        (authorization, user)。authorization 含 "Bearer " 前缀；
        任一候选文件都不可用时返回 ("", "")。
    """
    root = project_root()
    for make_path in _LLM_AUTH_CANDIDATES:
        path = make_path(root)
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            authorization = (data.get("authorization") or "").strip()
            if not authorization:
                continue
            user = (data.get("user") or "").strip()
            return authorization, user
        except Exception as e:
            logger.warning(f"读取 {path} 失败: {e}")
    return "", ""


def _entries_from_ms_json(path: Path) -> list[dict]:
    """解析 ms.json（对象格式）。

    结构：models 为 {model_id: {id, name, api: {url}, status, ...}}，
    与 lunwenAgent 运行时消费的是同一份文件。status 显式非 active 的
    条目跳过；api.url 缺失时用默认网关。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = []
    for key, item in (data.get("models") or {}).items():
        if not isinstance(item, dict):
            continue
        status = (item.get("status") or "active").strip().lower()
        if status != "active":
            continue
        model = (item.get("id") or key).strip()
        if not model:
            continue
        base_url = ((item.get("api") or {}).get("url") or "").strip().rstrip("/")
        entries.append(
            {
                "name": (item.get("name") or model).strip(),
                "model": model,
                "base_url": base_url or DEFAULT_LITELLM_BASE_URL,
            }
        )
    return entries


def _entries_from_m_json(path: Path) -> list[dict]:
    """解析 m.json（数组格式）。

    过滤规则：roles 含 "chat" 且 provider 不是 vllm（vllm 条目是
    lunwenAgent 的 autocomplete 专用，没有网关语义）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = []
    for item in data.get("models", []):
        roles = item.get("roles") or []
        provider = (item.get("provider") or "").lower()
        if "chat" not in roles or provider == "vllm":
            continue
        base_url = (item.get("onPremProxyUrl") or "").strip().rstrip("/")
        entries.append(
            {
                "name": (item.get("name") or item.get("model") or "").strip(),
                "model": (item.get("model") or "").strip(),
                "base_url": base_url or DEFAULT_LITELLM_BASE_URL,
            }
        )
    return entries


def load_model_entries() -> list[dict]:
    """读取模型列表，返回可用的 chat 模型条目。

    优先 ~/.config/opencode/ms.json（lunwenAgent 运行时同源，对象格式），
    不可用或无有效条目时回退项目根 m.json（数组格式）。

    每个条目: {"name": 显示名, "model": 模型 ID, "base_url": 网关地址}。
    两处都不可用时返回空列表。
    """
    for make_path in _MS_MODELS_CANDIDATES:
        path = make_path(None)
        try:
            if not path.is_file():
                continue
            entries = _entries_from_ms_json(path)
            if entries:
                return entries
            logger.warning(f"{path} 无可用条目，回退 {_MODELS_FILE}")
        except Exception as e:
            logger.warning(f"读取 {path} 失败，回退 {_MODELS_FILE}: {e}")

    path = project_root() / _MODELS_FILE
    try:
        return _entries_from_m_json(path)
    except FileNotFoundError:
        logger.warning(f"未找到 {_MODELS_FILE}（{path}），模型列表为空")
        return []
    except Exception as e:
        logger.error(f"解析 {_MODELS_FILE} 失败: {e}")
        return []


def resolve_model_entry(model_ref: Optional[str]) -> Optional[dict]:
    """按模型 ID 或显示名在模型列表中查找条目；找不到返回 None。"""
    if not model_ref:
        return None
    ref = model_ref.strip()
    if not ref:
        return None
    for entry in load_model_entries():
        if entry["model"] == ref or entry["name"] == ref:
            return entry
    return None


def default_model_id() -> Optional[str]:
    """默认模型：m.json 里第一个 chat 模型（列表维护方把它排在首选位置）。"""
    entries = load_model_entries()
    return entries[0]["model"] if entries else None


def resolve_llm_settings(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> dict:
    """解析 LLM 连接参数，返回供 Summarizer/Translator 用的关键字参数。

    优先级：
    1. 显式传入的 api_key + base_url（手动模式/环境变量），model 原样透传；
    2. litellm 模式：model 在模型列表中（或未指定 model）时，凭据取
        llm-config.json，网关地址取模型条目的网关地址；
        model 未指定时用默认模型。
    3. 都不可用时返回空 dict（Summarizer 内部再回退 OPENAI_API_KEY 环境变量）。
    """
    explicit_key = (api_key or "").strip()
    explicit_base = (base_url or "").strip().rstrip("/")

    if explicit_key:
        return {
            "api_key": explicit_key,
            "base_url": explicit_base or None,
            "model": (model or "").strip() or None,
        }

    # litellm 模式：有凭据才生效
    authorization, _user = load_llm_auth()
    if not authorization:
        return {}

    entry = resolve_model_entry(model)
    if entry is not None:
        resolved_model = entry["model"]
        resolved_base = entry["base_url"]
    elif model:
        # 调用方指定了模型但不在模型列表：仍用 litellm 网关直连该模型 ID
        resolved_model = model.strip()
        resolved_base = DEFAULT_LITELLM_BASE_URL
    else:
        resolved_model = default_model_id()
        resolved_base = DEFAULT_LITELLM_BASE_URL

    if not resolved_model:
        return {}

    # openai SDK 会自动加 "Bearer " 前缀，这里存裸 token
    bare_key = authorization.removeprefix("Bearer ").strip()
    return {
        "api_key": bare_key,
        "base_url": resolved_base,
        "model": resolved_model,
    }
