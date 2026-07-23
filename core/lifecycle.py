"""账号生命周期管理 — 定时检测、自动续期、过期预警。"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.account_graph import load_account_graphs, patch_account_graph
from core.base_platform import AccountStatus, RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.platform_accounts import build_platform_account
from core.registry import get
from platforms.chatgpt.sub2api_upload import probe_sub2api_account_model

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _utcnow_ts() -> int:
    return int(_utcnow().timestamp())


# ---------------------------------------------------------------------------
# Account validity check
# ---------------------------------------------------------------------------

def check_accounts_validity(
    *,
    platform: str = "",
    limit: int = 100,
    log_fn=None,
) -> dict[str, int]:
    """Check validity of active accounts. Returns {valid, invalid, error, skipped}."""
    log = log_fn or logger.info

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q.limit(limit)).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    # Only check accounts that are in an active lifecycle state
    active_statuses = {"registered", "trial", "subscribed"}
    targets = [
        a for a in accounts
        if graphs.get(int(a.id or 0), {}).get("lifecycle_status") in active_statuses
        # ChatGPT uses the independent gpt-5.5 probe below as its source of
        # truth, rather than the subscription endpoint used by other plugins.
        and (platform or a.platform != "chatgpt")
    ]

    results = {"valid": 0, "invalid": 0, "error": 0, "skipped": len(accounts) - len(targets)}
    for acc in targets:
        try:
            platform_cls = get(acc.platform)
            plugin = platform_cls(config=RegisterConfig())
            with Session(engine) as session:
                current = session.get(AccountModel, acc.id)
                if not current:
                    continue
                account_obj = build_platform_account(session, current)

            valid = plugin.check_valid(account_obj)
            with Session(engine) as session:
                model = session.get(AccountModel, acc.id)
                if model:
                    model.updated_at = _utcnow()
                    summary_updates = {"checked_at": _utcnow_iso(), "valid": valid}
                    if hasattr(plugin, "get_last_check_overview"):
                        summary_updates.update(plugin.get_last_check_overview() or {})
                    patch_account_graph(
                        session, model,
                        summary_updates=summary_updates,
                    )
                    session.add(model)
                    session.commit()
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
                log(f"  {acc.email} ({acc.platform}): 失效")
        except Exception as exc:
            results["error"] += 1
            log(f"  {acc.email} ({acc.platform}): 检测异常 {exc}")

    log(f"检测完成: 有效 {results['valid']}, 失效 {results['invalid']}, "
        f"异常 {results['error']}, 跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# Token auto-refresh (ChatGPT-specific for now, extensible)
# ---------------------------------------------------------------------------

def flag_expiring_trials(
    *,
    hours_warning: int = 48,
    log_fn=None,
) -> dict[str, int]:
    """Flag trial accounts that will expire within `hours_warning` hours."""
    log = log_fn or logger.info
    now_ts = _utcnow_ts()
    warning_ts = now_ts + hours_warning * 3600
    results = {"warned": 0, "expired": 0, "skipped": 0}

    with Session(engine) as session:
        overviews = session.exec(
            select(AccountOverviewModel)
            .where(AccountOverviewModel.lifecycle_status == "trial")
        ).all()

    for overview in overviews:
        summary = overview.get_summary()
        trial_end = int(summary.get("trial_end_time") or 0)
        if not trial_end:
            results["skipped"] += 1
            continue

        if trial_end < now_ts:
            # Already expired
            with Session(engine) as session:
                model = session.get(AccountModel, overview.account_id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        session, model,
                        lifecycle_status=AccountStatus.EXPIRED.value,
                        summary_updates={"expiry_warning": "expired"},
                    )
                    session.add(model)
                    session.commit()
            results["expired"] += 1
        elif trial_end < warning_ts:
            # Expiring soon
            hours_left = max(0, (trial_end - now_ts) // 3600)
            with Session(engine) as session:
                model = session.get(AccountModel, overview.account_id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        session, model,
                        summary_updates={
                            "expiry_warning": f"expiring_in_{hours_left}h",
                            "expiry_warning_hours": hours_left,
                        },
                    )
                    session.add(model)
                    session.commit()
            results["warned"] += 1
        else:
            results["skipped"] += 1

    log(f"过期预警: 已过期 {results['expired']}, 即将过期 {results['warned']}, "
        f"跳过 {results['skipped']}")
    return results


def _chatgpt_model_targets(account_ids: list[int] | None = None) -> list[tuple[int, str, str]]:
    """Load a stable probe snapshot, oldest registration first."""
    with Session(engine) as session:
        query = select(AccountModel).where(AccountModel.platform == "chatgpt")
        if account_ids is not None:
            query = query.where(AccountModel.id.in_(account_ids))
        accounts = session.exec(query.order_by(AccountModel.created_at, AccountModel.id)).all()
        graphs = load_account_graphs(session, [int(account.id or 0) for account in accounts if account.id])
    targets = []
    for account in accounts:
        account_id = int(account.id or 0)
        if not account_id:
            continue
        overview = dict(graphs.get(account_id, {}).get("overview") or {})
        targets.append((account_id, account.email, str(overview.get("model_probe_sub2api_account_id") or "")))
    return targets


def check_chatgpt_model_pool(
    *,
    account_ids: list[int] | None = None,
    max_workers: int = 5,
    log_fn=None,
) -> dict[str, int]:
    """Probe one stable ChatGPT account batch and persist its results.

    Network calls run concurrently, while graph updates and cleanup remain
    single-writer operations for SQLite stability.
    """
    log = log_fn or logger.info
    from application.accounts import AccountsService
    targets = _chatgpt_model_targets(account_ids)

    results = {"checked": 0, "valid": 0, "invalid": 0, "error": 0}
    probe_results: dict[int, dict[str, Any]] = {}
    worker_count = min(max(int(max_workers or 1), 1), 5, len(targets) or 1)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chatgpt-probe") as pool:
        futures = {
            pool.submit(
                probe_sub2api_account_model,
                email,
                sub2api_account_id=remote_id or None,
                timeout_seconds=15,
            ): (account_id, email)
            for account_id, email, remote_id in targets
        }
        for future in as_completed(futures):
            account_id, email = futures[future]
            try:
                probe_results[account_id] = future.result()
            except Exception as exc:
                probe_results[account_id] = {"status": "error", "message": str(exc)}

    with Session(engine) as session:
        graphs = load_account_graphs(session, [account_id for account_id, _, _ in targets])
        for account_id, email, _ in targets:
            probe = probe_results.get(account_id) or {"status": "error", "message": "未获得探测结果"}
            probe_status = str(probe.get("status") or "error")
            model = session.get(AccountModel, account_id)
            if not model:
                continue
            summary_updates = {
                "checked_at": _utcnow_iso(),
                "model_probe_model": "gpt-5.5",
                "model_probe_prompt": "hi",
                "model_probe_mode": "compact",
                "model_probe_status": probe_status,
                "model_probe_message": str(probe.get("message") or ""),
            }
            remote_id = probe.get("sub2api_account_id")
            if remote_id:
                summary_updates["model_probe_sub2api_account_id"] = str(remote_id)
            valid: bool | None = None
            if probe_status == "success":
                summary_updates.update({"model_probe_failure_count": 0, "valid": True})
                valid = True
                results["valid"] += 1
            elif probe_status == "failed":
                summary_updates.update({"model_probe_failure_count": 1, "valid": False})
                valid = False
                results["invalid"] += 1
                log(f"  {email}: 模型探测失效")
            else:
                results["error"] += 1
                log(f"  {email}: 模型探测异常 {summary_updates['model_probe_message']}")
            model.updated_at = _utcnow()
            patch_account_graph(session, model, summary_updates=summary_updates)
            session.add(model)
            results["checked"] += 1
        session.commit()

    cleanup = AccountsService().cleanup_invalid_chatgpt(include_sub2api=True)
    if cleanup.get("deleted_local") or cleanup.get("deleted_sub2api"):
        log(
            "ChatGPT 失效清理: "
            f"本地 {cleanup.get('deleted_local', 0)} / "
            f"Sub2API {cleanup.get('deleted_sub2api', 0)}"
        )
    log(
        "ChatGPT 模型巡检完成: "
        f"检测 {results['checked']}, 有效 {results['valid']}, "
        f"失效 {results['invalid']}, 异常 {results['error']}"
    )
    return results

# ---------------------------------------------------------------------------
# ChatGPT token refresh + CPA sync + liveness check
# ---------------------------------------------------------------------------

class LifecycleManager:
    """Runs periodic lifecycle tasks in a background thread."""

    def __init__(
        self,
        *,
        check_interval_hours: float = 6,
        warning_hours: int = 48,
    ):
        self.check_interval = check_interval_hours * 3600
        self.warning_hours = warning_hours
        self._running = False
        self._thread: threading.Thread | None = None
        self._chatgpt_thread: threading.Thread | None = None
        self._last_check = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lifecycle-manager")
        self._chatgpt_thread = threading.Thread(
            target=self._chatgpt_model_loop,
            daemon=True,
            name="chatgpt-model-monitor",
        )
        self._thread.start()
        self._chatgpt_thread.start()
        print("[LifecycleManager] 已启动")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            now = time.time()
            try:
                # Trial expiry warnings — run every cycle
                flag_expiring_trials(hours_warning=self.warning_hours)

                # Validity check
                if now - self._last_check >= self.check_interval:
                    print("[LifecycleManager] 开始账号有效性检测...")
                    check_accounts_validity()
                    self._last_check = now

            except Exception as exc:
                print(f"[LifecycleManager] 错误: {exc}")

            # Sleep in small increments so stop() is responsive
            for _ in range(60):
                if not self._running:
                    break
                time.sleep(1)

    def _chatgpt_model_loop(self):
        """Run oldest-first batches without blocking the registration queue."""
        while self._running:
            snapshot = [account_id for account_id, _, _ in _chatgpt_model_targets()]
            total = len(snapshot)
            if not total:
                self._sleep_chatgpt_monitor_interval()
                continue
            batch_size = max(1, -(-total // 5))
            worker_count = min(5, max(1, -(-batch_size // 3)))
            print(
                "[LifecycleManager] 开始 ChatGPT 模型巡检: "
                f"本轮 {total} 个，批次 {batch_size}，并发 {worker_count}"
            )
            for offset in range(0, total, batch_size):
                if not self._running:
                    break
                batch_ids = snapshot[offset:offset + batch_size]
                try:
                    check_chatgpt_model_pool(
                        account_ids=batch_ids,
                        max_workers=worker_count,
                        log_fn=print,
                    )
                except Exception as exc:
                    print(f"[LifecycleManager] ChatGPT 模型巡检错误: {exc}")
                if offset + batch_size < total:
                    self._sleep_chatgpt_monitor_interval()

            # Keep a stable cadence when a small pool finishes before five
            # batches have elapsed.
            if self._running:
                self._sleep_chatgpt_monitor_interval()

    def _sleep_chatgpt_monitor_interval(self):
        for _ in range(60):
            if not self._running:
                break
            time.sleep(1)


lifecycle_manager = LifecycleManager()
