from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .constants import CHATGPT_APP, OPENAI_AUTH


AUTH_WORKSPACE_URL = f"{OPENAI_AUTH}/workspace"
AUTH_WORKSPACE_SELECT_URL = f"{OPENAI_AUTH}/api/accounts/workspace/select"


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _base64url_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_jwt_payload(token: str | None) -> dict[str, Any]:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _openai_auth(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("https://api.openai.com/auth"))


def _openai_profile(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("https://api.openai.com/profile"))


def _normalize_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            number = float(value)
            dt = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _epoch_seconds(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number / 1000 if number > 1e11 else number)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return 0


def build_synthetic_codex_id_token(
    *,
    email: str = "",
    account_id: str = "",
    plan_type: str = "",
    user_id: str = "",
    expires_at: str = "",
) -> str:
    if not account_id:
        return ""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    auth_info: dict[str, Any] = {"chatgpt_account_id": account_id}
    if plan_type:
        auth_info["chatgpt_plan_type"] = plan_type
    if user_id:
        auth_info["chatgpt_user_id"] = user_id
        auth_info["user_id"] = user_id
    payload: dict[str, Any] = {
        "iat": now,
        "exp": _epoch_seconds(expires_at) or now + 90 * 24 * 60 * 60,
        "https://api.openai.com/auth": auth_info,
    }
    if email:
        payload["email"] = email
    return f"{_base64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_base64url_json(payload)}."


