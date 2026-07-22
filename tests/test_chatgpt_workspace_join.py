from types import SimpleNamespace

from core.base_platform import RegisterConfig
from core.base_mailbox import _extract_verification_link
from core.base_mailbox import MailboxAccount
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.workspace_join import (
    DEFAULT_ALIAS_FIRST_ONLY_WORKSPACE_IDS,
    DEFAULT_WORKSPACE_IDS,
    export_joined_workspace_cpa_sessions,
    open_workspace_invite_in_browser,
    parse_workspace_ids,
    request_workspace_join_in_browser,
    run_workspace_join_flow,
)


def test_parse_workspace_ids_uses_default_when_blank():
    assert parse_workspace_ids("") == DEFAULT_WORKSPACE_IDS.splitlines()


def test_default_workspace_ids_include_current_full_set_and_keep_first_only_policy():
    assert parse_workspace_ids("") == [
        "ff598c4d-ccaf-40c1-bfaa-cb94565764b1",
        "cf8e512d-1f3b-4603-950c-3d9758a8b435",
        "47336c9d-7607-4478-b37c-018049af1e46",
        "59208eb6-ec43-4d87-9289-dbd9e250bdd6",
        "2c82c020-e1bc-4363-9502-a6794405f793",
        "9901799e-e832-48b1-9278-9abe73168708",
        "c72dcdb4-63a0-40b7-b0bb-ccce3ca54984",
        "2b636e76-a87b-4222-b536-2dc4a545109f",
        "4779b1d7-3109-4ecb-957f-80262f4d7161",
        "ae67aa09-f3d3-4895-977d-9ca44ed1d996",
        "6daa08c1-59c8-4e06-9bc8-9d7246a63057",
        "521ffc8f-9612-4950-84ed-95773138eca6",
    ]
    assert DEFAULT_ALIAS_FIRST_ONLY_WORKSPACE_IDS == "cf8e512d-1f3b-4603-950c-3d9758a8b435"


def test_post_register_workspace_join_uses_session_export_by_default(monkeypatch):
    import platforms.chatgpt.workspace_join as workspace_join

    captured = {}

    def fake_run_workspace_join_flow(_page, _session_info, *, config, **_kwargs):
        captured["config"] = dict(config)
        return {"workspace_join": {"ok": True}}

    monkeypatch.setattr(workspace_join, "run_workspace_join_flow", fake_run_workspace_join_flow)

    ctx = SimpleNamespace(
        extra={"auto_chatgpt_workspace_join": True},
        identity=SimpleNamespace(mailbox_account=None),
        proxy="",
        log=lambda _message: None,
    )
    callback = ChatGPTPlatform(config=RegisterConfig())._build_post_register_in_browser_callback(ctx)

    assert callback is not None
    callback(object(), {"access_token": "registration-access"})

    assert captured["config"]["export_method"] == "session"


def test_parse_workspace_ids_accepts_commas_and_lines():
    assert parse_workspace_ids(" one,\ntwo \n\n three ") == ["one", "two", "three"]


