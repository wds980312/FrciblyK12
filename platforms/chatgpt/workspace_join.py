from __future__ import annotations

import time
import uuid
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from core.base_mailbox import MailboxAccount
from .cpa_session import export_workspace_cpa_session_from_browser
from .constants import CHATGPT_APP


DEFAULT_WORKSPACE_IDS = (
    "ff598c4d-ccaf-40c1-bfaa-cb94565764b1\n"
    "cf8e512d-1f3b-4603-950c-3d9758a8b435\n"
    "47336c9d-7607-4478-b37c-018049af1e46\n"
    "59208eb6-ec43-4d87-9289-dbd9e250bdd6\n"
    "2c82c020-e1bc-4363-9502-a6794405f793\n"
    "9901799e-e832-48b1-9278-9abe73168708\n"
    "c72dcdb4-63a0-40b7-b0bb-ccce3ca54984\n"
    "2b636e76-a87b-4222-b536-2dc4a545109f\n"
    "4779b1d7-3109-4ecb-957f-80262f4d7161\n"
    "ae67aa09-f3d3-4895-977d-9ca44ed1d996\n"
    "6daa08c1-59c8-4e06-9bc8-9d7246a63057\n"
    "521ffc8f-9612-4950-84ed-95773138eca6"
)

DEFAULT_ALIAS_FIRST_ONLY_WORKSPACE_IDS = "cf8e512d-1f3b-4603-950c-3d9758a8b435"


