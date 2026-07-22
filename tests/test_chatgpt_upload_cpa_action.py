from core.base_platform import Account, RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_chatgpt_upload_cpa_capability_uses_cpa_upload_helpers(monkeypatch):
    seen = {}

    def fake_generate_token_json(account):
        seen["account"] = account
        return {"email": account.email, "account_id": account.account_id}

    def fake_upload_to_cpa(token_data, api_url=None, api_key=None, proxy=None):
        seen["token_data"] = token_data
        seen["api_url"] = api_url
        seen["api_key"] = api_key
        return True, "uploaded"

    monkeypatch.setattr("platforms.chatgpt.cpa_upload.generate_token_json", fake_generate_token_json)
    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", fake_upload_to_cpa)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "upload_cpa",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            user_id="personal-account",
            token="access-token",
            extra={
                "account_id": "workspace-account",
                "access_token": "extra-access-token",
                "refresh_token": "refresh-token",
            },
        ),
        {"api_url": "https://cpa.example", "api_key": "secret"},
    )

    assert result == {"ok": True, "data": {"message": "uploaded", "email": "user@example.com"}}
    assert seen["account"].account_id == "workspace-account"
    assert seen["account"].access_token == "extra-access-token"
    assert seen["token_data"] == {"email": "user@example.com", "account_id": "workspace-account"}
    assert seen["api_url"] == "https://cpa.example"
    assert seen["api_key"] == "secret"


def test_chatgpt_upload_cpa_capability_uses_workspace_export_files(monkeypatch, tmp_path):
    uploads = []
    export_one = tmp_path / "workspace-one.json"
    export_two = tmp_path / "workspace-two.json"
    export_one.write_text('{"email":"user@example.com","account_id":"space-one"}', encoding="utf-8")
    export_two.write_text('{"email":"user@example.com","account_id":"space-two"}', encoding="utf-8")

    def fake_upload_to_cpa(token_data, api_url=None, api_key=None, filename=None, proxy=None):
        uploads.append((token_data, api_url, api_key, filename))
        return True, "uploaded"

    monkeypatch.setattr("platforms.chatgpt.cpa_upload.upload_to_cpa", fake_upload_to_cpa)
    monkeypatch.setattr(
        "platforms.chatgpt.cpa_upload.generate_token_json",
        lambda _account: {"email": "fallback@example.com", "account_id": "personal"},
    )

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "upload_cpa",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            user_id="personal-account",
            token="access-token",
            extra={
                "workspace_join": {
                    "cpa_exports": [
                        {"path": str(export_one), "workspace_id": "space-one"},
                        {"path": str(export_two), "workspace_id": "space-two"},
                    ]
                }
            },
        ),
        {"api_url": "https://cpa.example", "api_key": "secret"},
    )

    assert result == {
        "ok": True,
        "data": {
            "message": "uploaded 2 workspace CPA files",
            "email": "user@example.com",
            "uploads": [
                {"ok": True, "message": "uploaded", "workspace_id": "space-one", "filename": "user@example.com-space-on.json"},
                {"ok": True, "message": "uploaded", "workspace_id": "space-two", "filename": "user@example.com-space-tw.json"},
            ],
        },
    }
    assert uploads == [
        ({"email": "user@example.com", "account_id": "space-one"}, "https://cpa.example", "secret", "user@example.com-space-on.json"),
        ({"email": "user@example.com", "account_id": "space-two"}, "https://cpa.example", "secret", "user@example.com-space-tw.json"),
    ]


def test_chatgpt_open_local_browser_injects_cookies_via_cdp(monkeypatch):
    events = {}

    monkeypatch.setattr(
        "platforms.chatgpt.payment._parse_cookie_str",
        lambda _cookies, domain: [{"name": "__Secure-next-auth.session-token", "value": f"{domain}-session", "domain": domain, "path": "/"}],
    )
    monkeypatch.setattr(
        ChatGPTPlatform,
        "_open_host_chrome_via_cdp",
        staticmethod(lambda **kwargs: events.update(kwargs) or {"cookies_injected": len(kwargs["cookies"]), "cookies_skipped": 0}),
    )

    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "open_local_browser",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            extra={"cookies": "__Secure-next-auth.session-token=sess; oai-did=did"},
        ),
        {
            "url": "https://chatgpt.com/",
            "chrome_cdp_url": "http://host.docker.internal:9222",
        },
    )

    assert result["ok"] is True
    assert events["cdp_url"] == "http://host.docker.internal:9222"
    assert events["url"] == "https://chatgpt.com/"
    assert [item["name"] for item in events["cookies"]] == [
        "__Secure-next-auth.session-token",
        "__Secure-next-auth.session-token",
    ]