def convert_chatgpt_session_to_cpa_json(
    session: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(session, dict):
        raise ValueError("session must be a JSON object")

    token = _as_dict(session.get("token"))
    credentials = _as_dict(session.get("credentials"))
    provider_data = _as_dict(session.get("providerSpecificData"))
    account = _as_dict(session.get("account"))
    user = _as_dict(session.get("user"))

    access_token = _first_text(
        session.get("accessToken"),
        session.get("access_token"),
        token.get("accessToken"),
        token.get("access_token"),
        credentials.get("accessToken"),
        credentials.get("access_token"),
    )
    if not access_token:
        raise ValueError("missing accessToken")

    session_token = _first_text(
        session.get("sessionToken"),
        session.get("session_token"),
        token.get("sessionToken"),
        token.get("session_token"),
        credentials.get("session_token"),
    )
    refresh_token = _first_text(
        session.get("refreshToken"),
        session.get("refresh_token"),
        token.get("refreshToken"),
        token.get("refresh_token"),
        credentials.get("refresh_token"),
    )
    input_id_token = _first_text(
        session.get("idToken"),
        session.get("id_token"),
        token.get("idToken"),
        token.get("id_token"),
        credentials.get("id_token"),
    )

    payload = parse_jwt_payload(access_token)
    id_payload = parse_jwt_payload(input_id_token)
    auth = _openai_auth(payload)
    id_auth = _openai_auth(id_payload)
    profile = _openai_profile(payload)

    expires_at = _first_text(
        _normalize_timestamp(payload.get("exp")),
        _normalize_timestamp(session.get("expires")),
        _normalize_timestamp(session.get("expiresAt")),
        _normalize_timestamp(session.get("expired")),
        _normalize_timestamp(session.get("expires_at")),
    )
    email = _first_text(
        user.get("email"),
        session.get("email"),
        credentials.get("email"),
        provider_data.get("email"),
        profile.get("email"),
        id_payload.get("email"),
        payload.get("email"),
    )
    account_id = _first_text(
        auth.get("chatgpt_account_id"),
        id_auth.get("chatgpt_account_id"),
        credentials.get("chatgpt_account_id"),
        provider_data.get("chatgptAccountId"),
        provider_data.get("chatgpt_account_id"),
        session.get("chatgptAccountId"),
        session.get("account_id"),
        account.get("id"),
        session.get("id") if session.get("provider") == "codex" else "",
    )
    user_id = _first_text(
        user.get("id"),
        session.get("user_id"),
        session.get("chatgptUserId"),
        provider_data.get("chatgptUserId"),
        provider_data.get("chatgpt_user_id"),
        auth.get("chatgpt_user_id"),
        auth.get("user_id"),
        id_auth.get("chatgpt_user_id"),
        id_auth.get("user_id"),
    )
    plan_type = _first_text(
        auth.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"),
        credentials.get("plan_type"),
        provider_data.get("chatgptPlanType"),
        provider_data.get("chatgpt_plan_type"),
        session.get("planType"),
        session.get("plan_type"),
        account.get("planType"),
        account.get("plan_type"),
    )
    synthetic_id_token = ""
    if not input_id_token:
        synthetic_id_token = build_synthetic_codex_id_token(
            email=email,
            account_id=account_id,
            plan_type=plan_type,
            user_id=user_id,
            expires_at=expires_at,
        )
    id_token = input_id_token or synthetic_id_token
    exported_at = _normalize_timestamp(now or datetime.now(tz=timezone.utc))
    name = _first_text(email, "ChatGPT Account")

    data = {
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email,
        "name": name,
        "plan_type": plan_type,
        "chatgpt_plan_type": plan_type,
        "id_token": id_token,
        "id_token_synthetic": True if synthetic_id_token else None,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "session_token": session_token,
        "last_refresh": exported_at,
        "expired": expires_at,
        "disabled": True if session.get("disabled") else None,
    }
    return {key: value for key, value in data.items() if value is not None}


def _sanitize_file_token(value: str, fallback: str = "chatgpt-session") -> str:
    base = _first_text(value, fallback)
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return (base or fallback)[:80]


def _timestamp_token(now: datetime | None = None) -> str:
    dt = now or datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


def save_cpa_json_locally(
    cpa_json: dict[str, Any],
    *,
    email: str = "",
    output_dir: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    target_dir = Path(output_dir) if output_dir is not None else Path("data") / "cpa_exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_sanitize_file_token(email or str(cpa_json.get('email') or 'chatgpt-session'))}_{_timestamp_token(now)}.json"
    path = target_dir / filename
    path.write_text(json.dumps(cpa_json, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.resolve()


def _read_json_body_from_page(page) -> dict[str, Any]:
    text = page.evaluate(
        """
        () => {
          const pre = document.querySelector("pre");
          return String((pre && pre.innerText) || document.body.innerText || "");
        }
        """
    )
    data = json.loads(str(text or "").strip())
    if not isinstance(data, dict):
        raise ValueError("session page did not return a JSON object")
    return data


def _session_account_id(session_json: dict[str, Any]) -> str:
    try:
        return str(convert_chatgpt_session_to_cpa_json(session_json).get("account_id") or "").strip()
    except Exception:
        return ""


def _workspace_id_matches(account_id: str, workspace_id: str) -> bool:
    account_id = str(account_id or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not account_id or not workspace_id:
        return False
    return account_id == workspace_id or account_id.startswith(workspace_id) or workspace_id.startswith(account_id)


def _ensure_chatgpt_page(page) -> None:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" not in current_url.lower():
        page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)


def _fetch_session_json_via_api(page, *, workspace_id: str = "") -> dict[str, Any]:
    _ensure_chatgpt_page(page)
    result = page.evaluate(
        """
        async ({ workspaceId }) => {
          const headers = { accept: "*/*" };
          if (workspaceId) {
            headers["Chatgpt-Account-Id"] = workspaceId;
            headers["chatgpt-account-id"] = workspaceId;
          }
          const response = await fetch("/api/auth/session", {
            method: "GET",
            credentials: "include",
            cache: "no-store",
            headers,
          });
          const text = await response.text().catch(() => "");
          let json = {};
          try { json = text ? JSON.parse(text) : {}; } catch (_) {}
          return { ok: response.ok, status: response.status, text: text.slice(0, 300), json };
        }
        """,
        {"workspaceId": str(workspace_id or "").strip()},
    )
    data = dict(result or {})
    if not data.get("ok"):
        raise RuntimeError(f"session API HTTP {data.get('status')}: {str(data.get('text') or '')[:160]}")
    session_json = data.get("json")
    if not isinstance(session_json, dict):
        raise RuntimeError("session API did not return JSON object")
    return session_json


def _read_current_session_json(page) -> dict[str, Any]:
    page.goto(f"{CHATGPT_APP}/api/auth/session", wait_until="domcontentloaded", timeout=60000)
    return _read_json_body_from_page(page)


def _follow_auth_workspace_select(page, next_url: str, *, log=None) -> None:
    current_url = str(next_url or "").strip()
    if not current_url:
        raise RuntimeError("workspace/select response missing continue_url")

    for _hop in range(8):
        resolved = urljoin(OPENAI_AUTH, current_url)
        _safe_log(log, f"Workspace Join: follow workspace/select -> {resolved.split('?')[0]}")
        page.goto(resolved, wait_until="domcontentloaded", timeout=60000)
        if resolved.startswith(f"{CHATGPT_APP}/api/auth/callback/openai"):
            return
        current = str(getattr(page, "url", "") or "").strip()
        if current.startswith(f"{CHATGPT_APP}/"):
            return
        if current.startswith(f"{OPENAI_AUTH}/workspace"):
            return
        if current and current != resolved:
            current_url = current
            continue
        return
    raise RuntimeError(f"workspace/select follow exceeded redirects: {current_url}")


def _select_workspace_session_via_auth(page, *, workspace_id: str, log=None) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return {"ok": False, "error": "empty workspace_id"}

    _safe_log(log, f"Workspace Join: auth workspace/select {workspace_id[:8]}")
    page.goto(AUTH_WORKSPACE_URL, wait_until="domcontentloaded", timeout=60000)
    result = page.evaluate(
        """
        async ({ workspaceId }) => {
          const response = await fetch("/api/accounts/workspace/select", {
            method: "POST",
            credentials: "include",
            headers: {
              accept: "application/json",
              "content-type": "application/json",
            },
            body: JSON.stringify({ workspace_id: workspaceId }),
          });
          const text = await response.text().catch(() => "");
          let json = {};
          try { json = text ? JSON.parse(text) : {}; } catch (_) {}
          const nextUrl = String(json?.page?.payload?.url || json?.continue_url || "");
          return {
            ok: response.ok,
            status: response.status,
            url: response.url,
            text: text.slice(0, 500),
            nextUrl,
          };
        }
        """,
        {"workspaceId": workspace_id},
    )
    data = dict(result or {})
    if not data.get("ok"):
        return {
            "ok": False,
            "error": f"auth workspace/select HTTP {data.get('status')}: {str(data.get('text') or '')[:180]}",
        }

    next_url = str(data.get("nextUrl") or "").strip()
    if next_url:
        _follow_auth_workspace_select(page, next_url, log=log)
    else:
        _safe_log(log, "Workspace Join: auth workspace/select returned no continue_url; reading session directly")

    session_json = _read_current_session_json(page)
    account_id = _session_account_id(session_json)
    if not _workspace_id_matches(account_id, workspace_id):
        return {
            "ok": False,
            "error": f"auth workspace/select did not expose target session: account_id={account_id or '-'}",
            "session": session_json,
            "account_id": account_id,
        }
    _safe_log(log, f"Workspace Join: confirmed workspace by auth_select: {account_id[:8]}")
    return {
        "ok": True,
        "method": "auth_workspace_select",
        "session": session_json,
        "account_id": account_id,
    }


def _wait_session_workspace_selected(
    page,
    *,
    workspace_id: str,
    timeout_ms: int = 5000,
    log=None,
) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    if not str(getattr(page, "url", "") or "").strip():
        return {"ok": False, "error": "page url unavailable"}
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    last_account_id = ""
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        for header_workspace_id in (workspace_id, ""):
            try:
                session_json = _fetch_session_json_via_api(page, workspace_id=header_workspace_id)
                account_id = _session_account_id(session_json)
                last_account_id = account_id
                if _workspace_id_matches(account_id, workspace_id):
                    source = "session_api_header" if header_workspace_id else "session_api"
                    _safe_log(log, f"Workspace Join: confirmed workspace by {source}: {account_id[:8]}")
                    return {
                        "ok": True,
                        "session": session_json,
                        "account_id": account_id,
                        "source": source,
                    }
            except Exception as exc:
                last_exc = exc
        time.sleep(0.5)
    detail = f"account_id={last_account_id or '-'}"
    if last_exc is not None:
        detail += f", last_error={last_exc}"
    return {"ok": False, "error": detail}


def _exchange_workspace_session_via_api(page, *, workspace_id: str, log=None) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return {"ok": False, "error": "empty workspace_id"}

    _ensure_chatgpt_page(page)
    result = page.evaluate(
        """
        async ({ workspaceId }) => {
          const url = `/api/auth/session?exchange_workspace_token=true&workspace_id=${encodeURIComponent(workspaceId)}&reason=setCurrentAccount`;
          const response = await fetch(url, {
            method: "GET",
            credentials: "include",
            cache: "no-store",
            headers: {
              accept: "*/*",
              "Chatgpt-Account-Id": workspaceId,
              "chatgpt-account-id": workspaceId,
            },
          });
          const text = await response.text().catch(() => "");
          let json = {};
          try { json = text ? JSON.parse(text) : {}; } catch (_) {}
          return { ok: response.ok, status: response.status, url, text: text.slice(0, 300), json };
        }
        """,
        {"workspaceId": workspace_id},
    )
    data = dict(result or {})
    if not data.get("ok"):
        return {
            "ok": False,
            "error": f"session exchange HTTP {data.get('status')}: {str(data.get('text') or '')[:160]}",
        }

    session_json = data.get("json")
    if not isinstance(session_json, dict):
        return {"ok": False, "error": "session exchange did not return JSON object"}

    account_id = _session_account_id(session_json)
    if not _workspace_id_matches(account_id, workspace_id):
        return {
            "ok": False,
            "error": f"session exchange did not expose target workspace: account_id={account_id or '-'}",
            "session": session_json,
            "account_id": account_id,
        }

    _safe_log(log, f"Workspace Join: confirmed workspace by session exchange: {account_id[:8]}")
    return {
        "ok": True,
        "method": "session_exchange_workspace_token",
        "session": session_json,
        "account_id": account_id,
    }


def _try_switch_workspace_via_api(page, *, workspace_id: str, log=None) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return {"ok": False, "error": "empty workspace_id"}

    _ensure_chatgpt_page(page)
    exchange_result = _exchange_workspace_session_via_api(page, workspace_id=workspace_id, log=log)
    if (
        isinstance(exchange_result, dict)
        and exchange_result.get("ok")
        and isinstance(exchange_result.get("session"), dict)
    ):
        return {
            "ok": True,
            "method": str(exchange_result.get("method") or "session_exchange_workspace_token"),
            "session": dict(exchange_result["session"]),
            "account_id": str(exchange_result.get("account_id") or ""),
        }
    _safe_log(
        log,
        "Workspace Join: session exchange failed, fallback to session probe: "
        f"{exchange_result.get('error') if isinstance(exchange_result, dict) else exchange_result}",
    )

    session_result = _wait_session_workspace_selected(
        page,
        workspace_id=workspace_id,
        timeout_ms=12000,
        log=log,
    )
    if (
        isinstance(session_result, dict)
        and session_result.get("ok")
        and isinstance(session_result.get("session"), dict)
    ):
        return {
            "ok": True,
            "method": str(session_result.get("source") or "session_header_wait"),
            "session": dict(session_result["session"]),
            "account_id": str(session_result.get("account_id") or ""),
        }
    detail = session_result.get("error") if isinstance(session_result, dict) else session_result
    error = f"session header did not expose workspace: {detail or 'not selected'}"
    _safe_log(log, f"Workspace Join: {error}")
    return {"ok": False, "error": error}


def _profile_workspace_text(page, timeout_ms: int) -> str:
    page.wait_for_function(
        """
        () => {
          const el = document.querySelector('[data-testid="accounts-profile-button"]');
          const text = String((el && (el.innerText || el.textContent || el.getAttribute("aria-label"))) || "");
          return /Workspace/i.test(text) && !/Personal|个人帐户|个人账户/.test(text);
        }
        """,
        timeout=timeout_ms,
    )
    text = page.evaluate(
        """
        () => {
          const el = document.querySelector('[data-testid="accounts-profile-button"]');
          return String((el && (el.innerText || el.textContent || el.getAttribute("aria-label"))) || "").trim();
        }
        """
    )
    return str(text or "").strip()


def _looks_like_navigation_interrupt(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "execution context was destroyed",
            "most likely because of a navigation",
            "navigation",
            "frame was detached",
            "context was destroyed",
        )
    )


_PROFILE_MENU_TARGET_SCRIPT = """
() => {
  // PROFILE_MENU_TARGET
  const textOf = (el) => String(
    el && (el.innerText || el.textContent || el.getAttribute("aria-label")) || ""
  ).replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const target = document.querySelector('[data-testid="accounts-profile-button"]');
  if (!visible(target)) {
    return { ok: false, summary: "profile button not visible" };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    text: textOf(target),
  };
}
"""


_ACCOUNT_SUBMENU_TARGET_SCRIPT = """
() => {
  // ACCOUNT_SUBMENU_TARGET
  const textOf = (el) => String(
    el && (el.innerText || el.textContent || el.getAttribute("aria-label")) || ""
  ).replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const accountPattern = /Personal|Account|Free|Plus|Pro|Team|Enterprise|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237|\\u5e10\\u6237|\\u8d26\\u6237/i;
  const excludedPattern = /^Help\\b|\\u5e2e\\u52a9|Settings|\\u8bbe\\u7f6e/i;
  const items = Array.from(document.querySelectorAll(
    '[role="menuitem"][data-has-submenu], [role="menuitem"][aria-haspopup="menu"]'
  )).filter(visible);
  const accountItems = items.filter((item) => !excludedPattern.test(textOf(item)));
  const target = accountItems.find((item) => accountPattern.test(textOf(item))) ||
    accountItems.find((item) => item.querySelector('[data-testid="menu-item-submenu-chevron"]'));
  const candidates = items.map(textOf).filter(Boolean).slice(0, 8);
  if (!target) {
    return { ok: false, summary: `account submenu not visible; candidates=${JSON.stringify(candidates)}` };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    text: textOf(target),
  };
}
"""


_WORKSPACE_RADIO_TARGET_SCRIPT = """
(options = {}) => {
  // WORKSPACE_RADIO_TARGET
  const skipTexts = new Set((options && Array.isArray(options.skipTexts) ? options.skipTexts : []).map(String));
  const textOf = (el) => String(
    el && (el.innerText || el.textContent || el.getAttribute("aria-label")) || ""
  ).replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const workspacePattern = /Workspace|\\u5de5\\u4f5c\\u7a7a\\u95f4|\\u5de5\\u4f5c\\u533a/i;
  const items = Array.from(document.querySelectorAll('[role="menuitemradio"]'))
    .filter(visible)
    .filter((item) => !skipTexts.has(textOf(item)));
  const target = items.find((item) => item.getAttribute("aria-checked") === "false" && workspacePattern.test(textOf(item))) ||
    items.find((item) => workspacePattern.test(textOf(item))) ||
    items.find((item) => item.getAttribute("aria-checked") === "false");
  const candidates = items.map(textOf).filter(Boolean).slice(0, 12);
  if (!target) {
    return { ok: false, summary: `workspace radio not visible; candidates=${JSON.stringify(candidates)}` };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    text: textOf(target),
  };
}
"""


_WORKSPACE_READY_TARGET_SCRIPT = """
() => {
  // WORKSPACE_READY_TARGET
  const textOf = (el) => String(
    el && (el.innerText || el.textContent || el.getAttribute("aria-label")) || ""
  ).replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const profile = document.querySelector('[data-testid="accounts-profile-button"]');
  const profileText = textOf(profile);
  if (visible(profile) && /Workspace/i.test(profileText) && !/Personal|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237/.test(profileText)) {
    return { ok: true, source: "profile", text: profileText };
  }
  const welcomePattern = /Welcome\\s+to\\s+.+\\s+workspace/i;
  const nodes = Array.from(document.querySelectorAll("div, main, section, h1, h2, p, [role='status']"));
  const target = nodes.find((el) => visible(el) && welcomePattern.test(textOf(el)));
  if (target) {
    return { ok: true, source: "welcome", text: textOf(target) };
  }
  const candidates = nodes
    .filter((el) => visible(el))
    .map(textOf)
    .filter((text) => text && /Workspace|Welcome/i.test(text))
    .slice(0, 8);
  return { ok: false, summary: `workspace ready signal not visible; candidates=${JSON.stringify(candidates)}` };
}
"""

_ONBOARDING_DISMISS_TARGET_SCRIPT = """
() => {
  // ONBOARDING_DISMISS_TARGET
  const textOf = (el) => String(
    el && (el.innerText || el.textContent || el.getAttribute("aria-label")) || ""
  ).replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const normalized = (value) => String(value || "").replace(/[’‘`]/g, "'").replace(/\\s+/g, " ").trim();
  const dismissPattern = /^(Continue|Okay,? let's go|Got it|Skip|Skip Tour|Next|Done|Copy|继续|好的|知道了|跳过|跳过导览|下一步|完成)$/i;
  const candidates = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
  const preferredTexts = [
    "Skip Tour",
    "跳过导览",
    "Skip",
    "跳过",
    "Continue",
    "Okay, let's go",
    "Okay let's go",
    "Got it",
    "Done",
    "Copy",
    "Next",
    "下一步",
    "继续",
    "好的",
    "知道了",
    "完成",
  ];
  const byText = (wanted) => candidates.find((button) => normalized(textOf(button)).toLowerCase() === wanted.toLowerCase());
  const target = preferredTexts.map(byText).find(Boolean) ||
    candidates.find((button) => dismissPattern.test(normalized(textOf(button))));
  if (!target) {
    return {
      ok: false,
      summary: `no onboarding button; candidates=${JSON.stringify(candidates.map(textOf).filter(Boolean).slice(0, 8))}`,
    };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    text: textOf(target),
  };
}
"""


def _safe_log(log, message: str) -> None:
    if callable(log):
        try:
            log(message)
        except Exception:
            pass


def _page_wait(page, ms: int) -> None:
    try:
        page.wait_for_timeout(int(ms))
    except Exception:
        time.sleep(max(int(ms), 0) / 1000)


def _remaining_ms(deadline: float, minimum: int = 1000) -> int:
    return max(int(minimum), int((deadline - time.monotonic()) * 1000))


def _wait_for_dom_target(
    page,
    script: str,
    *,
    label: str,
    timeout_ms: int,
    arg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    last_summary = ""
    while time.monotonic() <= deadline:
        try:
            result = page.evaluate(script, arg) if arg is not None else page.evaluate(script)
        except Exception as exc:
            if _looks_like_navigation_interrupt(exc):
                raise
            last_summary = str(exc)[:200]
            result = None

        if isinstance(result, dict) and result.get("ok"):
            return result
        if isinstance(result, dict):
            last_summary = str(result.get("summary") or result)[:240]
        elif result is not None:
            return {"ok": False, "summary": f"{label}: unexpected result {str(result)[:120]}"}
        _page_wait(page, 150)

    if last_summary:
        return {"ok": False, "summary": f"{label}: {last_summary}"}
    return None


def _click_dom_target(page, target: dict[str, Any], *, label: str, log=None) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")
    x = float(target.get("x"))
    y = float(target.get("y"))
    mouse.click(x, y)
    _safe_log(log, f"Workspace Join: clicked {label}: {target.get('text') or '-'}")


def _wait_for_clickable_dom_target(
    page,
    script: str,
    *,
    label: str,
    timeout_ms: int,
    log=None,
) -> dict[str, Any]:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")

    result = _wait_for_dom_target(page, script, label=label, timeout_ms=timeout_ms)
    if isinstance(result, dict) and result.get("ok"):
        _click_dom_target(page, result, label=label, log=log)
        return result
    last_summary = str((result or {}).get("summary") if isinstance(result, dict) else result or "")[:240]

    raise RuntimeError(f"timed out waiting for {label}: {last_summary}")


def _move_mouse_to_target(page, target: dict[str, Any]) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "move"):
        return
    try:
        mouse.move(float(target.get("x")), float(target.get("y")))
    except Exception:
        pass


def _click_until_next_dom_target(
    page,
    *,
    click_script: str,
    next_script: str,
    click_label: str,
    next_label: str,
    timeout_ms: int,
    log=None,
    move_after_click: bool = False,
    post_click_wait_ms: int = 250,
    next_arg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")

    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    attempts = 0
    last_summary = ""

    while time.monotonic() <= deadline:
        next_probe_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 350)
        next_target = _wait_for_dom_target(
            page,
            next_script,
            label=next_label,
            timeout_ms=next_probe_timeout,
            arg=next_arg,
        )
        if isinstance(next_target, dict) and next_target.get("ok"):
            next_target["clickAttempts"] = attempts
            return next_target
        if isinstance(next_target, dict):
            last_summary = str(next_target.get("summary") or next_target)[:240]

        click_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 700)
        click_target = _wait_for_dom_target(
            page,
            click_script,
            label=click_label,
            timeout_ms=click_timeout,
        )
        if not isinstance(click_target, dict) or not click_target.get("ok"):
            last_summary = str((click_target or {}).get("summary") if isinstance(click_target, dict) else click_target or "")[:240]
            _safe_log(log, f"Workspace Join: {click_label} not visible; keep waiting for {next_label}")
            _page_wait(page, 250)
            continue

        attempts += 1
        _click_dom_target(page, click_target, label=click_label, log=log)
        if move_after_click:
            _move_mouse_to_target(page, click_target)
        _page_wait(page, post_click_wait_ms)

        next_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 900)
        next_target = _wait_for_dom_target(
            page,
            next_script,
            label=next_label,
            timeout_ms=next_timeout,
            arg=next_arg,
        )
        if isinstance(next_target, dict) and next_target.get("ok"):
            next_target["clickAttempts"] = attempts
            return next_target
        if isinstance(next_target, dict):
            last_summary = str(next_target.get("summary") or next_target)[:240]
        dismissed = _dismiss_chatgpt_onboarding(page, timeout_ms=200, log=log)
        if dismissed:
            _safe_log(log, f"Workspace Join: dismissed onboarding while waiting for {next_label}; retrying")
            continue
        _safe_log(log, f"Workspace Join: {next_label} not visible after {click_label} click, retrying")

    raise RuntimeError(
        f"timed out waiting for {next_label} after clicking {click_label}: {last_summary}"
    )


def _wait_workspace_ready_after_click(page, timeout_ms: int) -> dict[str, Any]:
    ready = _wait_for_dom_target(
        page,
        _WORKSPACE_READY_TARGET_SCRIPT,
        label="workspace ready signal",
        timeout_ms=timeout_ms,
    )
    if isinstance(ready, dict) and ready.get("ok"):
        return ready
    summary = str((ready or {}).get("summary") if isinstance(ready, dict) else ready or "")[:240]
    raise RuntimeError(f"timed out waiting for workspace ready signal: {summary}")


def _dismiss_chatgpt_onboarding(page, *, timeout_ms: int = 2500, log=None) -> int:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    dismissed = 0
    while time.monotonic() <= deadline and dismissed < 6:
        target = _wait_for_dom_target(
            page,
            _ONBOARDING_DISMISS_TARGET_SCRIPT,
            label="onboarding dismiss button",
            timeout_ms=min(_remaining_ms(deadline), 500),
        )
        if not isinstance(target, dict) or not target.get("ok"):
            break
        _click_dom_target(page, target, label="onboarding button", log=log)
        dismissed += 1
        _page_wait(page, 350)
    if dismissed:
        _safe_log(log, f"Workspace Join: dismissed {dismissed} onboarding step(s)")
    return dismissed


def _reload_and_confirm_workspace_session(
    page,
    *,
    workspace_id: str,
    timeout_ms: int = 8000,
    log=None,
) -> dict[str, Any]:
    _dismiss_chatgpt_onboarding(page, timeout_ms=2500, log=log)
    try:
        page.reload(wait_until="domcontentloaded", timeout=8000)
        _safe_log(log, "Workspace Join: reloaded ChatGPT after workspace switch")
    except Exception as exc:
        _safe_log(log, f"Workspace Join: reload after workspace switch failed, continue confirming: {exc}")
    _dismiss_chatgpt_onboarding(page, timeout_ms=2500, log=log)
    return _wait_session_workspace_selected(
        page,
        workspace_id=workspace_id,
        timeout_ms=timeout_ms,
        log=log,
    )


def _switch_workspace_via_profile_menu_until_confirmed(
    page,
    *,
    workspace_id: str,
    timeout_ms: int = 45000,
    log=None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    skipped_texts: list[str] = []
    last_error = ""
    attempts = 0
    while time.monotonic() <= deadline and attempts < 6:
        attempts += 1
        switch_result = switch_chatgpt_workspace_via_profile_menu(
            page,
            workspace_id=workspace_id,
            timeout_ms=min(_remaining_ms(deadline), 12000),
            log=log,
            skip_workspace_texts=skipped_texts,
        )
        selected_text = str(switch_result.get("selectedText") or "").strip()
        confirmed = _reload_and_confirm_workspace_session(
            page,
            workspace_id=workspace_id,
            timeout_ms=min(_remaining_ms(deadline), 8000),
            log=log,
        )
        if isinstance(confirmed, dict) and confirmed.get("ok") and isinstance(confirmed.get("session"), dict):
            switch_result["sessionConfirmed"] = True
            switch_result["session"] = dict(confirmed["session"])
            switch_result["account_id"] = str(confirmed.get("account_id") or "")
            switch_result["readySource"] = str(confirmed.get("source") or switch_result.get("readySource") or "")
            return switch_result

        last_error = str(confirmed.get("error") if isinstance(confirmed, dict) else confirmed or "")
        if selected_text and selected_text not in skipped_texts:
            skipped_texts.append(selected_text)
            _safe_log(
                log,
                "Workspace Join: selected workspace item did not match target; "
                f"skip next time: {selected_text} ({last_error})",
            )
            continue
        break
    raise RuntimeError(f"workspace session confirmation failed: {last_error or 'not selected'}")


def _recover_workspace_ready_after_navigation(
    page,
    *,
    workspace_id: str = "",
    timeout_ms: int = 45000,
) -> dict[str, Any]:
    try:
        ready = _wait_workspace_ready_after_click(page, int(timeout_ms))
        return {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": str(ready.get("text") or ""),
            "readySource": str(ready.get("source") or ""),
            "navigationRecovered": True,
        }
    except Exception:
        profile_text = _profile_workspace_text(page, int(timeout_ms))
        return {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": profile_text,
            "readySource": "profile",
            "navigationRecovered": True,
        }


def _wait_profile_workspace_after_click(page, timeout_ms: int) -> str:
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            return _profile_workspace_text(page, int(timeout_ms))
        except Exception as exc:
            last_exc = exc
            if not _looks_like_navigation_interrupt(exc):
                raise
            _page_wait(page, 1000)
    if last_exc is not None:
        raise last_exc
    return ""


def _visible_workspace_menu_debug(page) -> str:
    try:
        data = page.evaluate(
            """
            () => {
              const textOf = (el) => String(el?.innerText || el?.textContent || el?.getAttribute?.("aria-label") || "").replace(/\\s+/g, " ").trim();
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };
              return Array.from(document.querySelectorAll('button, a, [role="button"], [role="menuitem"], [role="menuitemradio"]'))
                .filter(visible)
                .map((el) => ({
                  role: el.getAttribute("role") || el.tagName.toLowerCase(),
                  testid: el.getAttribute("data-testid") || "",
                  text: textOf(el).slice(0, 80),
                }))
                .filter((item) => item.text || item.testid)
                .slice(0, 20);
            }
            """
        )
        return json.dumps(data, ensure_ascii=False)[:1200]
    except Exception as exc:
        return f"debug failed: {exc}"


def _switch_workspace_via_profile_menu_stepwise(
    page,
    *,
    workspace_id: str = "",
    timeout_ms: int = 45000,
    log=None,
    skip_workspace_texts: list[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    _click_until_next_dom_target(
        page,
        click_script=_PROFILE_MENU_TARGET_SCRIPT,
        next_script=_ACCOUNT_SUBMENU_TARGET_SCRIPT,
        click_label="profile button",
        next_label="account submenu",
        timeout_ms=_remaining_ms(deadline),
        log=log,
        post_click_wait_ms=300,
    )

    workspace = _wait_for_dom_target(
        page,
        _WORKSPACE_RADIO_TARGET_SCRIPT,
        label="workspace radio item",
        timeout_ms=min(_remaining_ms(deadline), 250),
        arg={"skipTexts": skip_workspace_texts or []} if skip_workspace_texts else None,
    )
    if not isinstance(workspace, dict) or not workspace.get("ok"):
        _safe_log(log, "Workspace Join: workspace radio not visible directly; opening account submenu")
        workspace = _click_until_next_dom_target(
            page,
            click_script=_ACCOUNT_SUBMENU_TARGET_SCRIPT,
            next_script=_WORKSPACE_RADIO_TARGET_SCRIPT,
            click_label="account submenu",
            next_label="workspace radio item",
            timeout_ms=_remaining_ms(deadline),
            log=log,
            move_after_click=True,
            post_click_wait_ms=350,
            next_arg={"skipTexts": skip_workspace_texts or []} if skip_workspace_texts else None,
        )

    _click_dom_target(page, workspace, label="workspace radio item", log=log)
    _page_wait(page, 800)
    confirmed = _wait_session_workspace_selected(
        page,
        workspace_id=workspace_id,
        timeout_ms=min(_remaining_ms(deadline), 1200),
        log=log,
    )
    if isinstance(confirmed, dict) and confirmed.get("ok"):
        return {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": str(workspace.get("text") or ""),
            "profileText": str(confirmed.get("account_id") or ""),
            "readySource": str(confirmed.get("source") or "session_api"),
            "stepwise": True,
            "sessionConfirmed": True,
            "session": dict(confirmed.get("session") or {}),
            "account_id": str(confirmed.get("account_id") or ""),
        }
    ready: dict[str, Any] = {}
    _wait_for_dom_target(
        page,
        _WORKSPACE_RADIO_TARGET_SCRIPT,
        label="workspace radio item",
        timeout_ms=250,
        arg={"skipTexts": skip_workspace_texts or []} if skip_workspace_texts else None,
    )
    try:
        ready = _wait_workspace_ready_after_click(page, min(_remaining_ms(deadline, minimum=1000), 3000))
    except Exception as exc:
        if _looks_like_navigation_interrupt(exc):
            recovered = _recover_workspace_ready_after_navigation(
                page,
                workspace_id=workspace_id,
                timeout_ms=min(_remaining_ms(deadline, minimum=1000), 5000),
            )
            recovered["selectedText"] = str(workspace.get("text") or recovered.get("selectedText") or "")
            recovered["stepwise"] = True
            recovered["sessionConfirmed"] = False
            return recovered
        _safe_log(log, f"Workspace Join: workspace ready signal not stable after click; will confirm by session: {exc}")
    return {
        "ok": True,
        "workspaceId": str(workspace_id or "").strip(),
        "selectedText": str(workspace.get("text") or ""),
        "profileText": str(ready.get("text") or ""),
        "readySource": str(ready.get("source") or ""),
        "stepwise": True,
        "sessionConfirmed": False,
    }

def switch_chatgpt_workspace_via_profile_menu(
    page,
    *,
    workspace_id: str = "",
    timeout_ms: int = 12000,
    log=None,
    skip_workspace_texts: list[str] | None = None,
) -> dict[str, Any]:
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)
    try:
        existing_profile_text = _profile_workspace_text(page, min(int(timeout_ms), 2500))
    except Exception:
        existing_profile_text = ""
    if existing_profile_text:
        if workspace_id:
            confirmed = _wait_session_workspace_selected(
                page,
                workspace_id=workspace_id,
                timeout_ms=min(int(timeout_ms), 2500),
                log=log,
            )
            if not (isinstance(confirmed, dict) and confirmed.get("ok")):
                _safe_log(
                    log,
                    "Workspace Join: profile already shows a workspace but session is not target; "
                    f"continue switching ({confirmed.get('error') if isinstance(confirmed, dict) else confirmed})",
                )
                existing_profile_text = ""
            else:
                result = {
                    "ok": True,
                    "workspaceId": str(workspace_id or "").strip(),
                    "selectedText": "",
                    "profileText": existing_profile_text,
                    "alreadyWorkspace": True,
                    "sessionConfirmed": True,
                    "session": dict(confirmed.get("session") or {}),
                    "account_id": str(confirmed.get("account_id") or ""),
                    "readySource": str(confirmed.get("source") or "session_api"),
                }
                if callable(log):
                    try:
                        log(f"Workspace Join: already in target workspace {existing_profile_text}")
                    except Exception:
                        pass
                return result
        if existing_profile_text:
            result = {
                "ok": True,
                "workspaceId": str(workspace_id or "").strip(),
                "selectedText": "",
                "profileText": existing_profile_text,
                "alreadyWorkspace": True,
            }
            if callable(log):
                try:
                    log(f"Workspace Join: already in workspace {existing_profile_text}")
                except Exception:
                    pass
            return result
    _dismiss_chatgpt_onboarding(page, timeout_ms=min(int(timeout_ms), 3500), log=log)
    try:
        result = _switch_workspace_via_profile_menu_stepwise(
            page,
            workspace_id=workspace_id,
            timeout_ms=timeout_ms,
            log=log,
            skip_workspace_texts=skip_workspace_texts,
        )
        if isinstance(result, dict) and result.get("ok"):
            if callable(log):
                try:
                    log(
                        "Workspace Join: switched profile menu to "
                        f"{result.get('profileText') or result.get('selectedText') or workspace_id}"
                    )
                except Exception:
                    pass
            return result
    except Exception as stepwise_exc:
        if _looks_like_navigation_interrupt(stepwise_exc):
            result = _recover_workspace_ready_after_navigation(
                page,
                workspace_id=workspace_id,
                timeout_ms=timeout_ms,
            )
            if callable(log):
                try:
                    log(
                        "Workspace Join: switched profile menu to "
                        f"{result.get('profileText') or result.get('selectedText') or workspace_id}"
                    )
                except Exception:
                    pass
            return result
        _safe_log(log, f"Workspace Join: stepwise profile menu switch failed, fallback: {stepwise_exc}")
        _safe_log(log, f"Workspace Join: visible menu debug: {_visible_workspace_menu_debug(page)}")
    try:
        result = page.evaluate(
            """
            async ({ workspaceId, timeoutMs }) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const text = (el) => String(el?.innerText || el?.textContent || el?.getAttribute?.("aria-label") || "").trim();
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };
              const fire = (el, type) => {
                el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
              };
              const clickLikeUser = (el) => {
                for (const type of ["pointerover", "pointerenter", "mouseover", "mouseenter", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                  fire(el, type);
                }
              };
              const waitFor = async (fn, label) => {
                const end = Date.now() + timeoutMs;
                let last;
                while (Date.now() < end) {
                  last = fn();
                  if (last) return last;
                  await sleep(150);
                }
                throw new Error(`Timed out waiting for ${label}`);
              };
              const profileButton = () => document.querySelector('[data-testid="accounts-profile-button"]');

              const profile = await waitFor(() => {
                const candidate = profileButton();
                return visible(candidate) ? candidate : null;
              }, "profile button");
              clickLikeUser(profile);

              const submenuTrigger = await waitFor(() => {
                const items = Array.from(document.querySelectorAll('[role="menuitem"][data-has-submenu], [role="menuitem"][aria-haspopup="menu"]')).filter(visible);
                return items.find((item) => /Personal|Account|帐户|账户/.test(text(item))) || items[0] || null;
              }, "account submenu");
              clickLikeUser(submenuTrigger);
              fire(submenuTrigger, "mousemove");
              await sleep(350);

              const radioItems = await waitFor(() => {
                const items = Array.from(document.querySelectorAll('[role="menuitemradio"]')).filter(visible);
                return items.length ? items : null;
              }, "workspace radio items");
              const target = radioItems.find((item) => item.getAttribute("aria-checked") === "false" && /Workspace/i.test(text(item))) ||
                radioItems.find((item) => item.getAttribute("aria-checked") === "false") ||
                radioItems.find((item) => /Workspace/i.test(text(item)));
              if (!target) {
                throw new Error("No selectable workspace item found");
              }
              const selectedText = text(target);
              clickLikeUser(target);

              const profileText = await waitFor(() => {
                const current = profileButton();
                const currentText = text(current);
                if (!currentText) return "";
                if (selectedText && currentText.includes(selectedText)) return currentText;
                if (/Workspace/i.test(currentText) && !/Personal|个人帐户|个人账户/.test(currentText)) return currentText;
                return "";
              }, "profile button workspace text");
              return { ok: true, workspaceId, selectedText, profileText };
            }
            """,
            {"workspaceId": str(workspace_id or "").strip(), "timeoutMs": int(timeout_ms)},
        )
    except Exception as exc:
        if not _looks_like_navigation_interrupt(exc):
            raise
        profile_text = _profile_workspace_text(page, int(timeout_ms))
        result = {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": profile_text,
            "navigationRecovered": True,
        }
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"workspace switch failed: {result}")
    if callable(log):
        try:
            log(f"Workspace Join: switched profile menu to {result.get('profileText') or result.get('selectedText') or workspace_id}")
        except Exception:
            pass
    return result


def export_workspace_cpa_session_from_browser(
    page,
    *,
    workspace_id: str = "",
    output_dir: str | Path | None = None,
    now: datetime | None = None,
    use_auth_workspace_select: bool = False,
    log=None,
) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    session_url = f"{CHATGPT_APP}/api/auth/session"

    api_switch_result: dict[str, Any] = {}
    session_json: dict[str, Any] | None = None
    if workspace_id:
        if use_auth_workspace_select:
            auth_select_result = _select_workspace_session_via_auth(page, workspace_id=workspace_id, log=log)
            if auth_select_result.get("ok") and isinstance(auth_select_result.get("session"), dict):
                session_json = dict(auth_select_result["session"])
                api_switch_result = {"method": str(auth_select_result.get("method") or "auth_workspace_select")}
                _safe_log(log, "Workspace Join: using auth workspace/select session")
            else:
                _safe_log(
                    log,
                    "Workspace Join: auth workspace/select failed, fallback to session probe: "
                    f"{auth_select_result.get('error') or 'not selected'}",
                )
        else:
            _safe_log(log, "Workspace Join: skip auth workspace/select; using session/UI switch path")

        if session_json is None:
            api_switch_result = _try_switch_workspace_via_api(page, workspace_id=workspace_id, log=log)
        if api_switch_result.get("ok") and isinstance(api_switch_result.get("session"), dict):
            session_json = dict(api_switch_result["session"])
            _safe_log(log, f"Workspace Join: using API switched session ({api_switch_result.get('method')})")
        elif session_json is None:
            _safe_log(log, f"Workspace Join: API switch failed, fallback to UI: {api_switch_result.get('error') or 'not selected'}")
            ui_result = _switch_workspace_via_profile_menu_until_confirmed(
                page,
                workspace_id=workspace_id,
                timeout_ms=45000,
                log=log,
            )
            session_json = dict(ui_result["session"])
            api_switch_result = {"method": str(ui_result.get("readySource") or "ui_session_confirmed")}

    if session_json is None:
        page.goto(session_url, wait_until="domcontentloaded", timeout=60000)
        session_json = _read_json_body_from_page(page)
    cpa_json = convert_chatgpt_session_to_cpa_json(session_json, now=now)
    account_id = str(cpa_json.get("account_id") or "").strip()
    if workspace_id and not _workspace_id_matches(account_id, workspace_id):
        raise RuntimeError(
            "workspace account_id mismatch: "
            f"target={workspace_id}, session_account_id={account_id or '-'}"
        )
    export_email = str(cpa_json.get("email") or "")
    if workspace_id:
        export_email = f"{export_email}-{workspace_id[:8]}"
    path = save_cpa_json_locally(
        cpa_json,
        email=export_email,
        output_dir=output_dir,
        now=now,
    )
    if callable(log):
        try:
            log(f"Workspace Join: CPA JSON saved to {path}")
        except Exception:
            pass
    return {
        "ok": True,
        "path": str(path),
        "workspace_id": workspace_id,
        "session_url": session_url,
        "switch_method": str(api_switch_result.get("method") or "ui" if workspace_id else "session"),
        "email": cpa_json.get("email", ""),
        "account_id": cpa_json.get("account_id", ""),
        "expired": cpa_json.get("expired", ""),
        "access_token": cpa_json.get("access_token", ""),
        "refresh_token": cpa_json.get("refresh_token", ""),
        "id_token": cpa_json.get("id_token", ""),
        "session_token": cpa_json.get("session_token", ""),
    }
