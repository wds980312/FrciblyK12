import pytest

from core.base_mailbox import MailboxAccount, SmsBowerMailMailbox


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = str(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_smsbower_mailbox_get_email_uses_gmail_domain_by_default(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append((url, dict(params or {}), timeout, proxies))
        return _FakeResponse(
            {"status": 1, "mail": "user@gmail.com", "mailId": "mail-123"}
        )

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(api_key="key")
    account = mailbox.get_email()

    assert account.email == "user@gmail.com"
    assert account.account_id == "mail-123"
    assert calls[0][0].endswith("/api/mail/getActivation")
    assert calls[0][1]["api_key"] == "key"
    assert calls[0][1]["domain"] == "gmail.com"


def test_smsbower_mailbox_get_email_waits_for_activation_stock(monkeypatch):
    calls = []
    sleeps = []
    clock = {"now": 0.0}

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append((url, dict(params or {})))
        if url.endswith("/api/mail/getActivation") and len(calls) == 1:
            return _FakeResponse({"status": 0, "error": "No mails yet"})
        if url.endswith("/api/mail/getActivation"):
            return _FakeResponse(
                {"status": 1, "mail": "waited@gmail.com", "mailId": "mail-waited"}
            )
        raise AssertionError(url)

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += float(seconds)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.time", lambda: clock["now"])
    monkeypatch.setattr("time.sleep", fake_sleep)

    mailbox = SmsBowerMailMailbox(
        api_key="key",
        activation_wait_seconds=10,
        activation_poll_interval=2,
    )

    account = mailbox.get_email()

    assert account.email == "waited@gmail.com"
    assert account.account_id == "mail-waited"
    assert len([call for call in calls if call[0].endswith("/api/mail/getActivation")]) == 2
    assert sleeps == [0.5, 0.5, 0.5, 0.5]


def test_smsbower_mailbox_get_email_default_waits_until_cancelled(monkeypatch):
    calls = []
    clock = {"now": 0.0}
    cancelled = {"value": False}

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append((url, dict(params or {})))
        if url.endswith("/api/mail/getActivation"):
            return _FakeResponse({"status": 0, "error": "No mails yet"})
        raise AssertionError(url)

    def fake_sleep(seconds):
        clock["now"] += float(seconds)
        cancelled["value"] = True

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.time", lambda: clock["now"])
    monkeypatch.setattr("time.sleep", fake_sleep)

    mailbox = SmsBowerMailMailbox(
        api_key="key",
        activation_poll_interval=3,
        cancel_check=lambda: cancelled["value"],
    )

    with pytest.raises(RuntimeError, match="任务已取消"):
        mailbox.get_email()

    assert mailbox.activation_wait_seconds == 0
    assert len([call for call in calls if call[0].endswith("/api/mail/getActivation")]) == 1


def test_smsbower_mailbox_uses_manual_activation_pool_before_buying(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append(url)
        if url.endswith("/api/mail/getCode"):
            return _FakeResponse({"status": 1, "code": "123456"})
        if url.endswith("/api/mail/setStatus"):
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(
        api_key="key",
        alias=True,
        reuse_limit=2,
        manual_activations="manual@gmail.com----11686414",
        alias_prefix="gpt",
    )

    first = mailbox.get_email()
    mailbox.wait_for_code(first, timeout=1)
    second = mailbox.get_email()

    assert first.email == "manual+gpt01@gmail.com"
    assert second.email == "manual+gpt02@gmail.com"
    assert first.account_id == "11686414"
    assert second.account_id == "11686414"
    assert not any(url.endswith("/api/mail/getActivation") for url in calls)


def test_smsbower_mailbox_rejects_malformed_manual_activation_pool():
    mailbox = SmsBowerMailMailbox(
        api_key="key",
        manual_activations="manual@gmail.com",
    )

    with pytest.raises(RuntimeError, match="手动邮箱 activation 格式错误"):
        mailbox.get_email()


def test_smsbower_mailbox_wait_for_code_reports_success(monkeypatch):
    actions = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        params = dict(params or {})
        if url.endswith("/api/mail/getCode"):
            return _FakeResponse({"status": 1, "code": "123456"})
        if url.endswith("/api/mail/setStatus"):
            actions.append(params)
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(api_key="key")
    account = MailboxAccount(email="user@gmail.com", account_id="mail-123")

    assert mailbox.wait_for_code(account, timeout=1) == "123456"
    assert actions == [{"api_key": "key", "id": "mail-123", "status": 3}]


def test_smsbower_mailbox_alias_reuses_one_activation_for_five_addresses(monkeypatch):
    calls = []
    codes = iter(["111111", "222222", "333333", "444444", "555555"])

    def fake_get(url, params=None, timeout=None, proxies=None):
        params = dict(params or {})
        calls.append((url, params))
        if url.endswith("/api/mail/getActivation"):
            return _FakeResponse(
                {"status": 1, "mail": "user@gmail.com", "mailId": "mail-123"}
            )
        if url.endswith("/api/mail/getCode"):
            return _FakeResponse({"status": 1, "code": next(codes)})
        if url.endswith("/api/mail/setStatus"):
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(
        api_key="key",
        alias=True,
        reuse_limit=5,
        alias_prefix="gpt",
    )

    accounts = []
    for _ in range(5):
        account = mailbox.get_email()
        accounts.append(account)
        mailbox.wait_for_code(account, timeout=1)

    assert [account.email for account in accounts] == [
        "user+gpt01@gmail.com",
        "user+gpt02@gmail.com",
        "user+gpt03@gmail.com",
        "user+gpt04@gmail.com",
        "user+gpt05@gmail.com",
    ]
    assert {account.account_id for account in accounts} == {"mail-123"}

    activation_calls = [
        item for item in calls if item[0].endswith("/api/mail/getActivation")
    ]
    status_calls = [
        item[1] for item in calls if item[0].endswith("/api/mail/setStatus")
    ]
    assert len(activation_calls) == 1
    assert status_calls == [
        {"api_key": "key", "id": "mail-123", "status": 5},
        {"api_key": "key", "id": "mail-123", "status": 5},
        {"api_key": "key", "id": "mail-123", "status": 5},
        {"api_key": "key", "id": "mail-123", "status": 5},
        {"api_key": "key", "id": "mail-123", "status": 3},
    ]


def test_smsbower_mailbox_alias_rotates_activation_when_next_code_fails(monkeypatch):
    activations = iter(
        [
            {"status": 1, "mail": "first@gmail.com", "mailId": "mail-1"},
            {"status": 1, "mail": "second@gmail.com", "mailId": "mail-2"},
        ]
    )
    actions = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        params = dict(params or {})
        if url.endswith("/api/mail/getActivation"):
            return _FakeResponse(next(activations))
        if url.endswith("/api/mail/getCode"):
            return _FakeResponse({"status": 1, "code": "123456"})
        if url.endswith("/api/mail/setStatus"):
            actions.append(params)
            if params.get("status") == 5:
                return _FakeResponse({"status": 0, "message": "Maximum number of codes reached"})
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(api_key="key", alias=True, reuse_limit=5)
    first = mailbox.get_email()
    mailbox.wait_for_code(first, timeout=1)
    second = mailbox.get_email()

    assert first.email == "first+gpt01@gmail.com"
    assert second.email == "second+gpt01@gmail.com"
    assert first.account_id == "mail-1"
    assert second.account_id == "mail-2"
    assert actions == [{"api_key": "key", "id": "mail-1", "status": 5}]


def test_smsbower_mailbox_activation_limit_stops_buying_new_mail(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append(url)
        if url.endswith("/api/mail/getActivation"):
            return _FakeResponse(
                {"status": 1, "mail": "first@gmail.com", "mailId": "mail-1"}
            )
        if url.endswith("/api/mail/getCode"):
            return _FakeResponse({"status": 1, "code": "123456"})
        if url.endswith("/api/mail/setStatus"):
            return _FakeResponse({"status": 0, "message": "Maximum number of codes reached"})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(
        api_key="key",
        alias=True,
        reuse_limit=5,
        activation_limit=1,
    )

    account = mailbox.get_email()
    mailbox.wait_for_code(account, timeout=1)

    with pytest.raises(RuntimeError, match="activation 已达本任务上限"):
        mailbox.get_email()

    assert len([url for url in calls if url.endswith("/api/mail/getActivation")]) == 1


def test_smsbower_mailbox_wait_for_code_cancels_on_timeout(monkeypatch):
    actions = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        params = dict(params or {})
        if url.endswith("/api/mail/setStatus"):
            actions.append(params)
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(api_key="key")
    account = MailboxAccount(email="user@gmail.com", account_id="mail-123")

    with pytest.raises(TimeoutError, match="等待验证码超时"):
        mailbox.wait_for_code(account, timeout=0)

    assert actions == [{"api_key": "key", "id": "mail-123", "status": 2}]


def test_smsbower_mailbox_release_cancels_activation(monkeypatch):
    actions = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        params = dict(params or {})
        if url.endswith("/api/mail/setStatus"):
            actions.append(params)
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)

    mailbox = SmsBowerMailMailbox(api_key="key")
    account = MailboxAccount(email="user@gmail.com", account_id="mail-123")

    assert mailbox.release(account, reason="registration failed") is True

    assert actions == [{"api_key": "key", "id": "mail-123", "status": 2}]


def test_smsbower_mailbox_wait_for_link_tolerates_transient_timeout(monkeypatch):
    import requests

    calls = []

    def fake_get(url, params=None, timeout=None, proxies=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("temporary timeout")
        if url.endswith("/api/mail/getCode"):
            return _FakeResponse(
                {
                    "status": 1,
                    "code": "Open https://chatgpt.com/k12-invite?token=abc to join",
                }
            )
        if url.endswith("/api/mail/setStatus"):
            return _FakeResponse({"status": 1})
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = SmsBowerMailMailbox(api_key="key")
    account = MailboxAccount(email="user@gmail.com", account_id="mail-123")

    link = mailbox.wait_for_link(account, keyword="k12-invite", timeout=5)

    assert link == "https://chatgpt.com/k12-invite?token=abc"
    assert len(calls) >= 2
