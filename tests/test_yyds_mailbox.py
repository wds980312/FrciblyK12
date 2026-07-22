from __future__ import annotations

from core.base_mailbox import create_mailbox
from core.yyds_mailbox import YYDSMailbox
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.text = str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, "kwargs": kwargs})
        if url == "https://maliapi.215.im/v1/accounts":
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "address": "fresh@example.test",
                        "token": "temp-mail-token",
                    },
                }
            )
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        if url == "https://maliapi.215.im/v1/messages":
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "messages": [
                            {"id": "old", "subject": "old code"},
                            {"id": "new", "subject": "Your temporary ChatGPT login code"},
                        ]
                    },
                }
            )
        if url == "https://maliapi.215.im/v1/messages/new":
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "id": "new",
                        "verificationCode": "654321",
                        "text": "Your temporary ChatGPT login code is 654321",
                    },
                }
            )
        raise AssertionError(f"unexpected GET {url}")


def test_yyds_provider_definition_and_factory_are_wired(monkeypatch):
    monkeypatch.setattr("core.yyds_mailbox.YYDSMailbox._new_session", lambda self: FakeSession())
    ProviderDefinitionsRepository().ensure_seeded()

    definition = ProviderDefinitionsRepository().get_by_key("mailbox", "yyds_mail_api")
    mailbox = create_mailbox(
        "yyds_mail_api",
        extra={
            "yyds_api_url": "https://maliapi.215.im/v1",
            "yyds_api_key": "fake-yyds-key",
        },
    )

    assert definition is not None
    assert definition.driver_type == "yyds_mail_api"
    assert isinstance(mailbox, YYDSMailbox)


def test_yyds_creates_mailbox_and_reads_new_verification_code():
    session = FakeSession()
    mailbox = YYDSMailbox(
        api_url="https://maliapi.215.im/v1",
        api_key="fake-yyds-key",
        poll_interval=0,
        session=session,
    )

    account = mailbox.get_email()
    code = mailbox.wait_for_code(account, timeout=1, before_ids={"old"})

    assert account.email == "fresh@example.test"
    assert account.account_id == "temp-mail-token"
    assert code == "654321"
    assert session.calls[0]["kwargs"]["headers"]["X-API-Key"] == "fake-yyds-key"
    assert session.calls[1]["kwargs"]["headers"]["Authorization"] == "Bearer temp-mail-token"
    assert session.calls[1]["kwargs"]["params"] == {"address": "fresh@example.test"}
