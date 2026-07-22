from types import SimpleNamespace

from application.tasks import _release_failed_mailbox_resource
from core.base_mailbox import MailboxAccount


class _Logger:
    def __init__(self):
        self.messages = []

    def log(self, message, level="info"):
        self.messages.append((level, message))


def test_release_failed_mailbox_resource_releases_last_identity_mailbox():
    released = []
    mailbox_account = MailboxAccount(email="user@gmail.com", account_id="mail-123")
    platform = SimpleNamespace(
        _last_identity=SimpleNamespace(mailbox_account=mailbox_account)
    )
    shared_mailbox = SimpleNamespace(
        release=lambda account, reason="": released.append((account, reason)) or True
    )
    logger = _Logger()

    assert _release_failed_mailbox_resource(
        shared_mailbox,
        platform,
        logger,
        reason="注册密码失败",
    ) is True

    assert released == [(mailbox_account, "注册密码失败")]
    assert any("已释放邮箱资源" in message for _, message in logger.messages)