def parse_workspace_ids(raw: Any) -> list[str]:
    text = str(raw or "").strip() or DEFAULT_WORKSPACE_IDS
    normalized = text.replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def _bool_config(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_config(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _workspace_id_matches_account(account_id: Any, workspace_id: Any) -> bool:
    account_id = str(account_id or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not account_id or not workspace_id:
        return False
    return account_id == workspace_id or account_id.startswith(workspace_id) or workspace_id.startswith(account_id)


def _extract_session_account_id_from_error(error: Any) -> str:
    text = str(error or "")
    match = re.search(r"(?:session_)?account_id=([0-9a-zA-Z_-]+)", text)
    return match.group(1).strip() if match else ""


def workspace_join_enabled(extra: dict[str, Any] | None) -> bool:
    cfg = dict((extra or {}).get("chatgpt_workspace_join") or {})
    return _bool_config((extra or {}).get("auto_chatgpt_workspace_join", cfg.get("enabled")), False)


def workspace_join_config(extra: dict[str, Any] | None) -> dict[str, Any]:
    source = dict((extra or {}).get("chatgpt_workspace_join") or {})
    source.setdefault("workspace_ids", (extra or {}).get("workspace_ids", DEFAULT_WORKSPACE_IDS))
    source.setdefault(
        "alias_first_only_workspace_ids",
        (extra or {}).get(
            "alias_first_only_workspace_ids",
            DEFAULT_ALIAS_FIRST_ONLY_WORKSPACE_IDS,
        ),
    )
    source.setdefault("enabled", workspace_join_enabled(extra))
    source.setdefault("route", "request")
    source.setdefault("accept_invite", True)
    source.setdefault("export_cpa_json", True)
    source.setdefault("cpa_output_dir", "")
    source.setdefault("interval_ms", 1500)
    source.setdefault("request_concurrency", 4)
    source.setdefault("max_retries", 3)
    source.setdefault("retry_backoff_ms", 1500)
    source.setdefault("export_retries", 2)
    source.setdefault("export_retry_backoff_ms", 2000)
    source.setdefault("export_method", "session")
    source.setdefault("use_auth_workspace_select", False)
    source.setdefault("invite_timeout", 240)
    return source


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if callable(log):
        try:
            log(message)
        except Exception:
            pass


def _is_playwright_target_closed(exc: Exception | str) -> bool:
    message = str(exc or "").lower()
    return any(
        token in message
        for token in (
            "target page, context or browser has been closed",
            "page.goto: target page",
            "page.evaluate: target page",
            "browser has been closed",
            "context has been closed",
            "page has been closed",
            "connection closed while reading from the driver",
        )
    )


def _ensure_chatgpt_origin(page, log: Callable[[str], None] | None) -> None:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" in current_url.lower():
        return
    _log(log, "Workspace Join: 当前页不在 chatgpt.com，先打开 ChatGPT 首页")
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000)


def _fetch_access_token_from_page(page, log: Callable[[str], None] | None) -> str:
    _ensure_chatgpt_origin(page, log)
    data = page.evaluate(
        """
        async () => {
          const response = await fetch("/api/auth/session", {
            headers: { accept: "*/*" },
            credentials: "include",
          });
          const text = await response.text().catch(() => "");
          let json = {};
          try { json = text ? JSON.parse(text) : {}; } catch (_) {}
          return {
            ok: response.ok,
            status: response.status,
            accessToken: json.accessToken || json.access_token || "",
            text: text.slice(0, 300),
          };
        }
        """
    )
    result = dict(data or {})
    token = str(result.get("accessToken") or "").strip()
    if token:
        _log(log, "Workspace Join: got accessToken from current ChatGPT page")
        return token
    raise RuntimeError(
        f"ChatGPT session did not return accessToken: HTTP {result.get('status')} "
        f"{str(result.get('text') or '')[:160]}"
    )


def request_workspace_join_in_browser(
    page,
    *,
    access_token: str = "",
    workspace_ids: list[str],
    route: str = "request",
    interval_ms: int = 1500,
    request_concurrency: int = 4,
    max_retries: int = 3,
    retry_backoff_ms: int = 1500,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    _ensure_chatgpt_origin(page, log)
    fallback_token = str(access_token or "").strip()
    try:
        access_token = _fetch_access_token_from_page(page, log)
    except Exception as exc:
        _log(log, f"Workspace Join: page session fetch failed, using registration token if available: {exc}")
        access_token = fallback_token
    if not access_token:
        raise RuntimeError("缺少 ChatGPT access_token，无法发送 workspace join request")

    results_by_workspace: dict[str, dict[str, Any]] = {}
    pending_ids = [str(item or "").strip() for item in workspace_ids if str(item or "").strip()]
    device_id = str(uuid.uuid4())
    normalized_route = str(route or "request").strip() or "request"
    max_attempts = max(int(max_retries), 0) + 1
    concurrency = min(max(int(request_concurrency or 1), 1), 8)

    for attempt in range(1, max_attempts + 1):
        if not pending_ids:
            break
        _log(
            log,
            "Workspace Join: 并发发送 join request "
            f"count={len(pending_ids)}, concurrency={concurrency}, attempt={attempt}",
        )
        batch_results = page.evaluate(
            """
            async ({ workspaceIds, route, token, deviceId, concurrency }) => {
              const ids = Array.isArray(workspaceIds) ? workspaceIds : [];
              const limit = Math.max(Number(concurrency) || 1, 1);
              const results = new Array(ids.length);
              let next = 0;
              async function send(wsId) {
                try {
                  const response = await fetch(`/backend-api/accounts/${wsId}/invites/${route}`, {
                    method: "POST",
                    credentials: "include",
                    mode: "cors",
                    headers: {
                      accept: "*/*",
                      authorization: `Bearer ${token}`,
                      "content-type": "application/json",
                      "oai-device-id": deviceId,
                      "oai-language": navigator.language || "en-US",
                    },
                    body: "",
                  });
                  const text = await response.text().catch(() => "");
                  return {
                    ok: response.ok,
                    status: response.status,
                    url: response.url,
                    text: text.slice(0, 500),
                    workspace_id: wsId,
                  };
                } catch (error) {
                  return {
                    ok: false,
                    status: 0,
                    url: "",
                    text: String(error && error.message || error || "").slice(0, 500),
                    workspace_id: wsId,
                  };
                }
              }
              async function worker() {
                while (next < ids.length) {
                  const index = next++;
                  results[index] = await send(ids[index]);
                }
              }
              const workers = Array.from({ length: Math.min(limit, ids.length) }, () => worker());
              await Promise.all(workers);
              return results;
            }
            """,
            {
                "workspaceIds": pending_ids,
                "route": normalized_route,
                "token": access_token,
                "deviceId": device_id,
                "concurrency": concurrency,
            },
        )
        next_pending: list[str] = []
        refresh_access_token = False
        for item in batch_results or []:
            last_result = dict(item or {})
            ws_id = str(last_result.get("workspace_id") or "").strip()
            if not ws_id:
                continue
            results_by_workspace[ws_id] = last_result
            if last_result.get("ok"):
                _log(log, f"Workspace Join: {ws_id[:8]} request 成功 HTTP {last_result.get('status')}")
                continue
            _log(
                log,
                f"Workspace Join: {ws_id[:8]} request 失败 HTTP {last_result.get('status')}: "
                f"{str(last_result.get('text') or '')[:180]}",
            )
            status = int(last_result.get("status") or 0)
            retryable = status == 0 or status in (401, 403, 408, 409, 425, 429) or status >= 500
            if retryable and attempt < max_attempts:
                next_pending.append(ws_id)
                if status in (401, 403):
                    refresh_access_token = True
        pending_ids = next_pending
        if pending_ids and attempt < max_attempts:
            if refresh_access_token:
                _log(log, "Workspace Join: accessToken rejected, refreshing session from page")
                try:
                    access_token = _fetch_access_token_from_page(page, log)
                except Exception as exc:
                    _log(log, f"Workspace Join: session refresh failed, retrying with previous token: {exc}")
            if concurrency <= 1 and interval_ms:
                time.sleep(max(int(interval_ms), 0) / 1000)
            time.sleep(max(int(retry_backoff_ms), 0) / 1000)
    return [results_by_workspace.get(ws_id, {"ok": False, "workspace_id": ws_id, "error": "not requested"}) for ws_id in workspace_ids]


def open_workspace_invite_in_browser(
    page,
    invite_url: str,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    url = str(invite_url or "").strip()
    if not url:
        return {"ok": False, "error": "empty invite url"}

    invite_workspace_id = _workspace_id_from_invite_url(url, [])
    _log(log, f"Workspace Join: open invite link wId={invite_workspace_id or '-'}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_timeout(1000)
    except Exception:
        time.sleep(1)

    clicked = False
    clicked_text = ""
    try:
        click_result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== "none" && style.visibility !== "hidden" &&
                  rect.width > 0 && rect.height > 0;
              };
              const pattern = /加入工作空间|加入工作區|转至\\s*ChatGPT\\s*for\\s*Teachers|轉至\\s*ChatGPT\\s*for\\s*Teachers|Go to\\s*ChatGPT\\s*for\\s*Teachers|Join workspace|Accept invite|Accept invitation|Continue/i;
              const nodes = Array.from(document.querySelectorAll("button, a, [role='button']"));
              const target = nodes.find((el) => visible(el) && pattern.test(String(el.innerText || el.textContent || el.getAttribute("aria-label") || "")));
              if (!target) return { clicked: false, text: "", url: location.href };
              const text = String(target.innerText || target.textContent || target.getAttribute("aria-label") || "").trim();
              target.click();
              return { clicked: true, text, url: location.href };
            }
            """
        )
        clicked = bool((click_result or {}).get("clicked"))
        clicked_text = str((click_result or {}).get("text") or "")
    except Exception as exc:
        _log(log, f"Workspace Join: 邀请页按钮点击探测失败，继续观察页面: {exc}")

    if not clicked:
        last_candidates: list[str] = []
        last_error = ""
        click_script = """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== "none" && style.visibility !== "hidden" &&
                  rect.width > 0 && rect.height > 0;
              };
              const textOf = (el) => String(
                el.innerText || el.textContent || el.getAttribute("aria-label") || ""
              ).replace(/\\s+/g, " ").trim();
              const pattern = /加入工作空间|加入工作區|转至\\s*ChatGPT\\s*for\\s*Teachers|轉至\\s*ChatGPT\\s*for\\s*Teachers|Go to\\s*ChatGPT\\s*for\\s*Teachers|ChatGPT\\s*for\\s*Teachers|Join workspace|Accept invite|Accept invitation|Continue/i;
              const nodes = Array.from(document.querySelectorAll("button, a, [role='button'], [role='link']"));
              const candidates = nodes
                .filter((el) => visible(el))
                .map((el) => textOf(el))
                .filter(Boolean)
                .slice(0, 12);
              const target = nodes.find((el) => visible(el) && pattern.test(textOf(el)));
              if (!target) return { clicked: false, text: "", candidates, url: location.href };
              const text = textOf(target);
              try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
              target.click();
              return { clicked: true, text, candidates, url: location.href };
            }
            """
        deadline = time.monotonic() + 30
        attempts = 0
        while attempts < 60 and time.monotonic() <= deadline:
            attempts += 1
            try:
                click_result = page.evaluate(click_script)
                clicked = bool((click_result or {}).get("clicked"))
                clicked_text = str((click_result or {}).get("text") or "")
                raw_candidates = (click_result or {}).get("candidates") or []
                if isinstance(raw_candidates, list):
                    last_candidates = [
                        str(item)[:80] for item in raw_candidates if str(item).strip()
                    ]
                if clicked:
                    break
            except Exception as exc:
                last_error = str(exc)
            if time.monotonic() > deadline:
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        if last_error and not clicked:
            _log(log, f"Workspace Join: invite button click probe failed: {last_error}")
        if last_candidates and not clicked:
            _log(log, f"Workspace Join: invite page button candidates: {last_candidates}")

    try:
        page.wait_for_timeout(2500 if clicked else 1200)
    except Exception:
        time.sleep(2.5 if clicked else 1.2)

    final_url = str(getattr(page, "url", "") or "")
    if clicked:
        _log(log, f"Workspace Join: 已点击邀请页按钮 {clicked_text or '-'}")
    if not clicked:
        _log(log, "Workspace Join: invite button not clicked; acceptance not confirmed")
    return {
        "ok": clicked,
        "invite_url": url,
        "clicked": clicked,
        "clicked_text": clicked_text,
        "final_url": final_url,
        "error": "" if clicked else "invite button not clicked",
    }


def _workspace_id_from_invite_url(invite_url: str, fallback_ids: list[str]) -> str:
    try:
        values = parse_qs(urlparse(str(invite_url or "")).query)
        for key in ("wId", "wid", "workspace_id", "workspaceId"):
            value = (values.get(key) or [""])[0]
            if value:
                return str(value)
    except Exception:
        pass
    return fallback_ids[0] if fallback_ids else ""


def _mailbox_alias_metadata(mailbox_account: MailboxAccount | None) -> dict[str, Any]:
    if mailbox_account is None:
        return {}
    extra = dict(getattr(mailbox_account, "extra", {}) or {})
    resources: list[Any] = []
    if isinstance(extra.get("provider_resource"), dict):
        resources.append(extra["provider_resource"])
    resources.extend(extra.get("provider_resources") or [])
    for item in resources:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if isinstance(metadata, dict) and metadata.get("alias_index") not in (None, ""):
            return dict(metadata)
    return {}


def _alias_index_from_mailbox(mailbox_account: MailboxAccount | None) -> int:
    metadata = _mailbox_alias_metadata(mailbox_account)
    try:
        return int(metadata.get("alias_index") or 0)
    except (TypeError, ValueError):
        return 0


def _canonical_email_root(email: str) -> tuple[str, str]:
    local, sep, domain = str(email or "").strip().lower().partition("@")
    if not sep:
        return "", ""
    return local.split("+", 1)[0], domain


def _is_confirmed_reused_alias_mailbox(mailbox_account: MailboxAccount | None) -> bool:
    metadata = _mailbox_alias_metadata(mailbox_account)
    try:
        alias_index = int(metadata.get("alias_index") or 0)
    except (TypeError, ValueError):
        return False
    if alias_index <= 1:
        return False
    current_email = str(getattr(mailbox_account, "email", "") or metadata.get("email") or "").strip()
    base_email = str(metadata.get("base_email") or "").strip()
    if not current_email or not base_email or "+" not in current_email.partition("@")[0]:
        return False
    current_root, current_domain = _canonical_email_root(current_email)
    base_root, base_domain = _canonical_email_root(base_email)
    return bool(current_root and current_root == base_root and current_domain == base_domain)


def _apply_alias_workspace_policy(
    workspace_ids: list[str],
    *,
    mailbox_account: MailboxAccount | None,
    config: dict[str, Any],
    log: Callable[[str], None] | None = None,
) -> tuple[list[str], list[str]]:
    if not _is_confirmed_reused_alias_mailbox(mailbox_account):
        return workspace_ids, []
    alias_index = _alias_index_from_mailbox(mailbox_account)
    restricted = set(parse_workspace_ids(config.get("alias_first_only_workspace_ids")))
    if not restricted:
        return workspace_ids, []
    allowed: list[str] = []
    skipped: list[str] = []
    for workspace_id in workspace_ids:
        if workspace_id in restricted:
            skipped.append(workspace_id)
        else:
            allowed.append(workspace_id)
    if skipped:
        metadata = _mailbox_alias_metadata(mailbox_account)
        base_email = str(metadata.get("base_email") or "").strip()
        _log(
            log,
            "Workspace Join: alias policy 跳过同根邮箱复用受限空间 "
            f"alias_index={alias_index}, base={base_email or '-'}, "
            f"workspace={', '.join(item[:8] for item in skipped)}",
        )
    return allowed, skipped


def _export_workspace_cpa_session_via_codex_oauth(
    page,
    *,
    session_info: dict[str, Any],
    workspace_id: str,
    output_dir: str | None = None,
    proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from .browser_register import _do_codex_oauth, _get_cookies
    from .constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE
    from .cpa_session import convert_chatgpt_session_to_cpa_json, save_cpa_json_locally
    from .oauth import generate_oauth_url

    email = str(session_info.get("email") or "").strip()
    password = str(session_info.get("password") or "").strip()
    oauth_start = generate_oauth_url(
        redirect_uri=CODEX_REDIRECT_URI,
        scope=CODEX_SCOPE,
        client_id=CODEX_CLIENT_ID,
    )
    _log(log, f"Workspace Join: Codex OAuth 选择 workspace {workspace_id[:8]} 并导出 CPA JSON")
    oauth_result = _do_codex_oauth(
        page,
        _get_cookies(page),
        email,
        password,
        None,
        None,
        proxy,
        log,
        oauth_start=oauth_start,
        target_workspace_id=workspace_id,
    )
    if not isinstance(oauth_result, dict) or not oauth_result.get("access_token"):
        raise RuntimeError(f"OAuth did not return access_token: {oauth_result}")

    cpa_json = convert_chatgpt_session_to_cpa_json(oauth_result)
    export_email = str(cpa_json.get("email") or email)
    path = save_cpa_json_locally(
        cpa_json,
        email=f"{export_email}-{workspace_id[:8]}",
        output_dir=output_dir,
    )
    result = {
        "ok": True,
        "path": str(path),
        "workspace_id": workspace_id,
        "method": "codex_oauth_workspace_select",
        **cpa_json,
    }
    _log(log, f"Workspace Join: CPA JSON saved to {path}")
    return result


def export_joined_workspace_cpa_sessions(
    page,
    *,
    config: dict[str, Any],
    session_info: dict[str, Any] | None = None,
    workspace_ids: list[str] | None = None,
    configured_workspace_ids: list[str] | None = None,
    alias_policy_skipped: list[str] | None = None,
    request_results: list[dict[str, Any]] | None = None,
    proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    workspace_ids = list(workspace_ids or parse_workspace_ids(config.get("workspace_ids")))
    configured_workspace_ids = list(configured_workspace_ids or workspace_ids)
    alias_policy_skipped = list(alias_policy_skipped or [])
    result: dict[str, Any] = {
        "ok": False,
        "workspace_ids": workspace_ids,
        "configured_workspace_ids": configured_workspace_ids,
        "alias_policy_skipped": alias_policy_skipped,
        "request_results": list(request_results or []),
        "invite_url": "",
        "accept_result": None,
        "cpa_export": None,
        "cpa_exports": [],
        "export_errors": [],
        "export_skipped": [],
    }
    top_level_updates: dict[str, Any] = {}

    if not workspace_ids:
        result["ok"] = True if alias_policy_skipped else False
        if alias_policy_skipped:
            result["warning"] = "all workspace ids skipped by alias policy"
        else:
            result["error"] = "no workspace ids"
        return {"workspace_join": result}

    if not _bool_config(config.get("export_cpa_json"), True):
        result["ok"] = True
        return {"workspace_join": result}

    export_errors: list[str] = []
    export_skipped: list[str] = []
    exported_account_ids: set[str] = set()
    export_retries = max(_int_config(config.get("export_retries"), 2), 0)
    export_backoff_seconds = max(_int_config(config.get("export_retry_backoff_ms"), 2000), 0) / 1000
    export_method = str(config.get("export_method") or "session").strip().lower()
    page_closed = False

    def _record_export(export_result: dict[str, Any], requested_workspace_id: str) -> None:
        account_id = str(export_result.get("account_id") or "").strip()
        if account_id in exported_account_ids:
            raise RuntimeError(
                "duplicate workspace account_id exported: "
                f"{account_id} for workspace {requested_workspace_id}"
            )

        safe_export = {
            key: value
            for key, value in export_result.items()
            if key not in {"access_token", "refresh_token", "id_token", "session_token"}
        }
        result["cpa_exports"].append(safe_export)
        exported_account_ids.add(account_id)
        if result["cpa_export"] is None:
            result["cpa_export"] = safe_export
        if not top_level_updates:
            for source_key, target_key in (
                ("access_token", "access_token"),
                ("refresh_token", "refresh_token"),
                ("id_token", "id_token"),
                ("session_token", "session_token"),
                ("account_id", "account_id"),
                ("expired", "expires_at"),
            ):
                value = export_result.get(source_key)
                if value not in (None, ""):
                    top_level_updates[target_key] = value
            top_level_updates["workspace_id"] = requested_workspace_id

    def _already_exported(workspace_id: str) -> bool:
        return any(
            _workspace_id_matches_account(account_id, workspace_id)
            for account_id in exported_account_ids
        )

    for workspace_id in workspace_ids:
        if page_closed:
            export_skipped.append(workspace_id)
            continue
        if _already_exported(workspace_id):
            _log(log, f"Workspace Join: {workspace_id[:8]} 已通过实际切换导出，跳过重复导出")
            continue

        last_error = ""
        for attempt in range(export_retries + 1):
            try:
                _log(
                    log,
                    f"Workspace Join: 切换工作空间 {workspace_id[:8]} "
                    f"并导出 CPA JSON (第 {attempt + 1} 次)",
                )
                output_dir = str(config.get("cpa_output_dir") or "").strip() or None
                if export_method in {"codex_oauth", "oauth", "k12_reg"}:
                    export_result = _export_workspace_cpa_session_via_codex_oauth(
                        page,
                        session_info=dict(session_info or {}),
                        workspace_id=workspace_id,
                        output_dir=output_dir,
                        proxy=proxy,
                        log=log,
                    )
                else:
                    export_result = export_workspace_cpa_session_from_browser(
                        page,
                        workspace_id=workspace_id,
                        output_dir=output_dir,
                        use_auth_workspace_select=_bool_config(
                            config.get("use_auth_workspace_select"),
                            False,
                        ),
                        log=log,
                    )
                if not isinstance(export_result, dict) or not export_result.get("ok"):
                    raise RuntimeError(f"unexpected export result: {export_result}")

                account_id = str(export_result.get("account_id") or "").strip()
                if not _workspace_id_matches_account(account_id, workspace_id):
                    raise RuntimeError(
                        "workspace account_id mismatch: "
                        f"target={workspace_id}, session_account_id={account_id or '-'}"
                    )
                _record_export(export_result, workspace_id)
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)
                if _is_playwright_target_closed(exc):
                    page_closed = True
                    break
                actual_account_id = _extract_session_account_id_from_error(last_error)
                actual_workspace_id = next(
                    (
                        item
                        for item in workspace_ids
                        if _workspace_id_matches_account(actual_account_id, item)
                    ),
                    "",
                )
                if (
                    actual_workspace_id
                    and actual_workspace_id != workspace_id
                    and actual_workspace_id not in exported_account_ids
                ):
                    try:
                        _log(
                            log,
                            "Workspace Join: 当前页面实际已切到 "
                            f"{actual_workspace_id[:8]}，先导出这个空间",
                        )
                        output_dir = str(config.get("cpa_output_dir") or "").strip() or None
                        if export_method in {"codex_oauth", "oauth", "k12_reg"}:
                            actual_export = _export_workspace_cpa_session_via_codex_oauth(
                                page,
                                session_info=dict(session_info or {}),
                                workspace_id=actual_workspace_id,
                                output_dir=output_dir,
                                proxy=proxy,
                                log=log,
                            )
                        else:
                            actual_export = export_workspace_cpa_session_from_browser(
                                page,
                                workspace_id=actual_workspace_id,
                                output_dir=output_dir,
                                use_auth_workspace_select=_bool_config(
                                    config.get("use_auth_workspace_select"),
                                    False,
                                ),
                                log=log,
                            )
                        if not isinstance(actual_export, dict) or not actual_export.get("ok"):
                            raise RuntimeError(f"unexpected export result: {actual_export}")
                        actual_export_account_id = str(actual_export.get("account_id") or "").strip()
                        if not _workspace_id_matches_account(
                            actual_export_account_id,
                            actual_workspace_id,
                        ):
                            raise RuntimeError(
                                "workspace account_id mismatch: "
                                f"target={actual_workspace_id}, "
                                f"session_account_id={actual_export_account_id or '-'}"
                            )
                        _record_export(actual_export, actual_workspace_id)
                        break
                    except Exception as actual_exc:
                        last_error = f"{last_error}; actual workspace export failed: {actual_exc}"
                if attempt < export_retries:
                    _log(
                        log,
                        f"Workspace Join: {workspace_id[:8]} 导出失败，准备重试: {last_error}",
                    )
                    time.sleep(export_backoff_seconds)

        if last_error:
            export_errors.append(f"{workspace_id}: {last_error}")
            _log(log, f"Workspace Join: {workspace_id[:8]} 导出失败，已跳过: {last_error}")

    result["ok"] = True
    result["export_errors"] = export_errors
    result["export_skipped"] = export_skipped
    if export_errors:
        result["export_partial_error"] = "Workspace Join switch/export partial failed: " + "; ".join(export_errors)
        _log(log, f"Workspace Join: {result['export_partial_error']}")
    if export_skipped:
        _log(log, f"Workspace Join: 页面已关闭，跳过剩余 workspace: {', '.join(item[:8] for item in export_skipped)}")
    if result["cpa_export"] is None and export_errors:
        result["cpa_export"] = {"ok": False, "error": "; ".join(export_errors)}

    return {"workspace_join": result, **top_level_updates}


def run_workspace_join_flow(
    page,
    session_info: dict[str, Any],
    *,
    mailbox,
    mailbox_account: MailboxAccount | None,
    config: dict[str, Any],
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    configured_workspace_ids = parse_workspace_ids(config.get("workspace_ids"))
    workspace_ids, alias_policy_skipped = _apply_alias_workspace_policy(
        configured_workspace_ids,
        mailbox_account=mailbox_account,
        config=config,
        log=log,
    )
    if not workspace_ids:
        if alias_policy_skipped:
            return {
                "workspace_join": {
                    "ok": True,
                    "workspace_ids": [],
                    "configured_workspace_ids": configured_workspace_ids,
                    "alias_policy_skipped": alias_policy_skipped,
                    "warning": "all workspace ids skipped by alias policy",
                    "request_results": [],
                    "cpa_exports": [],
                    "export_errors": [],
                    "export_skipped": [],
                }
            }
        return {"workspace_join": {"ok": False, "error": "no workspace ids"}}

    result: dict[str, Any] = {
        "ok": False,
        "workspace_ids": workspace_ids,
        "configured_workspace_ids": configured_workspace_ids,
        "alias_policy_skipped": alias_policy_skipped,
        "request_results": [],
        "invite_url": "",
        "accept_result": None,
        "cpa_export": None,
        "cpa_exports": [],
        "export_errors": [],
        "export_skipped": [],
    }
    top_level_updates: dict[str, Any] = {}

    before_ids = set()
    if mailbox is not None and mailbox_account is not None:
        try:
            before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
            _log(log, f"Workspace Join: 邮箱邀请基线 before_ids={len(before_ids)}")
        except Exception as exc:
            _log(log, f"Workspace Join: 邮箱基线读取失败，继续等待新邮件: {exc}")
    else:
        _log(log, "Workspace Join: 缺少 mailbox 上下文，将只发送 request，不自动收邀请")

    try:
        request_results = request_workspace_join_in_browser(
            page,
            access_token=str(session_info.get("access_token") or ""),
            workspace_ids=workspace_ids,
            route=str(config.get("route") or "request"),
            interval_ms=_int_config(config.get("interval_ms"), 1500),
            request_concurrency=_int_config(config.get("request_concurrency"), 4),
            max_retries=_int_config(config.get("max_retries"), 3),
            retry_backoff_ms=_int_config(config.get("retry_backoff_ms"), 1500),
            log=log,
        )
        result["request_results"] = request_results
        result["request_ok"] = all(bool(item.get("ok")) for item in request_results)
    except Exception as exc:
        result["error"] = f"workspace request failed: {exc}"
        return {"workspace_join": result}

    if not result.get("request_ok"):
        result["error"] = "workspace request failed"
        return {"workspace_join": result}

    if not _bool_config(config.get("accept_invite"), True):
        result["ok"] = True
        return {"workspace_join": result}

    # request 成功后账号已是 workspace 成员，逐个切团队并导出 CPA JSON，不再等邮件。
    return export_joined_workspace_cpa_sessions(
        page,
        config=config,
        session_info=session_info,
        workspace_ids=workspace_ids,
        configured_workspace_ids=configured_workspace_ids,
        alias_policy_skipped=alias_policy_skipped,
        request_results=result["request_results"],
        proxy=str(config.get("proxy") or "").strip() or None,
        log=log,
    )