def test_chatgpt_open_local_browser_rejects_untrusted_url():
    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "open_local_browser",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            extra={"cookies": "__Secure-next-auth.session-token=sess"},
        ),
        {"url": "https://example.com/"},
    )

    assert result["ok"] is False
    assert "只允许打开" in result["error"]


def test_chatgpt_open_local_browser_default_cdp_uses_resolved_host_ip(monkeypatch):
    monkeypatch.setattr("socket.gethostbyname", lambda host: "192.168.65.254" if host == "host.docker.internal" else "127.0.0.1")

    assert ChatGPTPlatform._default_host_chrome_cdp_url() == "http://192.168.65.254:9222"


def test_chatgpt_open_local_browser_clears_chatgpt_openai_origins():
    assert ChatGPTPlatform._local_chrome_state_clear_origins("https://chatgpt.com/") == [
        "https://chatgpt.com",
        "https://auth.openai.com",
        "https://openai.com",
    ]


def test_chatgpt_open_local_browser_normalizes_host_prefixed_cookies():
    cookies = ChatGPTPlatform._normalize_local_chrome_cookie_list(
        [
            {"name": "__Host-next-auth.csrf-token", "value": "csrf", "domain": ".chatgpt.com", "path": "/"},
            {"name": "__Secure-next-auth.session-token", "value": "sess", "domain": ".chatgpt.com", "path": "/"},
            {"name": "bad name", "value": "skip", "domain": ".chatgpt.com", "path": "/"},
        ],
        domain="chatgpt.com",
    )

    assert cookies[0] == {
        "name": "__Host-next-auth.csrf-token",
        "value": "csrf",
        "path": "/",
        "url": "https://chatgpt.com",
        "secure": True,
    }
    assert "domain" not in cookies[0]
    assert cookies[1]["domain"] == ".chatgpt.com"
    assert cookies[1]["secure"] is True
    assert [item["name"] for item in cookies] == [
        "__Host-next-auth.csrf-token",
        "__Secure-next-auth.session-token",
    ]


def test_chatgpt_local_workspace_export_normalizes_playwright_cookies():
    cookies = ChatGPTPlatform._normalize_local_chrome_cookies_for_playwright(
        [
            {"name": "__Secure-next-auth.session-token", "value": "sess", "domain": None, "path": "/"},
            {"name": "__Secure-next-auth.session-token.0", "value": "chunk0", "domain": ".chatgpt.com", "path": None},
            {"name": "bad name", "value": "skip", "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-sc", "value": "state", "url": "https://chatgpt.com/", "domain": ".chatgpt.com", "path": "/"},
        ],
        default_domain="chatgpt.com",
    )

    assert cookies == [
        {"name": "__Secure-next-auth.session-token", "value": "sess", "url": "https://chatgpt.com/"},
        {"name": "__Secure-next-auth.session-token.0", "value": "chunk0", "domain": ".chatgpt.com", "path": "/"},
        {"name": "oai-sc", "value": "state", "url": "https://chatgpt.com/"},
    ]
    assert all(item.get("url") or (item.get("domain") and item.get("path")) for item in cookies)


def test_chatgpt_open_local_browser_filters_large_unneeded_cookies(monkeypatch):
    events = {}

    def fake_parse(_cookies, domain):
        return [
            {"name": "__Secure-next-auth.session-token", "value": "sess", "domain": domain, "path": "/"},
            {"name": "_ga", "value": "x" * 10000, "domain": domain, "path": "/"},
            {"name": "ajs_anonymous_id", "value": "analytics", "domain": domain, "path": "/"},
        ]

    monkeypatch.setattr("platforms.chatgpt.payment._parse_cookie_str", fake_parse)
    monkeypatch.setattr(
        ChatGPTPlatform,
        "_open_host_chrome_via_cdp",
        staticmethod(lambda **kwargs: events.update(kwargs) or {"cookies_injected": len(kwargs["cookies"]), "cookies_skipped": 0}),
    )

    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "open_local_browser",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            extra={"cookies": "__Secure-next-auth.session-token=sess; _ga=big; ajs_anonymous_id=analytics"},
        ),
        {
            "url": "https://chatgpt.com/",
            "chrome_cdp_url": "http://host.docker.internal:9222",
        },
    )

    assert result["ok"] is True
    assert [item["name"] for item in events["cookies"]] == [
        "__Secure-next-auth.session-token",
        "__Secure-next-auth.session-token",
    ]