def test_workspace_join_alias_policy_skips_first_only_workspace_for_reused_alias(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    requested = []
    exported = []

    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda _page, **kwargs: requested.extend(kwargs["workspace_ids"])
        or [{"ok": True, "workspace_id": item} for item in kwargs["workspace_ids"]],
    )

    def fake_export(_page, *, workspace_id, output_dir=None, log=None, **_kwargs):
        exported.append(workspace_id)
        return {
            "ok": True,
            "path": str(tmp_path / f"{workspace_id}.json"),
            "workspace_id": workspace_id,
            "email": "member@example.com",
            "account_id": f"{workspace_id}-account",
            "expired": "2026-07-01T00:00:00Z",
            "access_token": f"{workspace_id}-access",
            "refresh_token": "",
            "id_token": f"{workspace_id}-id",
            "session_token": f"{workspace_id}-session",
        }

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fake_export,
        raising=False,
    )

    mailbox_account = MailboxAccount(
        email="root+gpt02@gmail.com",
        account_id="mail-1",
        extra={
            "provider_resource": {
                "metadata": {
                    "alias_index": 2,
                    "base_email": "root@gmail.com",
                }
            }
        },
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=mailbox_account,
        config={
            "workspace_ids": "workspace-allowed\nworkspace-first-only",
            "alias_first_only_workspace_ids": "workspace-first-only",
            "accept_invite": True,
            "export_cpa_json": True,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert requested == ["workspace-allowed"]
    assert exported == ["workspace-allowed"]
    assert result["workspace_join"]["workspace_ids"] == ["workspace-allowed"]
    assert result["workspace_join"]["alias_policy_skipped"] == ["workspace-first-only"]
    assert result["workspace_join"]["ok"] is True


def test_workspace_join_alias_policy_keeps_first_alias_workspace(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    requested = []
    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda _page, **kwargs: requested.extend(kwargs["workspace_ids"])
        or [{"ok": True, "workspace_id": item} for item in kwargs["workspace_ids"]],
    )

    mailbox_account = MailboxAccount(
        email="root+gpt01@gmail.com",
        account_id="mail-1",
        extra={
            "provider_resource": {
                "metadata": {
                    "email": "root+gpt01@gmail.com",
                    "alias_index": 1,
                    "base_email": "root@gmail.com",
                }
            }
        },
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=mailbox_account,
        config={
            "workspace_ids": "workspace-allowed\nworkspace-first-only",
            "alias_first_only_workspace_ids": "workspace-first-only",
            "accept_invite": True,
            "export_cpa_json": False,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert requested == ["workspace-allowed", "workspace-first-only"]
    assert result["workspace_join"]["workspace_ids"] == ["workspace-allowed", "workspace-first-only"]
    assert result["workspace_join"]["alias_policy_skipped"] == []


def test_workspace_join_alias_policy_ignores_unconfirmed_alias_metadata(monkeypatch):
    import platforms.chatgpt.workspace_join as workspace_join

    requested = []
    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda _page, **kwargs: requested.extend(kwargs["workspace_ids"])
        or [{"ok": True, "workspace_id": item} for item in kwargs["workspace_ids"]],
    )

    mailbox_account = MailboxAccount(
        email="different+gpt02@gmail.com",
        account_id="mail-1",
        extra={
            "provider_resource": {
                "metadata": {
                    "email": "different+gpt02@gmail.com",
                    "alias_index": 2,
                    "base_email": "root@gmail.com",
                }
            }
        },
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=mailbox_account,
        config={
            "workspace_ids": "workspace-first-only",
            "alias_first_only_workspace_ids": "workspace-first-only",
            "accept_invite": True,
            "export_cpa_json": False,
        },
    )

    assert requested == ["workspace-first-only"]
    assert result["workspace_join"]["workspace_ids"] == ["workspace-first-only"]
    assert result["workspace_join"]["alias_policy_skipped"] == []


def test_workspace_join_alias_policy_all_skipped_is_nonfatal():
    mailbox_account = MailboxAccount(
        email="root+gpt03@gmail.com",
        account_id="mail-1",
        extra={"provider_resource": {"metadata": {"alias_index": 3, "base_email": "root@gmail.com"}}},
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=mailbox_account,
        config={
            "workspace_ids": "workspace-first-only",
            "alias_first_only_workspace_ids": "workspace-first-only",
            "accept_invite": True,
            "export_cpa_json": True,
        },
    )

    assert result["workspace_join"]["ok"] is True
    assert result["workspace_join"]["workspace_ids"] == []
    assert result["workspace_join"]["alias_policy_skipped"] == ["workspace-first-only"]
    assert result["workspace_join"]["warning"] == "all workspace ids skipped by alias policy"


def test_request_workspace_join_in_browser_sends_batch_with_configured_concurrency(monkeypatch):
    import platforms.chatgpt.workspace_join as workspace_join

    monkeypatch.setattr(workspace_join, "_fetch_access_token_from_page", lambda _page, _log=None: "page-token")

    class FakePage:
        url = "https://chatgpt.com/"

        def __init__(self):
            self.calls = []

        def evaluate(self, _script, payload):
            self.calls.append(payload)
            return [
                {"ok": True, "status": 200, "workspace_id": workspace_id, "text": "", "url": ""}
                for workspace_id in payload["workspaceIds"]
            ]

    page = FakePage()

    result = request_workspace_join_in_browser(
        page,
        access_token="fallback-token",
        workspace_ids=["workspace-1", "workspace-2", "workspace-3"],
        request_concurrency=3,
        retry_backoff_ms=0,
        interval_ms=0,
    )

    assert [item["workspace_id"] for item in result] == ["workspace-1", "workspace-2", "workspace-3"]
    assert all(item["ok"] for item in result)
    assert len(page.calls) == 1
    assert page.calls[0]["workspaceIds"] == ["workspace-1", "workspace-2", "workspace-3"]
    assert page.calls[0]["concurrency"] == 3


def test_request_workspace_join_in_browser_does_not_retry_permanent_4xx(monkeypatch):
    import platforms.chatgpt.workspace_join as workspace_join

    monkeypatch.setattr(workspace_join, "_fetch_access_token_from_page", lambda _page, _log=None: "page-token")
    sleeps = []
    monkeypatch.setattr(workspace_join.time, "sleep", sleeps.append)

    class FakePage:
        url = "https://chatgpt.com/"

        def __init__(self):
            self.calls = []

        def evaluate(self, _script, payload):
            self.calls.append(payload)
            return [
                {
                    "ok": False,
                    "status": 400,
                    "workspace_id": workspace_id,
                    "text": "bad request",
                    "url": "",
                }
                for workspace_id in payload["workspaceIds"]
            ]

    page = FakePage()

    result = request_workspace_join_in_browser(
        page,
        access_token="fallback-token",
        workspace_ids=["workspace-1"],
        max_retries=3,
        retry_backoff_ms=1500,
        interval_ms=0,
    )

    assert result == [
        {"ok": False, "status": 400, "workspace_id": "workspace-1", "text": "bad request", "url": ""}
    ]
    assert len(page.calls) == 1
    assert sleeps == []


def test_request_workspace_join_in_browser_retries_transient_failures_and_caps_concurrency(monkeypatch):
    import platforms.chatgpt.workspace_join as workspace_join

    monkeypatch.setattr(workspace_join, "_fetch_access_token_from_page", lambda _page, _log=None: "page-token")
    sleeps = []
    monkeypatch.setattr(workspace_join.time, "sleep", sleeps.append)

    class FakePage:
        url = "https://chatgpt.com/"

        def __init__(self):
            self.calls = []

        def evaluate(self, _script, payload):
            self.calls.append(payload)
            status = 500 if len(self.calls) == 1 else 200
            return [
                {
                    "ok": status == 200,
                    "status": status,
                    "workspace_id": workspace_id,
                    "text": "" if status == 200 else "server error",
                    "url": "",
                }
                for workspace_id in payload["workspaceIds"]
            ]

    page = FakePage()

    result = request_workspace_join_in_browser(
        page,
        access_token="fallback-token",
        workspace_ids=["workspace-1", "workspace-2"],
        request_concurrency=99,
        max_retries=1,
        retry_backoff_ms=1500,
        interval_ms=0,
    )

    assert all(item["ok"] for item in result)
    assert len(page.calls) == 2
    assert page.calls[0]["concurrency"] == 8
    assert page.calls[1]["workspaceIds"] == ["workspace-1", "workspace-2"]
    assert sleeps == [1.5]


def test_workspace_join_flow_uses_fast_request_defaults(monkeypatch):
    import platforms.chatgpt.workspace_join as workspace_join

    captured = {}

    def fake_request(_page, **kwargs):
        captured.update(kwargs)
        return [{"ok": True, "workspace_id": item} for item in kwargs["workspace_ids"]]

    monkeypatch.setattr(workspace_join, "request_workspace_join_in_browser", fake_request)

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=None,
        config={
            "workspace_ids": "workspace-1\nworkspace-2",
            "accept_invite": True,
            "export_cpa_json": False,
        },
    )

    assert result["workspace_join"]["ok"] is True
    assert captured["request_concurrency"] == 4
    assert captured["retry_backoff_ms"] == 1500


def test_extract_verification_link_accepts_chatgpt_workspace_invite():
    html = """
    <a href="https://chatgpt.com/k12-invite?inv_ws_name=w&amp;wId=d1869eec-4d2d-4fce-967f-a1a6b906d51e&amp;aiId=abc">
      Join workspace
    </a>
    """

    assert _extract_verification_link(html, "k12-invite") == (
        "https://chatgpt.com/k12-invite?inv_ws_name=w"
        "&wId=d1869eec-4d2d-4fce-967f-a1a6b906d51e&aiId=abc"
    )


def test_open_workspace_invite_requires_clicking_invite_button():
    class FakePage:
        def __init__(self):
            self.url = ""

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, _ms):
            return None

        def evaluate(self, _script):
            return {"clicked": False, "text": "", "url": self.url}

    result = open_workspace_invite_in_browser(
        FakePage(),
        "https://chatgpt.com/k12-invite?wId=workspace-1&aiId=invite-1",
    )

    assert result["ok"] is False
    assert result["clicked"] is False
    assert "invite" in result["error"]


def test_open_workspace_invite_recognizes_go_to_teachers_button():
    button_text = "\u8f6c\u81f3 ChatGPT for Teachers"

    class FakePage:
        def __init__(self):
            self.url = ""

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, _ms):
            return None

        def evaluate(self, script):
            clicked = "\u8f6c\u81f3\\s*ChatGPT\\s*for\\s*Teachers" in script
            return {
                "clicked": clicked,
                "text": button_text if clicked else "",
                "url": self.url,
            }

    result = open_workspace_invite_in_browser(
        FakePage(),
        "https://chatgpt.com/k12-invite?wId=workspace-1&aiId=invite-1",
    )

    assert result["ok"] is True
    assert result["clicked"] is True
    assert result["clicked_text"] == button_text


