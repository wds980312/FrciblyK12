"""Sub2API Agent Identity import helpers."""
from __future__ import annotations

import json
import logging
from typing import Any, Tuple

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

DEFAULT_SUB2API_API_URL = "http://host.docker.internal:18080/api/v1"


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return config_store.get(key, "")
    except Exception:
        return ""


def _normalize_api_url(api_url: str) -> str:
    api_url = str(api_url or "").strip().rstrip("/")
    if not api_url:
        return ""
    if api_url.endswith("/api/v1"):
        return api_url
    return f"{api_url}/api/v1"


def _extract_error(response) -> str:
    fallback = f"Sub2API 导入失败: HTTP {response.status_code}"
    try:
        body = response.json()
    except Exception:
        text = str(getattr(response, "text", "") or "")
        return f"{fallback} - {text[:200]}" if text else fallback
    if isinstance(body, dict):
        return str(
            body.get("message")
            or body.get("detail")
            or body.get("error")
            or fallback
        )
    return fallback


def _auth_context(api_url: str | None = None, auth_token: str | None = None) -> Tuple[str, str, str | None]:
    if not api_url:
        api_url = _get_config_value("sub2api_api_url") or DEFAULT_SUB2API_API_URL
    if not auth_token:
        auth_token = (
            _get_config_value("sub2api_auth_token")
            or _get_config_value("sub2api_api_key")
        )
    api_url = _normalize_api_url(api_url)
    auth_token = str(auth_token or "").strip()
    if not api_url:
        return "", "", "Sub2API API URL 未配置"
    if not auth_token:
        return "", "", "Sub2API auth token 未配置"
    return api_url, auth_token, None


