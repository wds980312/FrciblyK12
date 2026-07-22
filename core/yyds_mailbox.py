"""YYDS temporary mailbox provider."""
from __future__ import annotations

import re
import time
from typing import Any

from curl_cffi import requests as cffi_requests

from core.base_mailbox import BaseMailbox, MailboxAccount


DEFAULT_API_URL = "https://maliapi.215.im/v1"
DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"
DEFAULT_IMPERSONATE = "chrome120"
RETRYABLE_ERROR_MARKERS = (
    "UNEXPECTED_EOF_WHILE_READING",
    "EOF occurred in violation of protocol",
    "SSLEOFError",
    "Connection aborted",
)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _payload_data(payload: Any) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _strip_html(value: object) -> str:
    if isinstance(value, list):
        return " ".join(_strip_html(item) for item in value)
    text = str(value or "")
    return re.sub(r"<[^>]+>", " ", text)


class YYDSMailbox(BaseMailbox):
    """Create YYDS mailboxes and poll their messages for verification codes."""

    def __init__(
        self,
        *,
        api_url: str = DEFAULT_API_URL,
        api_key: str = "",
        jwt: str = "",
        domain: str = "",
        poll_interval: float | str = 3,
        request_timeout: float | str = 15,
        proxy: str | None = None,
        session: Any | None = None,
    ):
        self.api_url = str(api_url or DEFAULT_API_URL).strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.jwt = str(jwt or "").strip()
        self.domain = str(domain or "").strip()
        self.poll_interval = max(0.0, float(3 if poll_interval in (None, "") else poll_interval))
        self.request_timeout = max(1.0, float(15 if request_timeout in (None, "") else request_timeout))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or self._new_session()

    @staticmethod
    def _new_session():
        return cffi_requests.Session(impersonate=DEFAULT_IMPERSONATE)

    @classmethod
    def from_config(cls, config: dict) -> "YYDSMailbox":
        return cls(
            api_url=config.get("yyds_api_url", DEFAULT_API_URL),
            api_key=config.get("yyds_api_key", ""),
            jwt=config.get("yyds_jwt", ""),
            domain=config.get("yyds_domain", ""),
            poll_interval=config.get("yyds_poll_interval", 3),
            request_timeout=config.get("yyds_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
        )

    def _auth_headers(self, *, token: str = "") -> dict[str, str]:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request_kwargs(self, *, headers: dict | None = None, params: dict | None = None, json: dict | None = None) -> dict:
        kwargs: dict = {
            "headers": headers or {},
            "proxies": self.proxy,
            "timeout": self.request_timeout,
        }
        if params:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        return kwargs

    def _request(self, method: str, url: str, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                request = getattr(self.session, method)
                return request(url, **kwargs)
            except Exception as exc:
                last_exc = exc
                message = str(exc)
                retryable = any(marker in message for marker in RETRYABLE_ERROR_MARKERS)
                if not retryable or attempt >= 2:
                    raise
                self.session = self._new_session()
                time.sleep(0.5 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise RuntimeError("YYDS 请求失败")

    def get_email(self) -> MailboxAccount:
        if not self.api_key and not self.jwt:
            raise RuntimeError("YYDS 邮箱未配置 API Key 或 JWT")

        payload = {"autoDomainStrategy": "prefer_owned"}
        if self.domain:
            payload = {"domain": self.domain}

        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        response = self._request(
            "post",
            f"{self.api_url}/accounts",
            **self._request_kwargs(headers=headers, json=payload),
        )
        response.raise_for_status()
        data = _payload_data(response.json())
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address:
            raise RuntimeError("YYDS 创建邮箱失败：响应未返回 address")
        if not token:
            token = self._fetch_token(address)
        if not token:
            raise RuntimeError("YYDS 创建邮箱失败：响应未返回 token")

        return MailboxAccount(
            email=address,
            account_id=token,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "yyds_mail_api",
                    "login_identifier": address,
                    "display_name": address,
                    "credentials": {},
                    "metadata": {"api_url": self.api_url, "domain": self.domain},
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "yyds_mail_api",
                    "resource_type": "mailbox",
                    "resource_identifier": token,
                    "handle": address,
                    "display_name": address,
                    "metadata": {"email": address, "api_url": self.api_url},
                },
            },
        )

    def _fetch_token(self, address: str) -> str:
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        response = self._request(
            "post",
            f"{self.api_url}/token",
            **self._request_kwargs(headers=headers, json={"address": address}),
        )
        response.raise_for_status()
        return str(_payload_data(response.json()).get("token") or "").strip()

    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        response = self._request(
            "get",
            f"{self.api_url}/messages",
            **self._request_kwargs(
                headers=self._auth_headers(token=account.account_id),
                params={"address": account.email},
            ),
        )
        response.raise_for_status()
        data = _payload_data(response.json())
        messages = data.get("messages") if isinstance(data, dict) else []
        return messages if isinstance(messages, list) else []

    def _message_detail(self, account: MailboxAccount, message_id: str) -> dict:
        response = self._request(
            "get",
            f"{self.api_url}/messages/{message_id}",
            **self._request_kwargs(
                headers=self._auth_headers(token=account.account_id),
                params={"address": account.email},
            ),
        )
        response.raise_for_status()
        return _payload_data(response.json())

    def get_current_ids(self, account: MailboxAccount) -> set:
        return {str(item.get("id") or "") for item in self._list_messages(account) if item.get("id")}

    @staticmethod
    def _extract_code(detail: dict, code_pattern: str | None = None) -> str:
        for key in ("verificationCode", "verification_code", "code", "otp"):
            value = str(detail.get(key) or "").strip()
            if value:
                return value
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        parts = [
            detail.get("subject", ""),
            detail.get("text", ""),
            _strip_html(detail.get("html", "")),
        ]
        match = pattern.search(" ".join(str(part or "") for part in parts))
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        seen = set(before_ids or set())
        deadline = time.time() + timeout
        while time.time() < deadline:
            for item in self._list_messages(account):
                message_id = str(item.get("id") or "").strip()
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                if keyword and keyword.lower() not in str(item.get("subject") or "").lower():
                    continue
                detail = self._message_detail(account, message_id)
                code = self._extract_code(detail, code_pattern=code_pattern)
                if code:
                    return code
            time.sleep(self.poll_interval)
        raise TimeoutError(f"YYDS 等待验证码超时 ({timeout}s)")
