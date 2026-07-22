"""YYDS mailbox provider tests."""
from __future__ import annotations

from core.base_mailbox import create_mailbox
from core.generic_http_mailbox import GenericHttpMailbox
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.proxies = {}
        self.verify = True
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if method == "POST" and url == "https://maliapi.215.im/v1/accounts":
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "address": "fresh@example.test",
                        "token": "temp-mail-token",
                    },
                }
            )
        if method == "GET" and url == "https://maliapi.215.im/v1/messages":
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "messages": [
                            {
                                "id": "msg-1",
                                "subject": "Your verification code",
                            }
                        ]
                    },
                }
            )
        if method == "GET" and url == "https://maliapi.215.im/v1/messages/msg-1":
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "id": "msg-1",
                        "verificationCode": "ABC-123",
                        "text": "Your code is ABC-123",
                    },
                }
            )
        raise AssertionError(f"unexpected request: {method} {url}")


def test_yyds_provider_definition_and_generic_pipeline_are_wired(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr("requests.Session", lambda: session)
    ProviderDefinitionsRepository().ensure_seeded()

    definition = ProviderDefinitionsRepository().get_by_key("mailbox", "yyds_mail_api")
    mailbox = create_mailbox(
        "yyds_mail_api",
        extra={
            "yyds_api_url": "https://maliapi.215.im/v1",
            "yyds_api_key": "fake-yyds-key",
        },
    )

    account = mailbox.get_email()
    code = mailbox.wait_for_code(
        account,
        timeout=1,
        code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
    )

    assert definition is not None
    assert definition.driver_type == "generic_http_mailbox"
    assert isinstance(mailbox, GenericHttpMailbox)
    assert account.email == "fresh@example.test"
    assert account.account_id == "temp-mail-token"
    assert code == "ABC-123"
    assert session.calls[0]["kwargs"]["headers"] == {
        "X-API-Key": "fake-yyds-key",
        "Content-Type": "application/json",
    }
    assert session.calls[0]["kwargs"]["json"] == {"autoDomainStrategy": "prefer_owned"}
    assert session.calls[1]["kwargs"]["headers"] == {"Authorization": "Bearer temp-mail-token"}
    assert session.calls[1]["kwargs"]["params"] == {"address": "fresh@example.test"}
    assert session.calls[2]["kwargs"]["headers"] == {"Authorization": "Bearer temp-mail-token"}