def get_sub2api_config_error(
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> str:
    _, _, error = _auth_context(api_url=api_url, auth_token=auth_token)
    return error or ""


def _auth_headers(auth_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }


def _extract_account_items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    candidates = [
        body.get("items"),
        body.get("data"),
        body.get("accounts"),
        body.get("records"),
        body.get("list"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = _extract_account_items(candidate)
            if nested:
                return nested
    return []


def _account_matches_email(item: dict[str, Any], email: str) -> bool:
    expected = str(email or "").strip().lower()
    if not expected:
        return False
    values = [
        item.get("name"),
        item.get("email"),
        item.get("account"),
        item.get("username"),
        item.get("label"),
    ]
    return any(str(value or "").strip().lower() == expected for value in values)


def _extract_export_email(export_data: dict[str, Any]) -> str:
    accounts = export_data.get("accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, dict):
                continue
            for key in ("name", "email", "account", "username"):
                value = str(item.get(key) or "").strip()
                if "@" in value:
                    return value
    for container_key in ("credentials", "extra", "agent_identity"):
        container = export_data.get(container_key)
        if isinstance(container, dict):
            value = str(container.get("email") or container.get("name") or "").strip()
            if "@" in value:
                return value
    return ""


def _extract_id_list(item: dict[str, Any], key: str) -> list[int]:
    raw = item.get(key)
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for value in raw:
        try:
            result.append(int(value))
        except Exception:
            continue
    return result


def list_sub2api_groups(
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> list[dict[str, Any]]:
    api_url, auth_token, error = _auth_context(api_url=api_url, auth_token=auth_token)
    if error:
        return []
    groups: list[dict[str, Any]] = []
    for params in (
        {"page": 1, "page_size": 200, "status": "active"},
        {"page": 1, "page_size": 200},
    ):
        try:
            response = cffi_requests.get(
                f"{api_url}/admin/groups",
                headers=_auth_headers(auth_token),
                params=params,
                proxies=None,
                verify=False,
                timeout=30,
                impersonate="chrome120",
            )
            if response.status_code not in (200, 201):
                continue
            groups = _extract_account_items(response.json())
        except Exception as exc:
            logger.warning("Sub2API 分组查询异常: %s", exc)
            continue
        if groups:
            break
    return groups


def resolve_sub2api_group(
    group_ref: str,
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> dict[str, Any] | None:
    ref = str(group_ref or "").strip()
    if not ref:
        return None
    groups = list_sub2api_groups(api_url=api_url, auth_token=auth_token)
    if ref.isdigit():
        expected_id = int(ref)
        for group in groups:
            try:
                if int(group.get("id") or 0) == expected_id:
                    return group
            except Exception:
                continue
    expected_name = ref.lower()
    for group in groups:
        if str(group.get("name") or "").strip().lower() == expected_name:
            return group
    return None


def bind_sub2api_account_to_group(
    account: dict[str, Any],
    group: dict[str, Any],
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> Tuple[bool, str]:
    api_url, auth_token, error = _auth_context(api_url=api_url, auth_token=auth_token)
    if error:
        return False, error
    account_id = account.get("id") or account.get("account_id") or account.get("uuid")
    group_id = group.get("id")
    if not account_id:
        return False, "Sub2API 账号 ID 为空，无法绑定分组"
    if not group_id:
        return False, "Sub2API 分组 ID 为空，无法绑定分组"
    try:
        group_id_int = int(group_id)
    except Exception:
        return False, f"Sub2API 分组 ID 无效: {group_id}"
    current_group_ids = _extract_id_list(account, "group_ids")
    if not current_group_ids and isinstance(account.get("groups"), list):
        current_group_ids = [
            int(item["id"])
            for item in account["groups"]
            if isinstance(item, dict) and str(item.get("id") or "").isdigit()
        ]
    next_group_ids = sorted({*current_group_ids, group_id_int})
    try:
        response = cffi_requests.put(
            f"{api_url}/admin/accounts/{account_id}",
            headers=_auth_headers(auth_token),
            json={"group_ids": next_group_ids},
            proxies=None,
            verify=False,
            timeout=30,
            impersonate="chrome120",
        )
        if response.status_code in (200, 201, 202, 204):
            return True, f"已绑定分组 {group.get('name') or group_id_int}"
        return False, _extract_error(response)
    except Exception as exc:
        logger.error("Sub2API 分组绑定异常: %s", exc)
        return False, f"Sub2API 分组绑定异常: {exc}"


def bind_sub2api_account_email_to_group(
    email: str,
    group_ref: str,
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> Tuple[bool, str]:
    email = str(email or "").strip()
    group_ref = str(group_ref or "").strip()
    if not email:
        return False, "Agent Identity 导出内容缺少邮箱，无法绑定 Sub2API 分组"
    if not group_ref:
        return True, ""
    group = resolve_sub2api_group(group_ref, api_url=api_url, auth_token=auth_token)
    if not group:
        return False, f"Sub2API 分组不存在: {group_ref}"
    matches = find_sub2api_accounts_by_emails([email], api_url=api_url, auth_token=auth_token)
    accounts = matches.get(email) or []
    if not accounts:
        return False, f"Sub2API 未找到刚导入账号: {email}"
    return bind_sub2api_account_to_group(
        accounts[0],
        group,
        api_url=api_url,
        auth_token=auth_token,
    )


def find_sub2api_accounts_by_emails(
    emails: list[str],
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Find imported Sub2API account records by exact email/name."""
    clean_emails = sorted({str(email or "").strip() for email in emails if str(email or "").strip()})
    if not clean_emails:
        return {}
    api_url, auth_token, error = _auth_context(api_url=api_url, auth_token=auth_token)
    if error:
        return {}

    headers = _auth_headers(auth_token)
    result: dict[str, list[dict[str, Any]]] = {email: [] for email in clean_emails}
    for email in clean_emails:
        seen_ids: set[str] = set()
        for params in (
            {"search": email, "page": 1, "page_size": 100},
            {"keyword": email, "page": 1, "page_size": 100},
            {"name": email, "page": 1, "page_size": 100},
            {"q": email, "page": 1, "page_size": 100},
        ):
            try:
                response = cffi_requests.get(
                    f"{api_url}/admin/accounts",
                    headers=headers,
                    params=params,
                    proxies=None,
                    verify=False,
                    timeout=30,
                    impersonate="chrome120",
                )
                if response.status_code not in (200, 201):
                    continue
                items = _extract_account_items(response.json())
            except Exception as exc:
                logger.warning("Sub2API 账号查询异常: %s", exc)
                continue
            for item in items:
                if not _account_matches_email(item, email):
                    continue
                item_id = str(item.get("id") or item.get("account_id") or item.get("uuid") or "")
                dedupe_key = item_id or json.dumps(item, sort_keys=True, ensure_ascii=False)
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                result[email].append(item)
            if result[email]:
                break
    return {email: matches for email, matches in result.items() if matches}


def _has_model_response(response) -> bool:
    text = str(getattr(response, "text", "") or "")
    if not text.strip():
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error") or data.get("error_message"):
            continue
        if any(key in data for key in ("delta", "content", "text", "choices", "output")):
            return True
    return False


def _extract_model_test_error(response) -> str | None:
    """Return the upstream reason embedded in a successful SSE test response."""
    text = str(getattr(response, "text", "") or "")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        error = data.get("error") or data.get("error_message")
        if isinstance(error, dict):
            message = error.get("message") or error.get("error") or error.get("detail")
        else:
            message = error
        if message:
            return str(message)
    return None


def probe_sub2api_account_model(
    email: str,
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
    sub2api_account_id: int | str | None = None,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Run the same minimal model test used by Sub2API's account test UI."""
    api_url, auth_token, error = _auth_context(api_url=api_url, auth_token=auth_token)
    if error:
        return {"status": "error", "message": error}

    account_id = sub2api_account_id
    if not account_id:
        matches = find_sub2api_accounts_by_emails(
            [email],
            api_url=api_url,
            auth_token=auth_token,
        )
        accounts = matches.get(str(email or "").strip()) or []
        if not accounts:
            return {"status": "error", "message": f"Sub2API 未找到账号: {email}"}
        account_id = accounts[0].get("id") or accounts[0].get("account_id")
    if not account_id:
        return {"status": "error", "message": f"Sub2API 账号 ID 为空: {email}"}

    try:
        response = cffi_requests.post(
            f"{api_url}/admin/accounts/{account_id}/test",
            headers=_auth_headers(auth_token),
            data=json.dumps(
                {"model_id": "gpt-5.5", "prompt": "hi", "mode": "compact"},
                ensure_ascii=False,
            ).encode("utf-8"),
            proxies=None,
            verify=False,
            timeout=max(int(timeout_seconds or 15), 1),
            impersonate="chrome120",
        )
    except Exception as exc:
        logger.warning("Sub2API 模型测试异常: %s", exc)
        return {"status": "error", "message": f"Sub2API 模型测试异常: {exc}"}

    if response.status_code not in (200, 201):
        return {
            "status": "failed",
            "message": _extract_error(response),
            "sub2api_account_id": account_id,
        }
    if not _has_model_response(response):
        upstream_error = _extract_model_test_error(response)
        return {
            "status": "failed",
            "message": upstream_error or "gpt-5.5 未返回模型响应",
            "sub2api_account_id": account_id,
        }
    return {
        "status": "success",
        "message": "gpt-5.5 返回了模型响应",
        "sub2api_account_id": account_id,
    }


def delete_sub2api_account(
    account_id: int | str,
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
) -> Tuple[bool, str]:
    """Delete a Sub2API account by admin account id."""
    api_url, auth_token, error = _auth_context(api_url=api_url, auth_token=auth_token)
    if error:
        return False, error
    account_id_text = str(account_id or "").strip()
    if not account_id_text:
        return False, "Sub2API account id 为空"
    try:
        response = cffi_requests.delete(
            f"{api_url}/admin/accounts/{account_id_text}",
            headers=_auth_headers(auth_token),
            proxies=None,
            verify=False,
            timeout=30,
            impersonate="chrome120",
        )
        if response.status_code in (200, 202, 204, 404):
            return True, "Sub2API 账号已删除"
        return False, _extract_error(response)
    except Exception as exc:
        logger.error("Sub2API 账号删除异常: %s", exc)
        return False, f"Sub2API 删除异常: {exc}"


def upload_agent_identity_to_sub2api(
    export_data: dict[str, Any],
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
    default_group: str | None = None,
) -> Tuple[bool, str]:
    """Import an Agent Identity Sub2API export JSON into Sub2API."""
    api_url, auth_token, error = _auth_context(api_url=api_url, auth_token=auth_token)
    if error:
        return False, error
    if not isinstance(export_data, dict):
        return False, "Agent Identity 导出内容不是 JSON 对象"
    if export_data.get("auth_mode") != "agentIdentity":
        return False, "Agent Identity 导出内容缺少 auth_mode=agentIdentity"

    target_url = f"{api_url}/admin/accounts/data"
    headers = _auth_headers(auth_token)
    logger.info("[Sub2API] 导入 Agent Identity")

    try:
        response = cffi_requests.post(
            target_url,
            headers=headers,
            data=json.dumps(
                {
                    "data": export_data,
                    "skip_default_group_bind": False,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            proxies=None,
            verify=False,
            timeout=120,
            impersonate="chrome120",
        )
        if response.status_code in (200, 201, 202, 207):
            try:
                body = response.json()
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("success") is False:
                return False, _extract_error(response)
            group_ref = str(
                default_group
                if default_group is not None
                else _get_config_value("sub2api_default_group")
            ).strip()
            if not group_ref:
                return True, "Sub2API 数据导入成功"
            ok, bind_message = bind_sub2api_account_email_to_group(
                _extract_export_email(export_data),
                group_ref,
                api_url=api_url,
                auth_token=auth_token,
            )
            if not ok:
                return False, f"Sub2API 数据导入成功，但分组绑定失败: {bind_message}"
            return True, f"Sub2API 数据导入成功，{bind_message}"
        return False, _extract_error(response)
    except Exception as exc:
        logger.error("Sub2API Agent Identity 导入异常: %s", exc)
        return False, f"Sub2API 导入异常: {exc}"
