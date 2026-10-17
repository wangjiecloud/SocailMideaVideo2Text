"""model_registry：模型列表（ms.json/m.json）+ llm-config.json 凭据解析的单元测试。

全部用 tmp_path 隔离，不读写真实配置文件；monkeypatch project_root 与
os.path.expanduser 指向临时目录（expanduser 必须一并 mock，否则会读到
真实的 ~/.config/opencode/ms.json）。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import model_registry as mr  # noqa: E402


def _expanduser_to(home: Path):
    """把 ~ 展开重定向到临时 home，隔离真实 ~/.config/opencode/。"""
    return mock.patch(
        "os.path.expanduser",
        side_effect=lambda x: str(home) + x[1:] if x.startswith("~") else x,
    )


def _write_models_file(root: Path, models: list) -> None:
    (root / "m.json").write_text(
        json.dumps({"models": models}, ensure_ascii=False), encoding="utf-8"
    )


def _write_ms_json(home: Path, data: dict) -> None:
    """往临时 home 的 ~/.config/opencode/ 写 ms.json（lunwenAgent 运行时位置）。"""
    d = home / ".config" / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ms.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


SAMPLE_MODELS = [
    {
        "name": "Claude-4.6-Sonnet",
        "provider": "continue-proxy",
        "model": "claude-sonnet-4.6",
        "onPremProxyUrl": "https://gw.example.com/litellm/v1",
        "roles": ["chat", "edit"],
    },
    {
        "name": "GLM-5",
        "provider": "continue-proxy",
        "model": "glm-5",
        "onPremProxyUrl": "https://gw.example.com/litellm/v1",
        "roles": ["chat", "edit"],
    },
    # vllm autocomplete 条目应被过滤
    {
        "name": "Qwen-2.5-Coder-7B",
        "provider": "vllm",
        "model": "Qwen/Qwen-2.5-Coder-7B",
        "apiBase": "https://gw.example.com/fastapply/v1",
        "roles": ["autocomplete"],
    },
]

SAMPLE_MS_MODELS = {
    "models": {
        "claude-sonnet-5": {
            "id": "claude-sonnet-5",
            "providerID": "workspace",
            "name": "Claude-5-Sonnet",
            "api": {
                "id": "claude-sonnet-5",
                "url": "https://gw.example.com/litellm/v1",
            },
            "status": "active",
        },
        "deepseek_v4": {
            "id": "deepseek_v4",
            "providerID": "workspace",
            "name": "DeepSeek-v4",
            "api": {
                "id": "deepseek_v4",
                "url": "https://gw.example.com/litellm/v1",
            },
            # 无 status 字段 → 视为 active
        },
        "old-model": {
            "id": "old-model",
            "providerID": "workspace",
            "name": "Old-Model",
            "api": {
                "id": "old-model",
                "url": "https://gw.example.com/litellm/v1",
            },
            "status": "deprecated",  # 非 active → 跳过
        },
    }
}


class ModelRegistryTest(unittest.TestCase):
    def _with_root(self, models=None, auth=None):
        """构造临时项目根，返回 root。"""
        root = Path(tempfile.mkdtemp())
        if models is not None:
            _write_models_file(root, models)
        if auth is not None:
            (root / "llm-config.json").write_text(json.dumps(auth), encoding="utf-8")
        return root

    def test_load_model_entries_filters_vllm_and_autocomplete(self):
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            entries = mr.load_model_entries()
        self.assertEqual([e["model"] for e in entries], ["claude-sonnet-4.6", "glm-5"])
        self.assertEqual(entries[0]["base_url"], "https://gw.example.com/litellm/v1")

    def test_load_model_entries_missing_file(self):
        root = self._with_root(models=None)
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            self.assertEqual(mr.load_model_entries(), [])

    def test_ms_json_takes_priority_over_m_json(self):
        """ms.json 存在时优先于项目根 m.json（与 lunwenAgent 运行时同源）。"""
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        _write_ms_json(home, SAMPLE_MS_MODELS)
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            entries = mr.load_model_entries()
            default = mr.default_model_id()
        self.assertEqual(
            [e["model"] for e in entries], ["claude-sonnet-5", "deepseek_v4"]
        )
        self.assertEqual(entries[0]["name"], "Claude-5-Sonnet")
        self.assertEqual(entries[0]["base_url"], "https://gw.example.com/litellm/v1")
        self.assertEqual(default, "claude-sonnet-5")

    def test_ms_json_missing_falls_back_to_m_json(self):
        """全局无 ms.json 时回退项目根 m.json。"""
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            entries = mr.load_model_entries()
        self.assertEqual([e["model"] for e in entries], ["claude-sonnet-4.6", "glm-5"])

    def test_ms_json_broken_falls_back_to_m_json(self):
        """ms.json 损坏时回退 m.json，而不是抛异常。"""
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        d = home / ".config" / "opencode"
        d.mkdir(parents=True)
        (d / "ms.json").write_text("{not valid json", encoding="utf-8")
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            entries = mr.load_model_entries()
        self.assertEqual([e["model"] for e in entries], ["claude-sonnet-4.6", "glm-5"])

    def test_ms_json_entry_without_api_url_uses_default_gateway(self):
        """ms.json 条目缺 api.url 时用默认网关地址。"""
        data = {
            "models": {
                "m1": {"id": "m1", "providerID": "workspace", "name": "M1"},
            }
        }
        root = self._with_root(models=None)
        home = Path(tempfile.mkdtemp())
        _write_ms_json(home, data)
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            entries = mr.load_model_entries()
        self.assertEqual(entries[0]["base_url"], mr.DEFAULT_LITELLM_BASE_URL)

    def test_resolve_model_entry_from_ms_json(self):
        """按 ID / 显示名都能解析到 ms.json 条目；deprecated 条目不可见。"""
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        _write_ms_json(home, SAMPLE_MS_MODELS)
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            by_id = mr.resolve_model_entry("deepseek_v4")
            by_name = mr.resolve_model_entry("Claude-5-Sonnet")
            old = mr.resolve_model_entry("old-model")
            m_json_only = mr.resolve_model_entry("glm-5")  # 仅存在于兜底 m.json
        self.assertEqual(by_id["model"], "deepseek_v4")
        self.assertEqual(by_name["model"], "claude-sonnet-5")
        self.assertIsNone(old)
        self.assertIsNone(m_json_only)

    def test_load_llm_auth_prefers_global_location(self):
        root = self._with_root()
        home = Path(tempfile.mkdtemp())
        (home / ".config" / "opencode").mkdir(parents=True)
        (home / ".config" / "opencode" / "llm-config.json").write_text(
            json.dumps({"authorization": "Bearer global-token", "user": "u1"}),
            encoding="utf-8",
        )
        (root / "llm-config.json").write_text(
            json.dumps({"authorization": "Bearer local-token", "user": "u2"}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            auth, user = mr.load_llm_auth()
        self.assertEqual(auth, "Bearer global-token")
        self.assertEqual(user, "u1")

    def test_load_llm_auth_fallback_to_project_root(self):
        home = Path(tempfile.mkdtemp())  # 全局位置无文件
        root = self._with_root(
            auth={"authorization": "Bearer local-token", "user": "u2"}
        )
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            auth, user = mr.load_llm_auth()
        self.assertEqual(auth, "Bearer local-token")
        self.assertEqual(user, "u2")

    def test_resolve_model_entry_by_id_and_name(self):
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            by_id = mr.resolve_model_entry("glm-5")
            by_name = mr.resolve_model_entry("Claude-4.6-Sonnet")
            missing = mr.resolve_model_entry("nonexistent")
        self.assertEqual(by_id["model"], "glm-5")
        self.assertEqual(by_name["model"], "claude-sonnet-4.6")
        self.assertIsNone(missing)

    def test_resolve_llm_settings_litellm_mode(self):
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        (home / ".config" / "opencode").mkdir(parents=True)
        (home / ".config" / "opencode" / "llm-config.json").write_text(
            json.dumps({"authorization": "Bearer tok-123", "user": "u"}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            # 显式 model：按模型列表条目解析网关
            s1 = mr.resolve_llm_settings(model="glm-5")
            # 未指定 model：用默认（第一个 chat 模型）
            s2 = mr.resolve_llm_settings()
            # 显式 api_key 优先级最高
            s3 = mr.resolve_llm_settings(
                api_key="sk-manual", base_url="https://x/v1", model="m"
            )
            # model 不在模型列表：仍走 litellm 网关直连该模型 ID
            s5 = mr.resolve_llm_settings(model="custom-model-x")
        self.assertEqual(
            s1,
            {
                "api_key": "tok-123",
                "base_url": "https://gw.example.com/litellm/v1",
                "model": "glm-5",
            },
        )
        self.assertEqual(s2["model"], "claude-sonnet-4.6")
        self.assertEqual(
            s3, {"api_key": "sk-manual", "base_url": "https://x/v1", "model": "m"}
        )
        self.assertEqual(
            s5,
            {
                "api_key": "tok-123",
                "base_url": mr.DEFAULT_LITELLM_BASE_URL,
                "model": "custom-model-x",
            },
        )

    def test_resolve_llm_settings_no_credentials_returns_empty(self):
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())  # 无任何凭据文件
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            self.assertEqual(mr.resolve_llm_settings(), {})

    def test_default_model_id(self):
        root = self._with_root(models=SAMPLE_MODELS)
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(mr, "project_root", return_value=root),
            _expanduser_to(home),
        ):
            self.assertEqual(mr.default_model_id(), "claude-sonnet-4.6")


if __name__ == "__main__":
    unittest.main()
