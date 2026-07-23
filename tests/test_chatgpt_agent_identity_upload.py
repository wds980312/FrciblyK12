from __future__ import annotations

import json

from platforms.chatgpt.cpa_upload import upload_agent_identity_to_cpa
from platforms.chatgpt.sub2api_upload import upload_agent_identity_to_sub2api
from platforms.chatgpt.sub2api_upload import probe_sub2api_account_model
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_upload_agent_identity_to_cpa_posts_export_json(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 201
        text = ""

        def json(self):
            return {"ok": True}

    def fake_post(url, *, headers, data, proxies, verify, timeout, impersonate):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "payload": json.loads(data.decode("utf-8")),
                "proxies": proxies,
                "verify": verify,
                "timeout": timeout,
                "impersonate": impersonate,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("platforms.chatgpt.cpa_upload.cffi_requests.post", fake_post)

    ok, message = upload_agent_identity_to_cpa(
        {
            "type": "sub2api-data",
            "auth_mode": "agentIdentity",
            "accounts": [{"name": "identity@test.com"}],
            "agent_identity": {"account_id": "acct-identity"},
        },
        filename="identity@test.com_agent_identity_sub2api.json",
        api_url="http://sub2.local",
        api_key="secret-token",
    )

    assert ok is True
    assert message == "上传成功"
    assert captured["url"] == (
        "http://sub2.local/v0/management/auth-files"
        "?name=identity%40test.com_agent_identity_sub2api.json"
    )
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["payload"]["auth_mode"] == "agentIdentity"
    assert captured["payload"]["agent_identity"]["account_id"] == "acct-identity"
    assert captured["proxies"] is None
    assert captured["verify"] is False


def test_chatgpt_platform_exposes_agent_identity_upload_action():
    actions = ChatGPTPlatform().get_platform_actions()

    action = next(item for item in actions if item["id"] == "upload_agent_identity_cpa")

    assert action["label"] == "导入 Agent Identity 到 Sub2API"
    assert [param["key"] for param in action["params"]] == ["api_url", "api_key"]


def test_upload_agent_identity_to_sub2api_imports_codex_session(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "imported": 1}

    def fake_post(url, *, headers, data, proxies, verify, timeout, impersonate):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "payload": json.loads(data.decode("utf-8")),
                "proxies": proxies,
                "verify": verify,
                "timeout": timeout,
                "impersonate": impersonate,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.post", fake_post)

    ok, message = upload_agent_identity_to_sub2api(
        {
            "type": "sub2api-data",
            "auth_mode": "agentIdentity",
            "accounts": [{"name": "identity@test.com"}],
            "agent_identity": {"account_id": "acct-identity"},
        },
        api_url="http://sub2.local",
        auth_token="secret-token",
    )

    assert ok is True
    assert message == "Sub2API 数据导入成功"
    assert captured["url"] == (
        "http://sub2.local/api/v1/admin/accounts/data"
    )
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["payload"]["data"]["auth_mode"] == "agentIdentity"
    assert captured["payload"]["data"]["agent_identity"]["account_id"] == "acct-identity"
    assert captured["payload"]["skip_default_group_bind"] is False
    assert captured["proxies"] is None
    assert captured["verify"] is False
    assert captured["timeout"] == 120


def test_sub2api_account_model_test_sends_fixed_model_and_prompt(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = 'data: {"type":"response.output_text.delta","delta":"hi"}\n\ndata: [DONE]\n'

        def json(self):
            return {"code": 0, "data": {"items": [{"id": 237, "name": "identity@test.com"}]}}

    def fake_get(url, *, headers, params, proxies, verify, timeout, impersonate):
        assert url == "http://sub2.local/api/v1/admin/accounts"
        return FakeResponse()

    def fake_post(url, *, headers, data, proxies, verify, timeout, impersonate):
        captured.update(
            {
                "url": url,
                "payload": json.loads(data.decode("utf-8")),
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.get", fake_get)
    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.post", fake_post)

    outcome = probe_sub2api_account_model(
        "identity@test.com",
        api_url="http://sub2.local",
        auth_token="secret-token",
    )

    assert outcome == {
        "status": "success",
        "message": "gpt-5.5 返回了模型响应",
        "sub2api_account_id": 237,
    }
    assert captured["url"] == "http://sub2.local/api/v1/admin/accounts/237/test"
    assert captured["payload"] == {"model_id": "gpt-5.5", "prompt": "hi", "mode": "compact"}
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["timeout"] == 90


def test_sub2api_account_model_test_keeps_network_failure_distinct(monkeypatch):
    def fake_post(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(
        "platforms.chatgpt.sub2api_upload.find_sub2api_accounts_by_emails",
        lambda *args, **kwargs: {"identity@test.com": [{"id": 237}]},
    )
    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.post", fake_post)

    outcome = probe_sub2api_account_model(
        "identity@test.com",
        api_url="http://sub2.local",
        auth_token="secret-token",
    )

    assert outcome["status"] == "error"
    assert "connection reset" in outcome["message"]


def test_sub2api_account_model_test_exposes_upstream_sse_error(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = (
            'data: {"error":{"message":"Agent runtime has been deleted.",'
            '"code":"biscuit_baker_service_agent_error_status"},"status":403}\n\n'
            "data: [DONE]\n"
        )

    monkeypatch.setattr(
        "platforms.chatgpt.sub2api_upload.find_sub2api_accounts_by_emails",
        lambda *args, **kwargs: {"identity@test.com": [{"id": 237}]},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.sub2api_upload.cffi_requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    outcome = probe_sub2api_account_model(
        "identity@test.com",
        api_url="http://sub2.local",
        auth_token="secret-token",
    )

    assert outcome == {
        "status": "failed",
        "message": "Agent runtime has been deleted.",
        "sub2api_account_id": 237,
    }


def test_upload_agent_identity_to_sub2api_binds_configured_group(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = json.dumps(self._payload)

        def json(self):
            return self._payload

    def fake_post(url, *, headers, data, proxies, verify, timeout, impersonate):
        calls.append(("post", url, json.loads(data.decode("utf-8"))))
        return FakeResponse(200, {"code": 0, "message": "success"})

    def fake_get(url, *, headers, params, proxies, verify, timeout, impersonate):
        calls.append(("get", url, dict(params or {})))
        if url.endswith("/admin/groups"):
            return FakeResponse(
                200,
                {"code": 0, "data": {"items": [{"id": 2, "name": "GPT"}]}},
            )
        if url.endswith("/admin/accounts"):
            return FakeResponse(
                200,
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "id": 237,
                                "name": "identity@test.com",
                                "group_ids": [1],
                            }
                        ]
                    },
                },
            )
        raise AssertionError(url)

    def fake_put(url, *, headers, json, proxies, verify, timeout, impersonate):
        calls.append(("put", url, json))
        return FakeResponse(200, {"code": 0, "message": "success"})

    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.post", fake_post)
    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.get", fake_get)
    monkeypatch.setattr("platforms.chatgpt.sub2api_upload.cffi_requests.put", fake_put)

    ok, message = upload_agent_identity_to_sub2api(
        {
            "type": "sub2api-data",
            "auth_mode": "agentIdentity",
            "accounts": [{"name": "identity@test.com"}],
            "agent_identity": {"account_id": "acct-identity"},
        },
        api_url="http://sub2.local",
        auth_token="secret-token",
        default_group="GPT",
    )

    assert ok is True
    assert message == "Sub2API 数据导入成功，已绑定分组 GPT"
    assert ("put", "http://sub2.local/api/v1/admin/accounts/237", {"group_ids": [1, 2]}) in calls


def test_auto_upload_registered_agent_identity_uses_export_artifact(monkeypatch):
    from application.tasks import _upload_registered_agent_identity

    captured = {}

    class FakeArtifact:
        filename = "auto@test.com_agent_identity_sub2api.json"
        content = json.dumps(
            {
                "type": "sub2api-data",
                "auth_mode": "agentIdentity",
                "agent_identity": {"account_id": "acct-auto"},
            }
        )

    class FakeExportsService:
        def export_chatgpt_agent_identity_sub2api(self, selection):
            captured["selection"] = selection
            return FakeArtifact()

    def fake_upload(export_data, *, api_url=None, auth_token=None):
        captured["upload"] = {
            "export_data": export_data,
            "api_url": api_url,
            "auth_token": auth_token,
        }
        return True, "Sub2API 导入成功"

    monkeypatch.setattr(
        "application.tasks.AccountExportsService",
        lambda: FakeExportsService(),
    )
    monkeypatch.setattr(
        "application.tasks.upload_agent_identity_to_sub2api",
        fake_upload,
    )

    result = _upload_registered_agent_identity(123)

    assert result == {
        "account_id": 123,
        "ok": True,
        "message": "Sub2API 导入成功",
        "filename": "auto@test.com_agent_identity_sub2api.json",
        "target": "sub2api",
    }
    assert captured["selection"].ids == [123]
    assert captured["selection"].select_all is False
    assert captured["upload"]["export_data"]["auth_mode"] == "agentIdentity"


def test_agent_identity_platform_action_uses_independent_queue(monkeypatch):
    from application.tasks import create_platform_action_task

    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"task_id": "task-test"}

    monkeypatch.setattr("application.tasks.create_task", fake_create_task)

    result = create_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "upload_agent_identity_cpa",
            "params": {},
        }
    )

    assert result == {"task_id": "task-test"}
    assert captured["task_type"] == "platform_action"
    assert captured["platform"] == "sub2api_import"
    assert captured["payload"]["platform"] == "chatgpt"
