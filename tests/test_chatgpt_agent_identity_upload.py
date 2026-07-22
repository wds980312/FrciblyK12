from __future__ import annotations

import json

from platforms.chatgpt.cpa_upload import upload_agent_identity_to_cpa
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

    assert action["label"] == "上传 Agent Identity"
    assert [param["key"] for param in action["params"]] == ["api_url", "api_key"]


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

    def fake_upload(export_data, *, filename, api_url=None, api_key=None):
        captured["upload"] = {
            "export_data": export_data,
            "filename": filename,
            "api_url": api_url,
            "api_key": api_key,
        }
        return True, "上传成功"

    monkeypatch.setattr(
        "application.tasks.AccountExportsService",
        lambda: FakeExportsService(),
    )
    monkeypatch.setattr(
        "application.tasks.upload_agent_identity_to_cpa",
        fake_upload,
    )

    result = _upload_registered_agent_identity(123)

    assert result == {
        "account_id": 123,
        "ok": True,
        "message": "上传成功",
        "filename": "auto@test.com_agent_identity_sub2api.json",
    }
    assert captured["selection"].ids == [123]
    assert captured["selection"].select_all is False
    assert captured["upload"]["export_data"]["auth_mode"] == "agentIdentity"
