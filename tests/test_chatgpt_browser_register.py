from platforms.chatgpt import browser_register


class _Page:
    url = "https://chatgpt.com/"


def test_registration_flow_returns_immediately_after_reaching_chatgpt_home(monkeypatch):
    monkeypatch.setattr(browser_register, "_seed_browser_device_id", lambda *_args: None)
    monkeypatch.setattr(
        browser_register,
        "_start_browser_signup_via_authorize",
        lambda *_args: {"page_type": "chatgpt_home", "current_url": "https://chatgpt.com/"},
    )
    monkeypatch.setattr(browser_register, "_get_cookies", lambda *_args: {})
    monkeypatch.setattr(
        browser_register,
        "_handle_post_signup_onboarding",
        lambda *_args: (_ for _ in ()).throw(AssertionError("onboarding must not block registration completion")),
    )

    state = browser_register._browser_registration_flow(
        _Page(),
        "new@example.com",
        "Secret123!",
        lambda: "123456",
        lambda _message: None,
    )

    assert state["page_type"] == "chatgpt_home"