def test_open_workspace_invite_waits_for_late_invite_button():
    class FakePage:
        def __init__(self):
            self.url = ""
            self.evaluate_calls = 0

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, _ms):
            return None

        def evaluate(self, _script):
            self.evaluate_calls += 1
            if self.evaluate_calls < 3:
                return {"clicked": False, "text": "", "url": self.url}
            return {
                "clicked": True,
                "text": "转至 ChatGPT for Teachers",
                "url": self.url,
            }

    page = FakePage()
    result = open_workspace_invite_in_browser(
        page,
        "https://chatgpt.com/k12-invite?wId=workspace-1&aiId=invite-1",
    )

    assert result["ok"] is True
    assert result["clicked"] is True
    assert page.evaluate_calls == 3


def test_workspace_join_flow_exports_cpa_and_returns_workspace_credentials(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    class FakeMailbox:
        def get_current_ids(self, _account):
            return set()

        def wait_for_link(self, *_args, **_kwargs):
            return "https://chatgpt.com/k12-invite?wId=workspace-1&aiId=invite-1"

    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda *_args, **_kwargs: [{"ok": True, "workspace_id": "workspace-1"}],
    )
    monkeypatch.setattr(
        workspace_join,
        "open_workspace_invite_in_browser",
        lambda *_args, **_kwargs: {"ok": True, "clicked": True},
    )
    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        lambda *_args, **_kwargs: {
            "ok": True,
            "path": str(tmp_path / "member.json"),
            "email": "member@example.com",
            "account_id": "workspace-1-account",
            "expired": "2026-07-01T00:00:00Z",
            "access_token": "workspace-access",
            "refresh_token": "",
            "id_token": "workspace-id",
            "session_token": "workspace-session",
        },
        raising=False,
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=FakeMailbox(),
        mailbox_account=object(),
        config={
            "workspace_ids": "workspace-1",
            "accept_invite": True,
            "export_cpa_json": True,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert result["access_token"] == "workspace-access"
    assert result["id_token"] == "workspace-id"
    assert result["session_token"] == "workspace-session"
    assert result["account_id"] == "workspace-1-account"
    assert result["workspace_join"]["cpa_export"]["path"].endswith("member.json")
    assert len(result["workspace_join"]["cpa_exports"]) == 1


def test_workspace_join_flow_exports_cpa_for_each_workspace(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda *_args, **_kwargs: [
            {"ok": True, "workspace_id": "workspace-1"},
            {"ok": True, "workspace_id": "workspace-2"},
        ],
    )

    calls = []

    def fake_export(_page, *, workspace_id, output_dir=None, log=None, **_kwargs):
        calls.append(workspace_id)
        return {
            "ok": True,
            "path": str(tmp_path / f"{workspace_id}.json"),
            "workspace_id": workspace_id,
            "email": "member@example.com",
            "account_id": f"{workspace_id}-account",
            "expired": "2026-07-01T00:00:00Z",
            "access_token": f"{workspace_id}-access",
            "refresh_token": "",
            "id_token": f"{workspace_id}-id",
            "session_token": f"{workspace_id}-session",
        }

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fake_export,
        raising=False,
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=None,
        config={
            "workspace_ids": "workspace-1\nworkspace-2",
            "accept_invite": True,
            "export_cpa_json": True,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert result["workspace_id"] == "workspace-1"
    assert result["access_token"] == "workspace-1-access"
    assert calls == ["workspace-1", "workspace-2"]
    assert [item["workspace_id"] for item in result["workspace_join"]["cpa_exports"]] == [
        "workspace-1",
        "workspace-2",
    ]


def test_export_joined_workspace_cpa_sessions_does_not_request_join(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    def fail_request(*_args, **_kwargs):
        raise AssertionError("retry export must not send workspace join requests")

    monkeypatch.setattr(workspace_join, "request_workspace_join_in_browser", fail_request)

    calls = []

    def fake_export(_page, *, workspace_id, output_dir=None, log=None, **_kwargs):
        calls.append((workspace_id, output_dir))
        return {
            "ok": True,
            "path": str(tmp_path / f"{workspace_id}.json"),
            "workspace_id": workspace_id,
            "email": "member@example.com",
            "account_id": f"{workspace_id}-account",
            "expired": "2026-07-01T00:00:00Z",
            "access_token": f"{workspace_id}-access",
            "refresh_token": "",
            "id_token": f"{workspace_id}-id",
            "session_token": f"{workspace_id}-session",
        }

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fake_export,
        raising=False,
    )

    result = export_joined_workspace_cpa_sessions(
        object(),
        config={
            "workspace_ids": "workspace-1\nworkspace-2",
            "export_cpa_json": True,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert calls == [("workspace-1", str(tmp_path)), ("workspace-2", str(tmp_path))]
    assert result["workspace_join"]["ok"] is True
    assert result["workspace_id"] == "workspace-1"
    assert result["access_token"] == "workspace-1-access"
    assert [item["workspace_id"] for item in result["workspace_join"]["cpa_exports"]] == [
        "workspace-1",
        "workspace-2",
    ]


def test_export_joined_workspace_cpa_sessions_can_use_codex_oauth(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    def fail_session_export(*_args, **_kwargs):
        raise AssertionError("codex oauth export must not use ChatGPT session/UI export")

    oauth_calls = []

    def fake_oauth_export(_page, *, session_info, workspace_id, output_dir=None, proxy=None, log=None):
        oauth_calls.append(
            {
                "email": session_info.get("email"),
                "workspace_id": workspace_id,
                "output_dir": output_dir,
                "proxy": proxy,
            }
        )
        return {
            "ok": True,
            "path": str(tmp_path / f"{workspace_id}.json"),
            "workspace_id": workspace_id,
            "method": "codex_oauth_workspace_select",
            "email": session_info.get("email"),
            "account_id": workspace_id,
            "access_token": f"{workspace_id}-access",
            "refresh_token": "",
            "id_token": f"{workspace_id}-id",
            "session_token": f"{workspace_id}-session",
            "expired": "2026-07-01T00:00:00Z",
        }

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fail_session_export,
        raising=False,
    )
    monkeypatch.setattr(
        workspace_join,
        "_export_workspace_cpa_session_via_codex_oauth",
        fake_oauth_export,
        raising=False,
    )

    result = export_joined_workspace_cpa_sessions(
        object(),
        config={
            "workspace_ids": "workspace-1",
            "export_cpa_json": True,
            "export_method": "codex_oauth",
            "cpa_output_dir": str(tmp_path),
        },
        session_info={"email": "member@example.com", "password": "Secret123!"},
        proxy="http://proxy.local:8080",
    )

    assert oauth_calls == [
        {
            "email": "member@example.com",
            "workspace_id": "workspace-1",
            "output_dir": str(tmp_path),
            "proxy": "http://proxy.local:8080",
        }
    ]
    assert result["workspace_join"]["cpa_exports"][0]["method"] == "codex_oauth_workspace_select"
    assert result["access_token"] == "workspace-1-access"


def test_workspace_join_flow_exports_actual_workspace_when_ui_switches_elsewhere(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda *_args, **_kwargs: [
            {"ok": True, "workspace_id": "workspace-1"},
            {"ok": True, "workspace_id": "workspace-2"},
        ],
    )

    calls = []

    def fake_export(_page, *, workspace_id, output_dir=None, log=None, **_kwargs):
        calls.append(workspace_id)
        if workspace_id == "workspace-1" and calls.count("workspace-1") == 1:
            account_id = "workspace-2-account"
        else:
            account_id = f"{workspace_id}-account"
        return {
            "ok": True,
            "path": str(tmp_path / f"{workspace_id}-{len(calls)}.json"),
            "workspace_id": workspace_id,
            "email": "member@example.com",
            "account_id": account_id,
            "expired": "2026-07-01T00:00:00Z",
            "access_token": f"{workspace_id}-access",
            "refresh_token": "",
            "id_token": f"{workspace_id}-id",
            "session_token": f"{workspace_id}-session",
        }

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fake_export,
        raising=False,
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=None,
        config={
            "workspace_ids": "workspace-1\nworkspace-2",
            "accept_invite": True,
            "export_cpa_json": True,
            "export_retries": 1,
            "export_retry_backoff_ms": 0,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert result["workspace_join"]["ok"] is True
    assert calls == ["workspace-1", "workspace-2"]
    assert [item["account_id"] for item in result["workspace_join"]["cpa_exports"]] == [
        "workspace-2-account",
    ]
    assert [item["workspace_id"] for item in result["workspace_join"]["cpa_exports"]] == [
        "workspace-2",
    ]
    assert "workspace-1" in result["workspace_join"]["export_partial_error"]


def test_workspace_join_flow_fails_duplicate_workspace_account_id(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda *_args, **_kwargs: [
            {"ok": True, "workspace_id": "workspace-1"},
            {"ok": True, "workspace_id": "workspace-2"},
        ],
    )

    def fake_export(_page, *, workspace_id, output_dir=None, log=None, **_kwargs):
        return {
            "ok": True,
            "path": str(tmp_path / f"{workspace_id}.json"),
            "workspace_id": workspace_id,
            "email": "member@example.com",
            "account_id": "workspace-1-account",
            "expired": "2026-07-01T00:00:00Z",
            "access_token": f"{workspace_id}-access",
            "refresh_token": "",
            "id_token": f"{workspace_id}-id",
            "session_token": f"{workspace_id}-session",
        }

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fake_export,
        raising=False,
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=None,
        mailbox_account=None,
        config={
            "workspace_ids": "workspace-1\nworkspace-2",
            "accept_invite": True,
            "export_cpa_json": True,
            "export_retries": 0,
            "cpa_output_dir": str(tmp_path),
        },
    )

    assert result["workspace_join"]["ok"] is True
    assert len(result["workspace_join"]["cpa_exports"]) == 1
    assert "workspace-2"[:8] in result["workspace_join"]["export_partial_error"]
    assert "workspace account_id mismatch" in result["workspace_join"]["export_partial_error"]


def test_workspace_join_flow_fails_when_cpa_export_fails(monkeypatch, tmp_path):
    import platforms.chatgpt.workspace_join as workspace_join

    class FakeMailbox:
        def get_current_ids(self, _account):
            return set()

        def wait_for_link(self, *_args, **_kwargs):
            return "https://chatgpt.com/k12-invite?wId=workspace-1&aiId=invite-1"

    logs: list[str] = []
    monkeypatch.setattr(
        workspace_join,
        "request_workspace_join_in_browser",
        lambda *_args, **_kwargs: [{"ok": True, "workspace_id": "workspace-1"}],
    )
    monkeypatch.setattr(
        workspace_join,
        "open_workspace_invite_in_browser",
        lambda *_args, **_kwargs: {"ok": True, "clicked": True},
    )

    def fail_export(*_args, **_kwargs):
        raise RuntimeError("workspace switch failed")

    monkeypatch.setattr(
        workspace_join,
        "export_workspace_cpa_session_from_browser",
        fail_export,
        raising=False,
    )

    result = run_workspace_join_flow(
        object(),
        {"access_token": "registration-access"},
        mailbox=FakeMailbox(),
        mailbox_account=object(),
        config={
            "workspace_ids": "workspace-1",
            "accept_invite": True,
            "export_cpa_json": True,
            "cpa_output_dir": str(tmp_path),
        },
        log=logs.append,
    )

    assert result["workspace_join"]["ok"] is True
    assert result["workspace_join"]["export_partial_error"] == (
        "Workspace Join switch/export partial failed: workspace-1: workspace switch failed"
    )
    assert result["workspace_join"]["cpa_export"] == {
        "ok": False,
        "error": "workspace-1: workspace switch failed",
    }
    assert any("Workspace Join switch/export partial failed" in item for item in logs)
