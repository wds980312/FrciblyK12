from __future__ import annotations

import pytest

from application.tasks import _resolve_mailbox_provider_for_task
from application.tasks import _resolve_sms_provider_for_task
from application.tasks import _resolve_registration_proxy_for_platform
from application.tasks import _is_smsbower_mail_stop_error
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository


@pytest.fixture(autouse=True)
def _seed_provider_definitions():
    ProviderDefinitionsRepository().ensure_seeded()


def test_resolve_sms_provider_for_task_uses_saved_herosms_default():
    repo = ProviderSettingsRepository()
    repo.save(
        setting_id=None,
        provider_type="sms",
        provider_key="herosms_api",
        display_name="HeroSMS",
        auth_mode="api_key",
        enabled=True,
        is_default=True,
        config={
            "sms_service": "dr",
            "sms_country": "187",
            "register_phone_extra_max": "3",
        },
        auth={"herosms_api_key": "hero123"},
        metadata={},
    )

    provider_key, settings = _resolve_sms_provider_for_task({})

    assert provider_key == "herosms_api"
    assert settings["herosms_api_key"] == "hero123"
    assert settings["sms_service"] == "dr"


def test_resolve_sms_provider_for_task_allows_inline_override():
    provider_key, settings = _resolve_sms_provider_for_task({
        "sms_provider": "herosms",
        "herosms_api_key": "inline",
        "sms_country": "52",
    })

    assert provider_key == "herosms"
    assert settings["herosms_api_key"] == "inline"
    assert settings["sms_country"] == "52"


def test_resolve_mailbox_provider_for_task_uses_saved_smsbower_alias_default():
    repo = ProviderSettingsRepository()
    repo.save(
        setting_id=None,
        provider_type="mailbox",
        provider_key="smsbower_mail_api",
        display_name="SMSBower Mail",
        auth_mode="api_key",
        enabled=True,
        is_default=True,
        config={
            "smsbower_mail_service": "go",
            "smsbower_mail_domain": "gmail.com",
            "smsbower_mail_alias": "1",
            "smsbower_mail_reuse_limit": "5",
        },
        auth={"smsbower_mail_api_key": "mail-key"},
        metadata={},
    )

    provider_key, settings = _resolve_mailbox_provider_for_task({})

    assert provider_key == "smsbower_mail_api"
    assert settings["mail_provider"] == "smsbower_mail_api"
    assert settings["smsbower_mail_api_key"] == "mail-key"
    assert settings["smsbower_mail_alias"] == "1"
    assert settings["smsbower_mail_reuse_limit"] == "5"


def test_smsbower_mail_activation_failures_stop_alias_batch_retries():
    assert _is_smsbower_mail_stop_error(
        "SMSBower Mail 获取邮箱失败: {'status': 0, 'error': 'No mails yet'}"
    )
    assert _is_smsbower_mail_stop_error(
        "HTTPSConnectionPool(host='smsbower.app'): /api/mail/getActivation SSLEOFError"
    )
    assert not _is_smsbower_mail_stop_error("ChatGPT 表单提交失败，请重试")


def test_chatgpt_registration_does_not_use_proxy_pool_or_explicit_proxy():
    calls = []

    proxy = _resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy="http://explicit-proxy.example:8080",
        proxy_getter=lambda: calls.append("called") or "http://pool-proxy.example:8080",
    )

    assert proxy is None
    assert calls == []


def test_non_chatgpt_registration_still_uses_proxy_pool():
    proxy = _resolve_registration_proxy_for_platform(
        "windsurf",
        explicit_proxy="",
        proxy_getter=lambda: "http://pool-proxy.example:8080",
    )

    assert proxy == "http://pool-proxy.example:8080"
