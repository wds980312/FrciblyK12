"""邮箱池基类 - 抽象临时邮箱/收件服务"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import html
import logging
import re
from urllib.parse import urlencode, urlparse

logger = logging.getLogger(__name__)

from core.tls import insecure_request, mark_session_insecure, suppress_insecure_request_warning

# ── 邮箱服务默认 API 地址（统一维护，需要时在此修改） ──
DEFAULT_LAOUDO_API_URL = "https://laoudo.com/api/email"
DEFAULT_AITRE_API_URL = "https://mail.aitre.cc/api/tempmail"
DEFAULT_TEMPMAIL_LOL_API_URL = "https://api.tempmail.lol/v2"
DEFAULT_TEMPMAIL_WEB_BASE_URL = "https://web2.temp-mail.org"
DEFAULT_SMSBOWER_API_URL = "https://smsbower.app"


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class BaseMailbox(ABC):
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        """等待并返回验证链接。默认由具体 provider 自行实现。"""
        raise NotImplementedError(f"{self.__class__.__name__} 暂不支持 wait_for_link()")

    def release(self, account: MailboxAccount, reason: str = "") -> bool:
        """释放未完成注册的邮箱资源。默认 provider 无需处理。"""
        return False


class FallbackMailbox(BaseMailbox):
    """按顺序尝试多个 provider，创建邮箱成功后固定使用同一 provider 收件。"""

    def __init__(self, providers: list[tuple[str, 'BaseMailbox']]):
        self.providers = [(str(key or "").strip(), mailbox) for key, mailbox in providers if str(key or "").strip() and mailbox]
        self._accounts: dict[str, BaseMailbox] = {}

    @staticmethod
    def _inject_provider_metadata(account: MailboxAccount, provider_key: str) -> MailboxAccount:
        account.extra = dict(account.extra or {})
        account.extra["mailbox_provider_key"] = provider_key
        provider_resource = dict((account.extra.get("provider_resource") or {}))
        if provider_resource and not provider_resource.get("provider_name"):
            provider_resource["provider_name"] = provider_key
            account.extra["provider_resource"] = provider_resource
        return account

    def _resolve_mailbox(self, account: MailboxAccount) -> BaseMailbox:
        provider_key = str((account.extra or {}).get("mailbox_provider_key") or "").strip()
        if provider_key:
            for key, mailbox in self.providers:
                if key == provider_key:
                    return mailbox
        mailbox = self._accounts.get(str(account.email or "").strip())
        if mailbox is not None:
            return mailbox
        raise RuntimeError(f"未找到邮箱 provider 上下文: {account.email}")

    def get_email(self) -> MailboxAccount:
        errors: list[str] = []
        for provider_key, mailbox in self.providers:
            try:
                print(f"[Mailbox] 尝试 provider: {provider_key}")
                account = mailbox.get_email()
                self._accounts[str(account.email or "").strip()] = mailbox
                self._inject_provider_metadata(account, provider_key)
                print(f"[Mailbox] 使用 provider 成功: {provider_key} -> {account.email}")
                return account
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                if message == "任务已取消":
                    raise
                errors.append(f"{provider_key}: {message}")
                print(f"[Mailbox] provider 失败: {provider_key} -> {message}")
                continue
        raise RuntimeError("所有邮箱 provider 均创建失败: " + " | ".join(errors))

    def get_current_ids(self, account: MailboxAccount) -> set:
        return self._resolve_mailbox(account).get_current_ids(account)

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        return self._resolve_mailbox(account).wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
        )

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        return self._resolve_mailbox(account).wait_for_link(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
        )

    def release(self, account: MailboxAccount, reason: str = "") -> bool:
        return self._resolve_mailbox(account).release(account, reason=reason)


def _extract_verification_link(text: str, keyword: str = "") -> str | None:
    combined = str(text or "")
    lowered = combined.lower()
    if keyword and keyword.lower() not in lowered:
        return None

    urls = [
        html.unescape(raw).rstrip(").,;")
        for raw in re.findall(r'https?://[^\s<>"\']+', combined, re.IGNORECASE)
    ]
    if not urls:
        return None

    primary_link_hints = ("verif", "confirm", "magic", "auth", "callback", "signin", "signup", "continue", "invite", "k12-invite")
    primary_host_hints = ("tavily", "firecrawl", "clerk", "stytch", "auth", "login")
    for url in urls:
        url_lower = url.lower()
        if any(token in url_lower for token in primary_link_hints) and any(host in url_lower for host in primary_host_hints):
            return url

    verification_hints = ("verify", "verification", "confirm", "magic link", "sign in", "login", "auth", "invite", "workspace", "join", "tavily", "firecrawl")
    if not any(token in lowered for token in verification_hints):
        return None

    for url in urls:
        url_lower = url.lower()
        if any(token in url_lower for token in primary_link_hints):
            return url

    return urls[0]


def _normalize_api_base_url(value: str | None, *, default: str, label: str) -> str:
    raw = str(value or "").strip() or default
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"{label} 无效: {value!r}")
    return raw.rstrip("/")


def _create_tempmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TempMailLolMailbox(proxy=proxy)


def _create_tempmail_web(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TempMailWebMailbox(
        base_url=extra.get("tempmail_web_base_url", ""),
        proxy=proxy,
    )


def _create_smsbower_mail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return SmsBowerMailMailbox(
        api_key=extra.get("smsbower_mail_api_key", ""),
        service=extra.get("smsbower_mail_service", ""),
        domain=extra.get("smsbower_mail_domain", ""),
        max_price=extra.get("smsbower_mail_max_price", ""),
        alias=extra.get("smsbower_mail_alias", ""),
        reuse_limit=extra.get("smsbower_mail_reuse_limit", ""),
        activation_limit=extra.get("smsbower_mail_activation_limit", ""),
        alias_prefix=extra.get("smsbower_mail_alias_prefix", ""),
        manual_activations=extra.get("smsbower_mail_manual_activations", ""),
        activation_wait_seconds=extra.get("smsbower_mail_activation_wait_seconds", ""),
        activation_poll_interval=extra.get("smsbower_mail_activation_poll_interval", ""),
        api_url=extra.get("smsbower_mail_api_url", ""),
        cancel_check=extra.get("_cancel_check"),
        proxy=proxy,
    )


def _create_duckmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return DuckMailMailbox(
        api_url=extra.get("duckmail_api_url", ""),
        provider_url=extra.get("duckmail_provider_url", ""),
        bearer=extra.get("duckmail_bearer", ""),
        proxy=proxy,
    )


def _create_ddg_email(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return DDGEmailMailbox(
        bearer=extra.get("ddg_bearer", ""),
        imap_host=extra.get("ddg_imap_host", ""),
        imap_user=extra.get("ddg_imap_user", ""),
        imap_pass=extra.get("ddg_imap_pass", ""),
        proxy=proxy,
    )


def _create_freemail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return FreemailMailbox(
        api_url=extra.get("freemail_api_url", ""),
        admin_token=extra.get("freemail_admin_token", ""),
        username=extra.get("freemail_username", ""),
        password=extra.get("freemail_password", ""),
        proxy=proxy,
    )


def _create_moemail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return MoeMailMailbox(
        api_url=extra.get("moemail_api_url"),
        username=extra.get("moemail_username", ""),
        password=extra.get("moemail_password", ""),
        session_token=extra.get("moemail_session_token", ""),
        proxy=proxy,
    )


def _create_cfworker(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return CFWorkerMailbox(
        api_url=extra.get("cfworker_api_url", ""),
        admin_token=extra.get("cfworker_admin_token", ""),
        domain=extra.get("cfworker_domain", ""),
        fingerprint=extra.get("cfworker_fingerprint", ""),
        proxy=proxy,
    )


def _create_testmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TestmailMailbox(
        api_url=extra.get("testmail_api_url", ""),
        api_key=extra.get("testmail_api_key", ""),
        namespace=extra.get("testmail_namespace", ""),
        tag_prefix=extra.get("testmail_tag_prefix", ""),
        proxy=proxy,
    )


def _create_outlook_email(extra: dict, proxy: str | None) -> 'BaseMailbox':
    from core.outlook_email_mailbox import OutlookEmailMailbox

    return OutlookEmailMailbox(
        api_url=extra.get("outlook_email_api_url", ""),
        api_key=extra.get("outlook_email_api_key", ""),
        admin_password=extra.get("outlook_email_admin_password", ""),
        fixed_email=extra.get("outlook_email_fixed_email", ""),
        group_id=extra.get("outlook_email_group_id", ""),
        account_limit=extra.get("outlook_email_account_limit", ""),
        account_offset=extra.get("outlook_email_account_offset", ""),
        account_sort_by=extra.get("outlook_email_account_sort_by", ""),
        account_sort_order=extra.get("outlook_email_account_sort_order", ""),
        account_tag_ids=extra.get("outlook_email_account_tag_ids", ""),
        account_include_untagged=extra.get("outlook_email_account_include_untagged", ""),
        email_folder=extra.get("outlook_email_folder", ""),
        email_top=extra.get("outlook_email_top", ""),
        email_subject_contains=extra.get("outlook_email_subject_contains", ""),
        email_from_contains=extra.get("outlook_email_from_contains", ""),
        email_keyword=extra.get("outlook_email_keyword", ""),
        poll_interval=extra.get("outlook_email_poll_interval", ""),
        skip_tag_names=extra.get("outlook_email_skip_tag_names", ""),
        register_success_tag_names=extra.get("outlook_email_register_success_tag_names", ""),
        plus_success_tag_names=extra.get("outlook_email_plus_success_tag_names", ""),
        proxy=proxy,
    )


def _create_local_ms_pool(extra: dict, proxy: str | None) -> 'BaseMailbox':
    from core.local_ms_mailbox import LocalMicrosoftMailboxPool

    return LocalMicrosoftMailboxPool(
        pool_text=extra.get("local_ms_pool_text", ""),
        pool_file=extra.get("local_ms_pool_file", ""),
        state_file=extra.get("local_ms_pool_state_file", ""),
        graph_scope=extra.get("local_ms_graph_scope", ""),
        allow_reuse=str(extra.get("local_ms_pool_allow_reuse", "")).strip().lower() in {"1", "true", "yes", "on"},
        proxy=proxy,
    )


def _create_laoudo(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return LaoudoMailbox(
        auth_token=extra.get("laoudo_auth", ""),
        email=extra.get("laoudo_email", ""),
        account_id=extra.get("laoudo_account_id", ""),
    )


def _create_generic_http(extra: dict, proxy: str | None, *, pipeline_config: dict | None = None) -> 'BaseMailbox':
    from core.generic_http_mailbox import GenericHttpMailbox
    return GenericHttpMailbox(
        pipeline_config=pipeline_config or {},
        settings=extra,
        proxy=proxy,
    )


MAILBOX_FACTORY_REGISTRY = {
    "generic_http_mailbox": _create_generic_http,
    "tempmail_lol_api": _create_tempmail,
    "tempmail_web_api": _create_tempmail_web,
    "smsbower_mail_api": _create_smsbower_mail,
    "duckmail_api": _create_duckmail,
    "ddg_email": _create_ddg_email,
    "ddg_email_api": _create_ddg_email,
    "freemail_api": _create_freemail,
    "moemail_api": _create_moemail,
    "cfworker_admin_api": _create_cfworker,
    "testmail_api": _create_testmail,
    "outlook_email_api": _create_outlook_email,
    "local_ms_pool": _create_local_ms_pool,
    "laoudo_api": _create_laoudo,
    # backward-compat fallback
    "generic_http": _create_generic_http,
    "tempmail_lol": _create_tempmail,
    "tempmail_web": _create_tempmail_web,
    "smsbower_mail": _create_smsbower_mail,
    "duckmail": _create_duckmail,
    "freemail": _create_freemail,
    "moemail": _create_moemail,
    "cfworker": _create_cfworker,
    "testmail": _create_testmail,
    "outlook_email": _create_outlook_email,
    "local_ms": _create_local_ms_pool,
    "laoudo": _create_laoudo,
}


def create_mailbox(provider: str, extra: dict = None, proxy: str = None) -> 'BaseMailbox':
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    definitions_repo = ProviderDefinitionsRepository()
    settings_repo = ProviderSettingsRepository()
    provider_key = str(provider or "").strip()
    if not provider_key:
        raise RuntimeError("未选择邮箱 provider，请先在设置页配置并启用默认邮箱 provider")
    definition = definitions_repo.get_by_key("mailbox", provider_key)
    if not definition or not definition.enabled:
        raise RuntimeError(f"邮箱 provider 不存在或未启用: {provider_key}")
    base_extra = dict(extra or {})

    raw_fallbacks = base_extra.get("mail_provider_fallbacks")
    explicit_fallbacks: list[str] = []
    if isinstance(raw_fallbacks, str):
        explicit_fallbacks = [item.strip() for item in raw_fallbacks.split(",") if item.strip()]
    elif isinstance(raw_fallbacks, (list, tuple, set)):
        explicit_fallbacks = [str(item or "").strip() for item in raw_fallbacks if str(item or "").strip()]

    enabled_items = settings_repo.list_enabled("mailbox")
    enabled_keys = [str(item.provider_key or "").strip() for item in enabled_items if str(item.provider_key or "").strip()]
    ordered_keys: list[str] = [provider_key]
    for key in explicit_fallbacks:
        if key not in ordered_keys:
            ordered_keys.append(key)
    for key in enabled_keys:
        if key == provider_key or key == "laoudo" or key in ordered_keys:
            continue
        ordered_keys.append(key)

    providers: list[tuple[str, BaseMailbox]] = []
    for key in ordered_keys:
        current_definition = definitions_repo.get_by_key("mailbox", key)
        if not current_definition or not current_definition.enabled:
            continue
        resolved_extra = settings_repo.resolve_runtime_settings("mailbox", key, base_extra)
        lookup_key = current_definition.driver_type if current_definition else key
        factory = MAILBOX_FACTORY_REGISTRY.get(lookup_key)
        if not factory:
            continue
        try:
            if lookup_key in ("generic_http_mailbox", "generic_http"):
                pipeline_config = current_definition.get_metadata() if current_definition else {}
                providers.append((key, factory(resolved_extra, proxy, pipeline_config=pipeline_config)))
            else:
                providers.append((key, factory(resolved_extra, proxy)))
        except Exception as exc:
            if key == provider_key:
                raise RuntimeError(f"邮箱 provider {key} 初始化失败: {exc}") from exc
            logger.warning("邮箱 provider %s 初始化失败，已跳过: %s", key, exc)

    if not providers:
        raise RuntimeError("没有可用的邮箱 provider 实例")
    if len(providers) == 1:
        return providers[0][1]
    return FallbackMailbox(providers)


class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""
    def __init__(self, auth_token: str, email: str, account_id: str, api_url: str = ""):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = (api_url or DEFAULT_LAOUDO_API_URL).rstrip("/")
        self._ua = "Mozilla/5.0"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(
            email=self._email,
            account_id=self._account_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "laoudo",
                    "login_identifier": self._email,
                    "display_name": self._email,
                    "credentials": {
                        "authorization": self.auth,
                    },
                    "metadata": {
                        "account_id": self._account_id,
                        "email": self._email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "laoudo",
                    "resource_type": "mailbox",
                    "resource_identifier": self._account_id,
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "account_id": self._account_id,
                        "email": self._email,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests
        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={"accountId": account.account_id, "allReceive": 0,
                        "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15, impersonate="chrome131"
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {m.get("id") or m.get("emailId") for m in mails if m.get("id") or m.get("emailId")}
        except Exception:
            pass
        return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "trae",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        from curl_cffi import requests as curl_requests
        seen = set(before_ids) if before_ids else set()
        start = time.time()
        h = {"authorization": self.auth, "user-agent": self._ua}
        while time.time() - start < timeout:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={"accountId": account.account_id, "allReceive": 0,
                            "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                    headers=h, timeout=15, impersonate="chrome131"
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (str(mail.get("subject", "")) + " " +
                                str(mail.get("content") or mail.get("html") or ""))
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        from curl_cffi import requests as curl_requests
        seen = set(before_ids or [])
        start = time.time()
        headers = {"authorization": self.auth, "user-agent": self._ua}
        while time.time() - start < timeout:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={"accountId": account.account_id, "allReceive": 0,
                            "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                    headers=headers, timeout=15, impersonate="chrome131"
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (str(mail.get("subject", "")) + " " +
                                str(mail.get("content") or mail.get("html") or ""))
                        link = _extract_verification_link(text, keyword)
                        if link:
                            return link
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""
    def __init__(self, email: str, api_url: str = ""):
        self._email = email
        self.api = (api_url or DEFAULT_AITRE_API_URL).rstrip("/")

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "trae",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids) if before_ids else set()
        last_check = None
        start = time.time()
        while time.time() - start < timeout:
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        last_check = None
        start = time.time()
        while time.time() - start < timeout:
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = str(mail.get("preview", "")) + " " + str(mail.get("content", ""))
                        link = _extract_verification_link(text, keyword)
                        if link:
                            return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""

    def __init__(self, proxy: str = None, api_url: str = ""):
        self.api = (api_url or DEFAULT_TEMPMAIL_LOL_API_URL).rstrip("/")
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._email = None

    def get_email(self) -> MailboxAccount:
        import requests
        r = requests.post(f"{self.api}/inbox/create",
            json={},
            proxies=self.proxy, timeout=15)
        data = r.json()
        self._email = data.get("address") or data.get("email", "")
        self._token = data.get("token", "")
        return MailboxAccount(
            email=self._email,
            account_id=self._token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "tempmail_lol",
                    "resource_type": "mailbox",
                    "resource_identifier": self._token,
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "email": self._email,
                        "token": self._token,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy, timeout=10)
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids or [])
        start = time.time()
        new_mail_count = 0
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy, timeout=10)
                emails = r.json().get("emails", [])
                for mail in sorted(emails, key=lambda x: x.get("date", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    new_mail_count += 1
                    text = mail.get("subject", "") + " " + mail.get("body", "") + " " + mail.get("html", "")
                    print(f"[TempMailLol] 新邮件 #{new_mail_count} id={mid} subject={str(mail.get('subject', ''))[:80]}")
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                    if m:
                        code_val = m.group(1) if m.groups() else m.group(0)
                        print(f"[TempMailLol] 匹配到验证码: {code_val}")
                        return code_val
                    print(f"[TempMailLol] 邮件未匹配验证码 text[:150]={text[:150]}")
            except Exception:
                pass
            time.sleep(3)
        print(f"[TempMailLol] 轮询超时 ({timeout}s)，共收到 {new_mail_count} 封新邮件")
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy, timeout=10)
                for mail in sorted(r.json().get("emails", []), key=lambda x: x.get("date", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    text = str(mail.get("subject", "")) + " " + str(mail.get("body", "")) + " " + str(mail.get("html", ""))
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class SmsBowerMailMailbox(BaseMailbox):
    """SMSBower 临时邮箱 API provider."""

    def __init__(
        self,
        api_key: str,
        *,
        service: str = "",
        domain: str = "",
        max_price: str | float | int = "",
        alias: str | bool = "",
        reuse_limit: str | int = "",
        activation_limit: str | int = "",
        alias_prefix: str = "",
        manual_activations: str | list | tuple = "",
        activation_wait_seconds: str | int | float = "",
        activation_poll_interval: str | int | float = "",
        api_url: str = "",
        cancel_check=None,
        proxy: str = None,
    ):
        import threading

        self.api_key = str(api_key or "").strip()
        self.api = _normalize_api_base_url(
            api_url,
            default=DEFAULT_SMSBOWER_API_URL,
            label="SMSBower Mail API",
        )
        self.service = str(service or "go").strip()
        self.domain = str(domain or "gmail.com").strip()
        self.max_price = str(max_price or "").strip()
        alias_raw = alias if isinstance(alias, bool) else str(alias or "").strip().lower()
        self.alias = "1" if alias_raw in (True, "1", "true", "yes", "on", "是") else ""
        try:
            parsed_reuse_limit = int(str(reuse_limit or "").strip() or "0")
        except ValueError:
            parsed_reuse_limit = 0
        self.reuse_limit = max(parsed_reuse_limit or (5 if self.alias else 1), 1)
        try:
            parsed_activation_limit = int(str(activation_limit or "").strip() or "0")
        except ValueError:
            parsed_activation_limit = 0
        self.activation_limit = max(parsed_activation_limit, 0)
        self.alias_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "", str(alias_prefix or "gpt").strip()) or "gpt"
        self.activation_wait_seconds = max(self._parse_float(activation_wait_seconds, 0.0), 0.0)
        self.activation_poll_interval = max(self._parse_float(activation_poll_interval, 3.0), 0.5)
        self.cancel_check = cancel_check if callable(cancel_check) else None
        self._manual_activation_raw = manual_activations
        self._manual_activation_seed = self._parse_manual_activations(manual_activations)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._released_mail_ids: set[str] = set()
        self._lock = threading.RLock()
        self._active_activation: dict | None = None
        self._activations: dict[str, dict] = {}
        self._mail_locks: dict[str, threading.RLock] = {}
        self._created_activation_count = 0

    @staticmethod
    def _ok(value) -> bool:
        return value in (1, "1", True, "success", "SUCCESS", "ok", "OK")

    @staticmethod
    def _parse_float(value, default: float) -> float:
        try:
            raw = str(value or "").strip()
            return float(raw) if raw else float(default)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _parse_manual_activations(value) -> list[dict]:
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple, set)):
            lines = list(value)
        else:
            lines = []

        activations: list[dict] = []
        for line in lines:
            raw = str(line or "").strip()
            if not raw:
                continue
            if "----" in raw:
                email, mail_id = raw.split("----", 1)
            elif "|" in raw:
                email, mail_id = raw.split("|", 1)
            elif "," in raw:
                email, mail_id = raw.split(",", 1)
            else:
                continue
            email = email.strip()
            mail_id = mail_id.strip()
            if email and mail_id:
                activations.append({"base_email": email, "mail_id": mail_id})
        return activations

    @staticmethod
    def _has_manual_activation_input(value) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set)):
            return any(str(item or "").strip() for item in value)
        return False

    def _request(self, path: str, params: dict, *, timeout: int = 20) -> dict:
        import requests

        if not self.api_key:
            raise RuntimeError("SMSBower Mail 未配置 API Key")
        payload = {"api_key": self.api_key, **params}
        resp = requests.get(
            f"{self.api}{path}",
            params=payload,
            timeout=timeout,
            proxies=self.proxy,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"SMSBower Mail 返回非 JSON: {resp.text[:200]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"SMSBower Mail 返回格式异常: {str(data)[:200]}")
        return data

    def _raise_if_cancelled(self) -> None:
        if callable(self.cancel_check) and self.cancel_check():
            raise RuntimeError("任务已取消")

    def _set_status(self, mail_id: str, status: int) -> bool:
        if not mail_id:
            return False
        try:
            data = self._request(
                "/api/mail/setStatus",
                {"id": str(mail_id), "status": int(status)},
                timeout=15,
            )
            return self._ok(data.get("status"))
        except Exception:
            return False

    def _mail_lock(self, mail_id: str):
        with self._lock:
            lock = self._mail_locks.get(mail_id)
            if lock is None:
                import threading

                lock = threading.RLock()
                self._mail_locks[mail_id] = lock
            return lock

    @staticmethod
    def _gmail_alias(base_email: str, tag: str) -> str:
        local, sep, domain = str(base_email or "").strip().partition("@")
        if not sep:
            return base_email
        local = local.split("+", 1)[0]
        return f"{local}+{tag}@{domain}"

    def _build_account_from_activation(self, activation: dict, alias_index: int) -> MailboxAccount:
        base_email = str(activation.get("base_email") or "").strip()
        mail_id = str(activation.get("mail_id") or "").strip()
        email = base_email
        if self.alias and self.reuse_limit > 1:
            email = self._gmail_alias(base_email, f"{self.alias_prefix}{alias_index:02d}")
        return MailboxAccount(
            email=email,
            account_id=mail_id,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "smsbower_mail",
                    "resource_type": "mailbox",
                    "resource_identifier": mail_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "base_email": base_email,
                        "mail_id": mail_id,
                        "service": self.service,
                        "domain": self.domain,
                        "alias_index": alias_index,
                        "reuse_limit": self.reuse_limit,
                    },
                },
            },
        )

    def _register_activation(self, mail: str, mail_id: str) -> MailboxAccount:
        with self._lock:
            activation = {
                "base_email": str(mail or "").strip(),
                "mail_id": str(mail_id or "").strip(),
                "assigned": 1,
                "completed": 0,
                "reuse_limit": self.reuse_limit,
                "closed": False,
            }
            self._activations[activation["mail_id"]] = activation
            self._active_activation = activation if self.reuse_limit > 1 else None
            self._created_activation_count += 1
            return self._build_account_from_activation(activation, 1)

    def _mark_code_accepted(self, mail_id: str) -> None:
        with self._lock:
            activation = self._activations.get(mail_id)
            if not activation:
                status = 3
            else:
                activation["completed"] = int(activation.get("completed") or 0) + 1
                completed = int(activation.get("completed") or 0)
                limit = int(activation.get("reuse_limit") or self.reuse_limit)
                status = 3 if completed >= limit else 5
                if status == 3:
                    activation["closed"] = True
                    if self._active_activation is activation:
                        self._active_activation = None
        ok = self._set_status(mail_id, status)
        if status == 5 and not ok:
            with self._lock:
                activation = self._activations.get(mail_id)
                if activation:
                    activation["closed"] = True
                    activation["next_code_failed"] = True
                    if self._active_activation is activation:
                        self._active_activation = None

    def _cancel_activation(self, mail_id: str) -> None:
        with self._lock:
            activation = self._activations.get(mail_id)
            if activation:
                activation["closed"] = True
                if self._active_activation is activation:
                    self._active_activation = None
        self._set_status(mail_id, 2)

    def get_email(self) -> MailboxAccount:
        with self._lock:
            activation = self._active_activation
            if (
                activation
                and not activation.get("closed")
                and int(activation.get("assigned") or 0) < int(activation.get("reuse_limit") or self.reuse_limit)
                and int(activation.get("assigned") or 0) <= int(activation.get("completed") or 0)
            ):
                activation["assigned"] = int(activation.get("assigned") or 0) + 1
                return self._build_account_from_activation(activation, int(activation["assigned"]))
            if self.activation_limit and self._created_activation_count >= self.activation_limit:
                raise RuntimeError(
                    f"SMSBower Mail activation 已达本任务上限: "
                    f"{self._created_activation_count}/{self.activation_limit}"
                )
            if self._manual_activation_seed:
                manual_activation = self._manual_activation_seed.pop(0)
                return self._register_activation(
                    manual_activation["base_email"],
                    manual_activation["mail_id"],
                )
            if self._has_manual_activation_input(self._manual_activation_raw):
                raise RuntimeError("SMSBower Mail 手动邮箱 activation 格式错误，请填写: email@gmail.com----mailId")

        params = {
            "service": self.service,
            "domain": self.domain,
        }
        if self.max_price:
            params["maxPrice"] = self.max_price
        if self.alias:
            params["alias"] = self.alias

        import time

        deadline = time.time() + self.activation_wait_seconds if self.activation_wait_seconds > 0 else None
        last_error: object = None
        while True:
            self._raise_if_cancelled()
            try:
                data = self._request("/api/mail/getActivation", params)
            except Exception as exc:
                last_error = exc
            else:
                mail = str(data.get("mail") or data.get("email") or "").strip()
                mail_id = str(data.get("mailId") or data.get("id") or "").strip()
                if self._ok(data.get("status")) and mail and mail_id:
                    return self._register_activation(mail, mail_id)
                last_error = data

            if deadline is not None and time.time() >= deadline:
                break
            sleep_until = time.time() + self.activation_poll_interval
            while time.time() < sleep_until:
                self._raise_if_cancelled()
                time.sleep(min(0.5, max(sleep_until - time.time(), 0)))

        raise RuntimeError(f"SMSBower Mail 获取邮箱失败: {str(last_error)[:300]}")

    def get_current_ids(self, account: MailboxAccount) -> set:
        mail_id = str(getattr(account, "account_id", "") or "").strip()
        return {mail_id} if mail_id else set()

    def release(self, account: MailboxAccount, reason: str = "") -> bool:
        mail_id = str(getattr(account, "account_id", "") or "").strip()
        if not mail_id:
            return False
        if mail_id in self._released_mail_ids:
            return True
        self._cancel_activation(mail_id)
        ok = True
        if ok:
            self._released_mail_ids.add(mail_id)
        return ok

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        import re
        import time

        mail_id = str(getattr(account, "account_id", "") or "").strip()
        if not mail_id:
            raise RuntimeError("SMSBower Mail 缺少 mailId")

        lock = self._mail_lock(mail_id)
        with lock:
            pattern = code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)'
            deadline = time.time() + max(int(timeout or 0), 0)
            while time.time() < deadline:
                try:
                    data = self._request("/api/mail/getCode", {"mailId": mail_id}, timeout=15)
                except Exception:
                    time.sleep(3)
                    continue
                code_text = str(data.get("code") or data.get("sms") or data.get("text") or "").strip()
                if self._ok(data.get("status")) and code_text:
                    if keyword and keyword.lower() not in code_text.lower():
                        time.sleep(3)
                        continue
                    match = re.search(pattern, code_text)
                    code = match.group(1) if match and match.groups() else (match.group(0) if match else code_text)
                    self._mark_code_accepted(mail_id)
                    return code
                message = str(data.get("message") or data.get("error") or data).lower()
                if "maximum number of codes" in message or "max" in message and "code" in message:
                    with self._lock:
                        activation = self._activations.get(mail_id)
                        if activation:
                            activation["closed"] = True
                            activation["max_codes_reached"] = True
                            if self._active_activation is activation:
                                self._active_activation = None
                    raise RuntimeError(f"SMSBower Mail 已达到继续取码上限: {str(data)[:300]}")
                time.sleep(3)

        self._cancel_activation(mail_id)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        import time

        mail_id = str(getattr(account, "account_id", "") or "").strip()
        if not mail_id:
            raise RuntimeError("SMSBower Mail 缺少 mailId")

        lock = self._mail_lock(mail_id)
        with lock:
            deadline = time.time() + max(int(timeout or 0), 0)
            while time.time() < deadline:
                try:
                    data = self._request("/api/mail/getCode", {"mailId": mail_id}, timeout=15)
                except Exception:
                    time.sleep(3)
                    continue
                text = str(data.get("code") or data.get("sms") or data.get("text") or "").strip()
                if self._ok(data.get("status")) and text:
                    link = _extract_verification_link(text, keyword)
                    if link:
                        self._mark_code_accepted(mail_id)
                        return link
                message = str(data.get("message") or data.get("error") or data).lower()
                if "maximum number of codes" in message or "max" in message and "code" in message:
                    with self._lock:
                        activation = self._activations.get(mail_id)
                        if activation:
                            activation["closed"] = True
                            activation["max_codes_reached"] = True
                            if self._active_activation is activation:
                                self._active_activation = None
                    raise RuntimeError(f"SMSBower Mail 已达到继续取码上限: {str(data)[:300]}")
                time.sleep(3)

        self._cancel_activation(mail_id)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TempMailWebMailbox(BaseMailbox):
    """参考项目同款 Temp-Mail Web API。"""

    def __init__(self, base_url: str = "", proxy: str = None):
        self.base_url = _normalize_api_base_url(
            base_url,
            default=DEFAULT_TEMPMAIL_WEB_BASE_URL,
            label="Temp-Mail Web URL",
        )
        self.proxy = str(proxy or "").strip()
        self._accounts: dict[str, str] = {}
        self._executor = None
        self._browser = None
        self._page = None

    def _ensure_browser(self):
        if self._page is not None:
            return self._page
        from camoufox.sync_api import Camoufox

        launch_opts = {"headless": True}
        if self.proxy:
            parsed = urlparse(self.proxy)
            if parsed.scheme and parsed.hostname and parsed.port:
                proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
                if parsed.username:
                    proxy_config["username"] = parsed.username
                if parsed.password:
                    proxy_config["password"] = parsed.password
                launch_opts["proxy"] = proxy_config
            else:
                launch_opts["proxy"] = {"server": self.proxy}
            launch_opts["geoip"] = True
        self._browser = Camoufox(**launch_opts)
        browser = self._browser.__enter__()
        self._page = browser.new_page()
        self._page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
        return self._page

    def _run_in_browser_thread(self, fn):
        from concurrent.futures import ThreadPoolExecutor

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tempmail-web")
        future = self._executor.submit(fn)
        return future.result()

    @staticmethod
    def _decode_json_response(response: dict, action: str):
        import json

        status = int((response or {}).get("status", 0) or 0)
        text = str((response or {}).get("body", "") or "")
        if status != 200:
            raise RuntimeError(
                f"Temp-Mail Web {action} 失败: HTTP {status} {text[:300]}"
            )
        try:
            return json.loads(text)
        except Exception as exc:
            raise RuntimeError(
                f"Temp-Mail Web {action} 返回非 JSON: {exc}; body={text[:300]}"
            ) from exc

    def _request_json(self, method: str, path: str, *, auth_header: str = "") -> dict | list:
        import random
        import time

        target_url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        action = "创建邮箱" if path.lstrip("/") == "mailbox" else "拉取消息"
        max_attempts = 4 if path.lstrip("/") == "mailbox" else 2

        for attempt in range(1, max_attempts + 1):
            def _browser_call():
                page = self._ensure_browser()
                return page.evaluate(
                    """
                    async ({ targetUrl, method, authHeader, baseUrl }) => {
                      try {
                        const response = await fetch(targetUrl, {
                          method,
                          credentials: 'include',
                          referrer: baseUrl,
                          headers: {
                            'Accept': 'application/json',
                            ...(method === 'GET' ? { 'Cache-Control': 'no-cache' } : {}),
                            ...(authHeader ? { 'Authorization': authHeader } : {}),
                          },
                          ...(method === 'POST' ? { body: '{}' } : {}),
                        });
                        return {
                          status: response.status,
                          body: await response.text(),
                        };
                      } catch (error) {
                        return {
                          status: 0,
                          body: error instanceof Error ? error.message : String(error),
                        };
                      }
                    }
                    """,
                    {
                        "targetUrl": target_url,
                        "method": method,
                        "authHeader": auth_header,
                        "baseUrl": self.base_url,
                    },
                )

            result = self._run_in_browser_thread(_browser_call)
            status = int((result or {}).get("status", 0) or 0)
            if status != 429 or attempt >= max_attempts:
                return self._decode_json_response(result, action)
            wait_seconds = min(20, 3 * attempt + random.uniform(0.5, 2.5))
            print(f"[TempMailWeb] {action} 遇到 429，等待 {wait_seconds:.1f}s 后重试 ({attempt}/{max_attempts})")
            time.sleep(wait_seconds)

        return self._decode_json_response(result, action)

    def get_email(self) -> MailboxAccount:
        import json

        data = self._request_json("POST", "/mailbox")
        address = str(data.get("address") or data.get("mailbox") or data.get("email") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError(f"Temp-Mail Web 创建邮箱失败: {json.dumps(data, ensure_ascii=False)[:300]}")
        self._accounts[address] = token
        print(f"[TempMailWeb] 生成邮箱: {address}")
        return MailboxAccount(
            email=address,
            account_id=token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "tempmail_web",
                    "resource_type": "mailbox",
                    "resource_identifier": token,
                    "handle": address,
                    "display_name": address,
                    "metadata": {
                        "email": address,
                        "token": token,
                        "base_url": self.base_url,
                    },
                },
            },
        )

    def _fetch_messages(self, account: MailboxAccount) -> list[dict]:
        token = str(account.account_id or self._accounts.get(account.email) or "").strip()
        if not token:
            raise RuntimeError(f"Temp-Mail Web 缺少 token: {account.email}")
        data = self._request_json("GET", "/messages", auth_header=f"Bearer {token}")
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return list(data.get("messages") or [])
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _message_id(message: dict) -> str:
        return str(
            message.get("id")
            or message.get("_id")
            or f"{message.get('createdAt', '')}:{message.get('subject', '')}"
        )

    @staticmethod
    def _extract_code(message: dict, code_pattern: str | None = None) -> str:
        subject = str(message.get("subject") or "").strip()
        if subject:
            last_token = subject.split()[-1]
            if re.fullmatch(r"\d{6}", last_token):
                return last_token
        text = " ".join(
            str(message.get(key) or "")
            for key in ("subject", "body", "text", "content", "html")
        )
        match = re.search(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)", text)
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                self._message_id(item)
                for item in self._fetch_messages(account)
                if self._message_id(item)
            }
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import time

        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = self._fetch_messages(account)
                for item in messages:
                    mid = self._message_id(item)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(item.get(key) or "")
                        for key in ("subject", "body", "text", "content", "html")
                    )
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._extract_code(item, code_pattern=code_pattern)
                    if code:
                        print(f"[TempMailWeb] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            time.sleep(5)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time

        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = self._fetch_messages(account)
                for item in messages:
                    mid = self._message_id(item)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(item.get(key) or "")
                        for key in ("subject", "body", "text", "content", "html")
                    )
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(5)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")

    def __del__(self):
        executor = getattr(self, "_executor", None)
        browser = getattr(self, "_browser", None)
        if executor is not None and browser is not None:
            try:
                executor.submit(browser.__exit__, None, None, None).result(timeout=5)
            except Exception:
                pass
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass


class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""

    def __init__(self, api_url: str = "",
                 provider_url: str = "",
                 bearer: str = "",
                 proxy: str = None):
        self.api = api_url.rstrip("/")
        self.provider_url = provider_url
        self.bearer = bearer
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._address = None

    def _common_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }

    def get_email(self) -> MailboxAccount:
        import requests, random, string
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.provider_url.replace("https://api.", "").replace("https://", "")
        address = f"{username}@{domain}"
        # 创建账号
        r = insecure_request(requests.post, f"{self.api}/api/mail?endpoint=%2Faccounts",
            json={"address": address, "password": password},
            headers=self._common_headers(), proxies=self.proxy, timeout=15)
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = insecure_request(requests.post, f"{self.api}/api/mail?endpoint=%2Ftoken",
            json={"address": self._address, "password": password},
            headers=self._common_headers(), proxies=self.proxy, timeout=15)
        self._token = r2.json().get("token", "")
        return MailboxAccount(
            email=self._address,
            account_id=self._token,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "duckmail",
                    "login_identifier": self._address,
                    "display_name": self._address,
                    "credentials": {
                        "address": self._address,
                        "password": password,
                        "token": self._token,
                    },
                    "metadata": {
                        "provider_url": self.provider_url,
                        "api_url": self.api,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "duckmail",
                    "resource_type": "mailbox",
                    "resource_identifier": self._token,
                    "handle": self._address,
                    "display_name": self._address,
                    "metadata": {
                        "email": self._address,
                        "provider_url": self.provider_url,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                headers={"authorization": f"Bearer {account.account_id}",
                         "x-api-provider-base-url": self.provider_url},
                proxies=self.proxy, timeout=10)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                    headers={"authorization": f"Bearer {account.account_id}",
                             "x-api-provider-base-url": self.provider_url},
                    proxies=self.proxy, timeout=10)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen: continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%2F{mid}",
                            headers={"authorization": f"Bearer {account.account_id}",
                                     "x-api-provider-base-url": self.provider_url},
                            proxies=self.proxy, timeout=10)
                        detail = r2.json()
                        body = str(detail.get("text") or "") + " " + str(detail.get("subject") or "")
                    except Exception:
                        body = str(msg.get("subject") or "")
                    body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
                    m = re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)", body)
                    if m: return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                    headers={"authorization": f"Bearer {account.account_id}",
                             "x-api-provider-base-url": self.provider_url},
                    proxies=self.proxy, timeout=10)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    try:
                        r2 = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%2F{mid}",
                            headers={"authorization": f"Bearer {account.account_id}",
                                     "x-api-provider-base-url": self.provider_url},
                            proxies=self.proxy, timeout=10)
                        detail = r2.json()
                        body = str(detail.get("text") or "") + " " + str(detail.get("html") or "") + " " + str(detail.get("subject") or "")
                    except Exception:
                        body = str(msg.get("subject") or "")
                    link = _extract_verification_link(body, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""

    def __init__(self, api_url: str, admin_token: str = "", domain: str = "",
                 fingerprint: str = "", proxy: str = None):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = domain
        self.fingerprint = fingerprint
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._api_mode = "auto"

    def _headers(self) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        return h

    def _cloud_mail_headers(self) -> dict:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "Authorization": self.admin_token,
        }

    def _cloud_mail_domain(self) -> str:
        return str(self.domain or "").strip().lstrip("@")

    def _validate_cloud_mail_domain(self, config: dict) -> None:
        domain = self._cloud_mail_domain()
        raw_domains = config.get("domainList") if isinstance(config, dict) else []
        domains = {
            str(item or "").strip().lstrip("@")
            for item in (raw_domains or [])
            if str(item or "").strip()
        }
        if domain and domains and domain not in domains:
            raise RuntimeError(f"Cloud Mail 未启用邮箱域名 {domain}，当前可用域名: {', '.join(sorted(domains))}")

    def _detect_api_mode(self) -> str:
        if self._api_mode in {"cfworker", "cloud_mail"}:
            return self._api_mode

        import requests

        data = {}
        config = None
        try:
            r = requests.get(
                f"{self.api}/api/setting/websiteConfig",
                headers={"accept": "application/json, text/plain, */*"},
                proxies=self.proxy,
                timeout=5,
            )
            if getattr(r, "status_code", 200) >= 400:
                self._api_mode = "cfworker"
                return self._api_mode
            try:
                data = r.json()
            except Exception:
                self._api_mode = "cfworker"
                return self._api_mode
            data = data if isinstance(data, dict) else {}
            config = data.get("data")
        except Exception:
            self._api_mode = "cfworker"
            return self._api_mode

        if data.get("code") == 200 and isinstance(config, dict):
            self._validate_cloud_mail_domain(config)
            self._api_mode = "cloud_mail"
        else:
            self._api_mode = "cfworker"
        return self._api_mode

    def _json_or_error(self, response, label: str) -> dict:
        try:
            data = response.json()
        except Exception as exc:
            body = str(getattr(response, "text", "") or "")[:200]
            status = getattr(response, "status_code", "?")
            raise RuntimeError(f"{label} 未返回 JSON，status={status}, resp={body!r}") from exc
        return data if isinstance(data, dict) else {"data": data}

    def _make_cloud_mail_account(self, name: str, legacy_error: Exception | None = None) -> MailboxAccount:
        import requests

        domain = self._cloud_mail_domain()
        if not domain:
            if legacy_error:
                raise legacy_error
            raise RuntimeError("Cloud Mail 未配置邮箱域名")
        if not self.admin_token:
            raise RuntimeError("Cloud Mail 需要填写开放 API Token（由 /api/public/genToken 生成）")

        email = f"{name}@{domain}"
        r = requests.post(
            f"{self.api}/api/public/addUser",
            json={"list": [{"email": email}]},
            headers=self._cloud_mail_headers(),
            proxies=self.proxy,
            timeout=15,
        )
        data = self._json_or_error(r, "Cloud Mail addUser")
        code = data.get("code")
        if getattr(r, "status_code", 200) >= 400 or (code not in (None, 200)):
            raise RuntimeError(
                f"Cloud Mail addUser 失败: status={getattr(r, 'status_code', '?')}, resp={str(data)[:200]}"
            )

        self._api_mode = "cloud_mail"
        self._token = email
        print(f"[CFWorker] Cloud Mail 生成邮箱: {email}")
        # **预热默认关闭**：以前默认 sleep 8s 让 Cloudflare worker 同步路由——实测
        # ChatGPT Plus 注册场景下 worker 同步速度足够快，8s 是纯浪费。如未来发现
        # 路由同步延迟可通过环境变量重新打开：``CLOUD_MAIL_WARMUP_SECONDS=8``。
        try:
            import os as _os, time as _time
            warmup = float(_os.environ.get("CLOUD_MAIL_WARMUP_SECONDS", "0") or 0)
            if warmup > 0:
                print(f"[CFWorker] Cloud Mail 邮箱预热等待 {warmup:.0f}s（让 worker 同步路由后再继续）")
                _time.sleep(warmup)
        except Exception as _warmup_exc:
            print(f"[CFWorker] 邮箱预热被跳过: {_warmup_exc}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cloud_mail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "api_url": self.api,
                        "domain": domain,
                        "api_mode": "cloud_mail",
                    },
                },
            },
        )

    def _normalize_cloud_mail_mails(self, data: dict) -> list:
        items = data.get("data", data)
        if isinstance(items, dict):
            items = items.get("list", [])
        if not isinstance(items, list):
            return []

        normalized = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = item.get("emailId", item.get("id", ""))
            raw = " ".join(
                str(item.get(field) or "")
                for field in ("subject", "sendEmail", "sendName", "content", "text")
            )
            normalized.append({**item, "id": mid, "raw": raw})
        return normalized

    def _get_cloud_mail_mails(self, email: str) -> list:
        import requests

        if not self.admin_token:
            raise RuntimeError("Cloud Mail 需要填写开放 API Token（由 /api/public/genToken 生成）")

        queries = [email]
        fuzzy_email = f"%{email}%"
        if fuzzy_email not in queries:
            queries.append(fuzzy_email)

        for index, to_email in enumerate(queries):
            # 不传 type / isDel 过滤；某些 cloud_mail 后端会把 OpenAI OTP 自动归到
            # spam (type!=0) 或软删除 (isDel=1)，再传 0 反而过滤掉了我们要找的邮件。
            r = requests.post(
                f"{self.api}/api/public/emailList",
                json={
                    "toEmail": to_email,
                    "timeSort": "desc",
                    "num": 1,
                    "size": 50,
                },
                headers=self._cloud_mail_headers(),
                proxies=self.proxy,
                timeout=10,
            )
            data = self._json_or_error(r, "Cloud Mail emailList")
            if getattr(r, "status_code", 200) >= 400 or data.get("code") not in (None, 200):
                raise RuntimeError(
                    f"Cloud Mail emailList 失败: status={getattr(r, 'status_code', '?')}, resp={str(data)[:200]}"
                )
            self._api_mode = "cloud_mail"
            mails = self._normalize_cloud_mail_mails(data)
            if mails or index == len(queries) - 1:
                return mails
        return []

    def get_email(self) -> MailboxAccount:
        import requests, random, string
        name = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        if self._detect_api_mode() == "cloud_mail":
            return self._make_cloud_mail_account(name)
        payload = {"enablePrefix": True, "name": name}
        if self.domain:
            payload["domain"] = self.domain
        r = requests.post(f"{self.api}/admin/new_address",
            json=payload, headers=self._headers(),
            proxies=self.proxy, timeout=15)
        print(f"[CFWorker] new_address status={r.status_code} resp={r.text[:200]}")
        try:
            data = r.json()
        except Exception as exc:
            return self._make_cloud_mail_account(name, exc)
        email = data.get("email", data.get("address", ""))
        token = data.get("token", data.get("jwt", ""))
        if not email:
            return self._make_cloud_mail_account(name)
        self._api_mode = "cfworker"
        self._token = token
        print(f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}...")
        return MailboxAccount(
            email=email,
            account_id=token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cfworker",
                    "resource_type": "mailbox",
                    "resource_identifier": token or email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "token": token,
                        "api_url": self.api,
                        "domain": self.domain,
                    },
                },
            },
        )

    def _get_mails(self, email: str) -> list:
        import requests
        if self._detect_api_mode() == "cloud_mail":
            return self._get_cloud_mail_mails(email)
        try:
            r = requests.get(f"{self.api}/admin/mails",
                params={"limit": 20, "offset": 0, "address": email},
                headers=self._headers(), proxies=self.proxy, timeout=10)
            data = r.json()
            return data.get("results", data) if isinstance(data, dict) else data
        except Exception:
            return self._get_cloud_mail_mails(email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._get_mails(account.email)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        new_mail_count = 0
        while time.time() - start < timeout:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    new_mail_count += 1
                    raw = str(mail.get("raw", ""))
                    subject = str(mail.get("subject", "") or "")
                    print(f"[CFWorker] 新邮件 #{new_mail_count} id={mid} subject={subject[:80]}")
                    # 1. 优先匹配 <span>XXXXXX</span> （Trae 邮件格式）
                    code_m = re.search(r'<span[^>]*>\s*(\d{6})\s*</span>', raw)
                    if code_m:
                        print(f"[CFWorker] 匹配到验证码 (span): {code_m.group(1)}")
                        return code_m.group(1)
                    # 2. 跳过 MIME header，只搜 body 部分，避免匹配时间戳
                    body_start = raw.find('\r\n\r\n')
                    search_text = raw[body_start:] if body_start != -1 else raw
                    search_text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', search_text)
                    # 排除时间戳模式 m=+XXXXXX. 和 t=XXXXXXXXXX
                    search_text = re.sub(r'm=\+\d+\.\d+', '', search_text)
                    search_text = re.sub(r'\bt=\d+\b', '', search_text)
                    m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', search_text)
                    if m:
                        code_val = m.group(1) if m.groups() else m.group(0)
                        print(f"[CFWorker] 匹配到验证码 (regex): {code_val}")
                        return code_val
                    # 没有匹配到验证码，打印 raw 前 150 字符帮助诊断
                    print(f"[CFWorker] 邮件未匹配验证码 raw[:150]={raw[:150]}")
            except Exception:
                pass
            time.sleep(3)
        print(f"[CFWorker] 轮询超时 ({timeout}s)，共收到 {new_mail_count} 封新邮件")
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    raw = str(mail.get("raw", ""))
                    link = _extract_verification_link(raw, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""

    def __init__(
        self,
        api_url: str = "",
        username: str = "",
        password: str = "",
        session_token: str = "",
        proxy: str = None,
    ):
        self.api = _normalize_api_base_url(api_url, default="", label="MoeMail API URL")
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._configured_username = str(username or "").strip()
        self._configured_password = str(password or "")
        self._configured_session_token = str(session_token or "").strip()
        self._session_token = self._configured_session_token or None
        self._email = None
        self._session = None
        self._username = self._configured_username
        self._password = self._configured_password

    def _new_session(self):
        import requests

        s = requests.Session()
        s.proxies = self.proxy
        mark_session_insecure(s)
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        s.headers.update({"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"})
        return s

    def _extract_session_token(self, session) -> str:
        for cookie in session.cookies:
            if "session-token" in cookie.name:
                return cookie.value
        return ""

    def _apply_session_token(self, session, token: str) -> None:
        domain = urlparse(self.api).hostname or ""
        cookie_names = [
            "__Secure-authjs.session-token",
            "authjs.session-token",
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
        ]
        for name in cookie_names:
            session.cookies.set(name, token, domain=domain, path="/")
            session.cookies.set(name, token, path="/")

    def _login_with_existing_account(self) -> str:
        s = self._new_session()

        if self._configured_session_token:
            self._apply_session_token(s, self._configured_session_token)
            self._session = s
            self._session_token = self._configured_session_token
            print("[MoeMail] 使用已提供的 session-token")
            return self._configured_session_token

        if not (self._configured_username and self._configured_password):
            raise RuntimeError("MoeMail 未配置可复用账号，请提供用户名密码或 session-token")

        with suppress_insecure_request_warning():
            csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        with suppress_insecure_request_warning():
            login_resp = s.post(
                f"{self.api}/api/auth/callback/credentials",
                headers={"content-type": "application/x-www-form-urlencoded"},
                data=urlencode({
                    "username": self._configured_username,
                    "password": self._configured_password,
                    "csrfToken": csrf,
                    "redirect": "false",
                    "callbackUrl": self.api,
                }),
                allow_redirects=True,
                timeout=15,
            )
        self._session = s
        self._username = self._configured_username
        self._password = self._configured_password
        token = self._extract_session_token(s)
        if token:
            self._session_token = token
            print("[MoeMail] 使用手动注册账号登录成功")
            return token
        raise RuntimeError(
            f"MoeMail 登录失败: 已提供用户名密码，但未获取到 session-token (HTTP {login_resp.status_code})"
        )

    def _ensure_session(self) -> str:
        if self._session_token and self._session is not None:
            return self._session_token
        if self._configured_session_token or self._configured_username:
            return self._login_with_existing_account()
        return self._register_and_login()

    def _register_and_login(self) -> str:
        import random, string

        s = self._new_session()
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        self._username = username
        self._password = password
        print(f"[MoeMail] 注册账号: {username} / {password}")
        with suppress_insecure_request_warning():
            r_reg = s.post(f"{self.api}/api/auth/register",
                json={"username": username, "password": password, "turnstileToken": ""},
                timeout=15)
        print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        if r_reg.status_code >= 400:
            try:
                register_error = r_reg.json().get("error") or r_reg.text
            except Exception:
                register_error = r_reg.text
            raise RuntimeError(f"MoeMail 注册失败: {str(register_error).strip() or f'HTTP {r_reg.status_code}'}")
        # 获取 CSRF
        with suppress_insecure_request_warning():
            csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        with suppress_insecure_request_warning():
            login_resp = s.post(f"{self.api}/api/auth/callback/credentials",
                headers={"content-type": "application/x-www-form-urlencoded"},
                data=urlencode({
                    "username": username,
                    "password": password,
                    "csrfToken": csrf,
                    "redirect": "false",
                    "callbackUrl": self.api,
                }),
                allow_redirects=True, timeout=15)
        self._session = s
        token = self._extract_session_token(s)
        if token:
            self._session_token = token
            print(f"[MoeMail] 登录成功")
            return token
        print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        raise RuntimeError(
            f"MoeMail 登录失败: 未获取到 session-token (HTTP {login_resp.status_code})"
        )

    # 优先用这些域名（信誉较好，不易被 AWS/Google 等拒绝）
    _PREFERRED_DOMAINS = ("sall.cc", "cnmlgb.de", "zhooo.org", "coolkid.icu")

    def get_email(self) -> MailboxAccount:
        self._session_token = self._configured_session_token or None
        self._session = None
        self._ensure_session()
        import random, string
        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，优先选信誉好的域名，避免被 AWS 等平台拒绝
        domain = "sall.cc"
        try:
            with suppress_insecure_request_warning():
                cfg_r = self._session.get(f"{self.api}/api/config", timeout=10)
            all_domains = [d.strip() for d in cfg_r.json().get("emailDomains", "sall.cc").split(",") if d.strip()]
            if all_domains:
                # 从可用域名中筛选优先域名，按 _PREFERRED_DOMAINS 顺序选择
                preferred = [d for d in self._PREFERRED_DOMAINS if d in all_domains]
                if preferred:
                    domain = random.choice(preferred)
                else:
                    # 无优先域名可用，从剩余中随机选
                    domain = random.choice(all_domains)
        except Exception:
            pass
        with suppress_insecure_request_warning():
            r = self._session.post(f"{self.api}/api/emails/generate",
                json={"name": name, "domain": domain, "expiryTime": 86400000},
                timeout=15)
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        print(f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}")
        if not email_id:
            print(f"[MoeMail] 生成失败: {data}")
            generate_error = data.get("error") or data.get("message") or r.text
            raise RuntimeError(f"MoeMail 生成邮箱失败: {str(generate_error).strip() or f'HTTP {r.status_code}'}")
        if not self._email:
            raise RuntimeError("MoeMail 生成邮箱失败: 返回结果缺少 email")
        self._email_count = getattr(self, '_email_count', 0) + 1
        return MailboxAccount(
            email=self._email,
            account_id=str(email_id),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "moemail",
                    "login_identifier": getattr(self, "_username", ""),
                    "display_name": getattr(self, "_username", "") or self._email,
                    "credentials": {
                        "username": getattr(self, "_username", ""),
                        "password": getattr(self, "_password", ""),
                        "session_token": self._session_token,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "email": self._email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "moemail",
                    "resource_type": "mailbox",
                    "resource_identifier": str(email_id),
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "email": self._email,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            with suppress_insecure_request_warning():
                r = self._session.get(f"{self.api}/api/emails/{account.account_id}", timeout=10)
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        pattern = re.compile(code_pattern) if code_pattern else None
        while time.time() - start < timeout:
            try:
                with suppress_insecure_request_warning():
                    r = self._session.get(f"{self.api}/api/emails/{account.account_id}",
                        timeout=10)
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen: continue
                    seen.add(mid)
                    body = str(msg.get("content") or msg.get("text") or msg.get("body") or msg.get("html") or "") + " " + str(msg.get("subject") or "")
                    body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
                    if pattern:
                        m = pattern.search(body)
                    else:
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', body)
                    if m: return m.group(1) if m.groups() else m.group(0) if code_pattern else m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                with suppress_insecure_request_warning():
                    r = self._session.get(f"{self.api}/api/emails/{account.account_id}",
                        timeout=10)
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(msg.get("content") or "") + " " +
                        str(msg.get("text") or "") + " " +
                        str(msg.get("body") or "") + " " +
                        str(msg.get("html") or "") + " " +
                        str(msg.get("subject") or "")
                    )
                    link = _extract_verification_link(body, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """

    def __init__(self, api_url: str, admin_token: str = "",
                 username: str = "", password: str = "",
                 proxy: str = None):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = None
        self._email = None

    def _get_session(self):
        import requests
        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15)
        self._session = s
        return s

    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()
        import requests
        r = self._session.get(f"{self.api}/api/generate", timeout=15)
        data = r.json()
        email = data.get("email", "")
        self._email = email
        print(f"[Freemail] 生成邮箱: {email}")
        provider_account = {
            "provider_type": "mailbox",
            "provider_name": "freemail",
            "login_identifier": self.username or email,
            "display_name": self.username or email,
            "credentials": {},
            "metadata": {
                "api_url": self.api,
                "auth_mode": "admin_token" if self.admin_token else "username_password",
            },
        }
        if self.admin_token:
            provider_account["credentials"]["admin_token"] = self.admin_token
        if self.username:
            provider_account["credentials"]["username"] = self.username
        if self.password:
            provider_account["credentials"]["password"] = self.password
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_account": provider_account,
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "freemail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50}, timeout=10)
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20}, timeout=10)
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen: continue
                    seen.add(mid)
                    # 直接用 verification_code 字段
                    code = str(msg.get("verification_code") or "")
                    if code and code != "None":
                        return code
                    # 兜底：从 preview 提取
                    text = str(msg.get("preview", "")) + " " + str(msg.get("subject", ""))
                    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                    if m: return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20}, timeout=10)
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(msg.get(key, ""))
                        for key in ("preview", "subject", "html", "text", "content", "body")
                    )
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TestmailMailbox(BaseMailbox):
    """testmail.app 邮箱服务，地址格式为 {namespace}.{tag}@inbox.testmail.app。"""

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        namespace: str = "",
        tag_prefix: str = "",
        proxy: str = None,
    ):
        self.api = _normalize_api_base_url(api_url, default="", label="Testmail API URL")
        self.api_key = str(api_key or "").strip()
        self.namespace = str(namespace or "").strip()
        self.tag_prefix = str(tag_prefix or "").strip().strip(".")
        self.proxy = {"http": proxy, "https": proxy} if proxy else None

    def _assert_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError("Testmail 未配置 API Key")
        if not self.namespace:
            raise RuntimeError("Testmail 未配置 namespace")

    def _build_tag(self) -> str:
        import random
        import string

        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        return f"{self.tag_prefix}.{suffix}" if self.tag_prefix else suffix

    def _query_inbox(
        self,
        *,
        tag: str,
        timestamp_from: int | None,
        livequery: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        import requests

        params = {
            "apikey": self.api_key,
            "namespace": self.namespace,
            "tag": tag,
            "limit": limit,
        }
        if timestamp_from is not None:
            params["timestamp_from"] = int(timestamp_from)
        if livequery:
            params["livequery"] = "true"
        response = requests.get(self.api, params=params, proxies=self.proxy, timeout=15)
        payload = response.json()
        if payload.get("result") == "fail":
            raise RuntimeError(f"Testmail 查询失败: {payload.get('message') or response.text}")
        return payload.get("emails", []) or []

    @staticmethod
    def _message_id(mail: dict) -> str:
        return str(
            mail.get("id")
            or mail.get("message_id")
            or f"{mail.get('timestamp', '')}:{mail.get('tag', '')}:{mail.get('subject', '')}"
        )

    @staticmethod
    def _message_text(mail: dict) -> str:
        return " ".join(
            str(mail.get(key, "") or "")
            for key in ("subject", "text", "html")
        )

    def get_email(self) -> MailboxAccount:
        import time

        self._assert_ready()
        tag = self._build_tag()
        email = f"{self.namespace}.{tag}@inbox.testmail.app"
        created_at_ms = int(time.time() * 1000)
        return MailboxAccount(
            email=email,
            account_id=tag,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "testmail",
                    "login_identifier": self.namespace,
                    "display_name": self.namespace,
                    "credentials": {
                        "api_key": self.api_key,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "namespace": self.namespace,
                        "tag_prefix": self.tag_prefix,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "testmail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "namespace": self.namespace,
                        "tag": tag,
                        "api_url": self.api,
                        "created_at_ms": created_at_ms,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        tag = str(account.account_id or "")
        if not tag:
            return set()
        started = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {}).get("created_at_ms")
        try:
            mails = self._query_inbox(tag=tag, timestamp_from=started, limit=20)
            return {self._message_id(mail) for mail in mails if self._message_id(mail)}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re
        import time

        tag = str(account.account_id or "")
        if not tag:
            raise RuntimeError("Testmail mailbox 缺少 tag")
        seen = set(before_ids or [])
        started = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {}).get("created_at_ms")
        pattern = re.compile(code_pattern) if code_pattern else None
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._query_inbox(tag=tag, timestamp_from=started, limit=20)
                for mail in sorted(mails, key=lambda item: item.get("timestamp", 0), reverse=True):
                    mid = self._message_id(mail)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = self._message_text(mail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
                    match = pattern.search(text) if pattern else re.search(r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                    if match:
                        return match.group(1) if match.groups() else match.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time

        tag = str(account.account_id or "")
        if not tag:
            raise RuntimeError("Testmail mailbox 缺少 tag")
        seen = set(before_ids or [])
        started = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {}).get("created_at_ms")
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._query_inbox(tag=tag, timestamp_from=started, limit=20)
                for mail in sorted(mails, key=lambda item: item.get("timestamp", 0), reverse=True):
                    mid = self._message_id(mail)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    link = _extract_verification_link(self._message_text(mail), keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class DDGEmailMailbox(BaseMailbox):
    """DuckDuckGo Email Protection — 生成 @duck.com 私密别名，通过 IMAP 从转发邮箱读取验证码"""

    DDG_API = "https://quack.duckduckgo.com/api/email/addresses"

    # 常见邮箱 IMAP 地址自动匹配
    _IMAP_HOSTS = {
        "163.com": "imap.163.com",
        "126.com": "imap.126.com",
        "qq.com": "imap.qq.com",
        "gmail.com": "imap.gmail.com",
        "outlook.com": "imap-mail.outlook.com",
        "hotmail.com": "imap-mail.outlook.com",
        "yahoo.com": "imap.mail.yahoo.com",
    }

    def __init__(self, bearer: str = "", imap_host: str = "",
                 imap_user: str = "", imap_pass: str = "", proxy: str = None):
        self.bearer = bearer
        self.imap_host = imap_host
        self.imap_user = imap_user
        self.imap_pass = imap_pass
        self.proxy = {"http": proxy, "https": proxy} if proxy else None

        # 自动推断 IMAP host
        if not self.imap_host and self.imap_user and "@" in self.imap_user:
            domain = self.imap_user.split("@", 1)[1].lower()
            self.imap_host = self._IMAP_HOSTS.get(domain, f"imap.{domain}")

    def _headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "origin": "https://duckduckgo.com",
            "referer": "https://duckduckgo.com/",
        }

    def get_email(self) -> MailboxAccount:
        import requests
        r = insecure_request(
            requests.post, self.DDG_API,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        address = data.get("address", "")
        if not address:
            raise RuntimeError(f"DDG Email 创建别名失败: {r.text[:200]}")
        email = f"{address}@duck.com"
        print(f"[DDG Email] 创建别名: {email}")
        return MailboxAccount(
            email=email,
            account_id=address,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "ddg_email",
                    "resource_type": "mailbox",
                    "resource_identifier": address,
                    "handle": email,
                    "display_name": email,
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def _imap_search_code(self, alias_email: str, timeout: int, code_pattern: str = None) -> str:
        import imaplib
        import email as email_lib
        import time

        if not self.imap_user or not self.imap_pass:
            raise RuntimeError("DDG Email 未配置 IMAP（ddg_imap_user / ddg_imap_pass），无法读取验证码")

        pattern = code_pattern or r'(?<!\d)(\d{6})(?!\d)'
        start = time.time()
        seen_ids: set[bytes] = set()
        baseline_done = False

        while time.time() - start < timeout:
            conn = None
            try:
                conn = imaplib.IMAP4_SSL(self.imap_host, 993, timeout=10)
                # 163/126 要求先发 ID 命令
                if any(h in self.imap_host for h in ("163.com", "126.com", "yeah.net")):
                    imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')
                    conn._simple_command('ID', '("name" "IMAPClient" "version" "1.0")')
                conn.login(self.imap_user, self.imap_pass)
                conn.select("INBOX", readonly=True)

                _, msg_nums = conn.search(None, "ALL")
                ids = msg_nums[0].split() if msg_nums and msg_nums[0] else []

                # 首次轮询：把所有已有邮件标记为已读，只等新邮件
                if not baseline_done:
                    seen_ids = set(ids)
                    baseline_done = True
                    print(f"[DDG Email] IMAP baseline: {len(seen_ids)} existing emails skipped")
                    conn.logout()
                    conn = None
                    time.sleep(5)
                    continue

                for mid in reversed(ids[-30:]):
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    _, msg_data = conn.fetch(mid, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)

                    # 检查是否是发给 alias 的（DDG 转发会保留原始 To）
                    to_addr = str(msg.get("To", "") or "").lower()
                    from_addr = str(msg.get("From", "") or "").lower()
                    subject = str(msg.get("Subject", "") or "")

                    # 只看发给 alias 或来自 openai/noreply 的
                    if alias_email.lower() not in to_addr and "openai" not in from_addr and "noreply" not in from_addr:
                        continue

                    # 提取正文
                    body_parts = []
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct in ("text/plain", "text/html"):
                                payload = part.get_payload(decode=True)
                                if payload:
                                    charset = part.get_content_charset() or "utf-8"
                                    body_parts.append(payload.decode(charset, errors="replace"))
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            charset = msg.get_content_charset() or "utf-8"
                            body_parts.append(payload.decode(charset, errors="replace"))

                    combined = subject + " " + " ".join(body_parts)
                    # 去掉 style/script 标签内容，避免匹配 CSS 颜色值如 #000000
                    combined = re.sub(r'<style[^>]*>.*?</style>', '', combined, flags=re.DOTALL | re.IGNORECASE)
                    combined = re.sub(r'<script[^>]*>.*?</script>', '', combined, flags=re.DOTALL | re.IGNORECASE)
                    combined = re.sub(r'<[^>]+>', ' ', combined)
                    m = re.search(pattern, combined)
                    if m:
                        code = m.group(1) if m.groups() else m.group(0)
                        print(f"[DDG Email] IMAP 获取验证码: {code}")
                        return code

                conn.logout()
            except (imaplib.IMAP4.error, OSError) as e:
                print(f"[DDG Email] IMAP 连接异常: {e}")
            finally:
                if conn:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            time.sleep(5)

        raise TimeoutError(f"DDG Email IMAP 等待验证码超时 ({timeout}s)")

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        return self._imap_search_code(account.email, timeout, code_pattern)
