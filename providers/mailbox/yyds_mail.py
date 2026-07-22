"""YYDS mailbox provider registration."""

from core.yyds_mailbox import YYDSMailbox  # noqa: F401
from providers.registry import register_provider


register_provider("mailbox", "yyds_mail_api")(YYDSMailbox)