def test_chatgpt_open_local_browser_keeps_chunked_session_cookies(monkeypatch):
    events = {}

    def fake_parse(_cookies, domain):
        return [
            {"name": "__Secure-next-auth.session-token.0", "value": "chunk0", "domain": domain, "path": "/"},
            {"name": "__Secure-next-auth.session-token.1", "value": "chunk1", "domain": domain, "path": "/"},
            {"name": "_ga", "value": "analytics", "domain": domain, "path": "/"},
        ]

    monkeypatch.setattr("platforms.chatgpt.payment._parse_cookie_str", fake_parse)
    monkeypatch.setattr(
        ChatGPTPlatform,
        "_open_host_chrome_via_cdp",
        staticmethod(lambda **kwargs: events.update(kwargs) or {"cookies_injected": len(kwargs["cookies"]), "cookies_skipped": 0}),
    )

    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "open_local_browser",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            extra={"cookies": "__Secure-next-auth.session-token.0=chunk0; __Secure-next-auth.session-token.1=chunk1"},
        ),
        {
            "url": "https://chatgpt.com/",
            "chrome_cdp_url": "http://host.docker.internal:9222",
        },
    )

    assert result["ok"] is True
    assert [item["name"] for item in events["cookies"]] == [
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
    ]


def test_chatgpt_local_workspace_export_uses_local_chrome_listener(monkeypatch):
    events = {}

    def fake_parse(_cookies, domain):
        return [
            {"name": "__Secure-next-auth.session-token.0", "value": f"{domain}-chunk0", "domain": domain, "path": "/"},
            {"name": "__Secure-next-auth.session-token.1", "value": f"{domain}-chunk1", "domain": domain, "path": "/"},
            {"name": "_ga", "value": "analytics", "domain": domain, "path": "/"},
        ]

    def fake_export(**kwargs):
        events.update(kwargs)
        return {
            "captured": [
                {"ok": True, "workspace_id": "space-one", "account_id": "space-one", "email": "user@example.com"},
                {"ok": True, "workspace_id": "space-two", "account_id": "space-two", "email": "user@example.com"},
            ],
            "uploaded_count": 2,
            "missing_workspace_ids": [],
            "cookies_injected": len(kwargs["cookies"]),
            "cookies_skipped": 0,
        }

    monkeypatch.setattr("platforms.chatgpt.payment._parse_cookie_str", fake_parse)
    monkeypatch.setattr(
        ChatGPTPlatform,
        "_export_workspace_cpa_from_local_chrome_via_cdp",
        staticmethod(fake_export),
        raising=False,
    )

    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "local_workspace_export",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            extra={"cookies": "__Secure-next-auth.session-token.0=c0; __Secure-next-auth.session-token.1=c1"},
        ),
        {
            "workspace_ids": "space-one\nspace-two",
            "chrome_cdp_url": "http://host.docker.internal:9222",
            "timeout_sec": "123",
            "poll_ms": "456",
            "api_url": "https://cpa.example",
            "api_key": "secret",
        },
    )

    assert result["ok"] is True
    assert result["data"]["uploaded_count"] == 2
    assert result["data"]["message"] == "本地 Chrome 已捕获并上传 2/2 个 workspace"
    assert events["cdp_url"] == "http://host.docker.internal:9222"
    assert events["workspace_ids"] == ["space-one", "space-two"]
    assert events["timeout_sec"] == 123
    assert events["poll_ms"] == 456
    assert events["api_url"] == "https://cpa.example"
    assert events["api_key"] == "secret"
    assert [item["name"] for item in events["cookies"]] == [
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
    ]


def test_chatgpt_local_workspace_export_applies_alias_first_only_policy(monkeypatch):
    events = {}

    def fake_parse(_cookies, domain):
        return [
            {"name": "__Secure-next-auth.session-token", "value": f"{domain}-sess", "domain": domain, "path": "/"},
        ]

    def fake_export(**kwargs):
        events.update(kwargs)
        return {
            "captured": [
                {"ok": True, "workspace_id": "workspace-allowed", "account_id": "workspace-allowed", "email": "user@example.com"},
            ],
            "uploaded_count": 1,
            "missing_workspace_ids": [],
            "cookies_injected": len(kwargs["cookies"]),
            "cookies_skipped": 0,
        }

    monkeypatch.setattr("platforms.chatgpt.payment._parse_cookie_str", fake_parse)
    monkeypatch.setattr(
        ChatGPTPlatform,
        "_export_workspace_cpa_from_local_chrome_via_cdp",
        staticmethod(fake_export),
        raising=False,
    )

    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "local_workspace_export",
        Account(
            platform="chatgpt",
            email="root+gpt02@gmail.com",
            password="Secret123!",
            extra={
                "cookies": "__Secure-next-auth.session-token=sess",
                "provider_resource": {
                    "metadata": {
                        "alias_index": 2,
                        "base_email": "root@gmail.com",
                    }
                },
                "chatgpt_workspace_join": {
                    "alias_first_only_workspace_ids": "workspace-first-only",
                },
            },
        ),
        {
            "workspace_ids": "workspace-allowed\nworkspace-first-only",
            "chrome_cdp_url": "http://host.docker.internal:9222",
        },
    )

    assert result["ok"] is True
    assert events["workspace_ids"] == ["workspace-allowed"]
    assert result["data"]["workspace_ids"] == ["workspace-allowed"]
    assert result["data"]["configured_workspace_ids"] == ["workspace-allowed", "workspace-first-only"]
    assert result["data"]["alias_policy_skipped"] == ["workspace-first-only"]


def test_chatgpt_local_workspace_export_keeps_first_alias_workspace(monkeypatch):
    events = {}

    def fake_parse(_cookies, domain):
        return [
            {"name": "__Secure-next-auth.session-token", "value": f"{domain}-sess", "domain": domain, "path": "/"},
        ]

    def fake_export(**kwargs):
        events.update(kwargs)
        return {
            "captured": [
                {"ok": True, "workspace_id": item, "account_id": item, "email": "root+gpt01@gmail.com"}
                for item in kwargs["workspace_ids"]
            ],
            "uploaded_count": len(kwargs["workspace_ids"]),
            "missing_workspace_ids": [],
            "cookies_injected": len(kwargs["cookies"]),
            "cookies_skipped": 0,
        }

    monkeypatch.setattr("platforms.chatgpt.payment._parse_cookie_str", fake_parse)
    monkeypatch.setattr(
        ChatGPTPlatform,
        "_export_workspace_cpa_from_local_chrome_via_cdp",
        staticmethod(fake_export),
        raising=False,
    )

    result = ChatGPTPlatform(config=RegisterConfig()).execute_action(
        "local_workspace_export",
        Account(
            platform="chatgpt",
            email="root+gpt01@gmail.com",
            password="Secret123!",
            extra={
                "cookies": "__Secure-next-auth.session-token=sess",
                "provider_resource": {
                    "metadata": {
                        "email": "root+gpt01@gmail.com",
                        "alias_index": 1,
                        "base_email": "root@gmail.com",
                    }
                },
                "chatgpt_workspace_join": {
                    "alias_first_only_workspace_ids": "workspace-first-only",
                },
            },
        ),
        {
            "workspace_ids": "workspace-allowed\nworkspace-first-only",
            "chrome_cdp_url": "http://host.docker.internal:9222",
        },
    )

    assert result["ok"] is True
    assert events["workspace_ids"] == ["workspace-allowed", "workspace-first-only"]
    assert result["data"]["alias_policy_skipped"] == []


def test_chatgpt_local_chrome_ready_wait_polls_until_session_available(monkeypatch):
    values = [
        {"href": "about:blank", "readyState": "complete", "sessionOk": False, "hasAccessToken": False},
        {"href": "https://chatgpt.com/", "readyState": "loading", "sessionOk": False, "hasAccessToken": False},
        {
            "href": "https://chatgpt.com/",
            "readyState": "complete",
            "sessionOk": True,
            "hasAccessToken": True,
            "accountId": "workspace-one",
        },
    ]
    calls = []
    logs = []

    def fake_call(method, params):
        calls.append((method, params))
        return {"result": {"value": values.pop(0)}}

    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda _seconds: None)

    result = ChatGPTPlatform._wait_local_chrome_chatgpt_ready(
        fake_call,
        timeout_sec=5,
        log=logs.append,
    )

    assert result["accountId"] == "workspace-one"
    assert [method for method, _params in calls] == ["Runtime.evaluate", "Runtime.evaluate", "Runtime.evaluate"]
    assert all("fetch('/api/auth/session'" in params["expression"] for _method, params in calls)
    assert logs[-1] == "本地 Chrome Workspace 导出: ChatGPT session 已就绪 account_id=workspac"


def test_chatgpt_local_workspace_export_prefers_existing_chatgpt_cdp_target():
    targets = [
        {"type": "page", "url": "chrome://newtab/", "webSocketDebuggerUrl": "ws://newtab"},
        {"type": "iframe", "url": "https://chatgpt.com/", "webSocketDebuggerUrl": "ws://iframe"},
        {"type": "page", "url": "https://chatgpt.com/", "webSocketDebuggerUrl": "ws://chatgpt"},
        {"type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://example"},
    ]

    target = ChatGPTPlatform._choose_existing_chatgpt_cdp_target(targets)

    assert target["webSocketDebuggerUrl"] == "ws://chatgpt"
