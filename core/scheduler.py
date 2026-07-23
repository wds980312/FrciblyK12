"""定时任务调度 - 账号有效性检测、trial 到期提醒"""
from datetime import datetime, timezone

from sqlmodel import Session, select

from .account_graph import load_account_graphs, patch_account_graph
from .base_platform import AccountStatus, RegisterConfig
from .db import engine, AccountModel
from .platform_accounts import build_platform_account
from .registry import get, load_all
from .config_store import config_store
from application.tasks import (
    create_register_task,
    get_chatgpt_pool_status,
    has_queued_or_active_chatgpt_work,
)
from services.task_runtime import task_runtime
import threading
import time


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread = None
        self._last_trial_expiry_check = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[Scheduler] 已启动")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                now = time.monotonic()
                if now - self._last_trial_expiry_check >= 3600:
                    self.check_trial_expiry()
                    self._last_trial_expiry_check = now
                self.check_chatgpt_pool_maintenance()
            except Exception as e:
                print(f"[Scheduler] 错误: {e}")
            time.sleep(self._chatgpt_pool_interval_seconds())

    @staticmethod
    def _chatgpt_pool_interval_seconds() -> int:
        try:
            return max(int(config_store.get("chatgpt_pool_interval_seconds", "60") or 60), 60)
        except (TypeError, ValueError):
            return 60

    def check_chatgpt_pool_maintenance(self) -> bool:
        """Create registration work directly from the monitored pool state."""
        enabled = str(config_store.get("chatgpt_pool_maintenance_enabled", "") or "").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return False
        try:
            target = max(int(config_store.get("chatgpt_pool_target", "100") or 100), 1)
        except (TypeError, ValueError):
            target = 100
        try:
            concurrency = max(int(config_store.get("chatgpt_pool_registration_concurrency", "1") or 1), 1)
        except (TypeError, ValueError):
            concurrency = 1
        if has_queued_or_active_chatgpt_work():
            return False
        pool = get_chatgpt_pool_status()
        shortfall = max(target - int(pool.get("usable") or 0), 0)
        if not shortfall:
            return False
        batch_size = min(concurrency, shortfall)
        task = create_register_task(
            {
                "platform": "chatgpt",
                "count": batch_size,
                "concurrency": concurrency,
                "executor_type": "headless",
                "captcha_solver": "auto",
                "source": "pool_maintenance",
                "extra": {
                    "identity_provider": "mailbox",
                    "auto_upload_agent_identity_cpa": True,
                    "source": "pool_maintenance",
                },
            }
        )
        task_runtime.wake_up()
        print(
            f"[Scheduler] 号池 {pool.get('usable', 0)}/{target}，"
            f"已创建 {batch_size} 个注册任务: {task.get('id', '')}"
        )
        return True

    def check_trial_expiry(self):
        """检查 trial 到期账号，更新状态"""
        now = int(datetime.now(timezone.utc).timestamp())
        with Session(engine) as s:
            accounts = s.exec(select(AccountModel)).all()
            graphs = load_account_graphs(s, [int(acc.id or 0) for acc in accounts if acc.id])
            updated = 0
            for acc in accounts:
                graph = graphs.get(int(acc.id or 0), {})
                if graph.get("lifecycle_status") != "trial":
                    continue
                trial_end_time = int((graph.get("overview") or {}).get("trial_end_time") or 0)
                if trial_end_time and trial_end_time < now:
                    acc.updated_at = datetime.now(timezone.utc)
                    patch_account_graph(s, acc, lifecycle_status=AccountStatus.EXPIRED.value)
                    s.add(acc)
                    updated += 1
            s.commit()
            if updated:
                print(f"[Scheduler] {updated} 个 trial 账号已到期")

    def check_accounts_valid(self, platform: str = None, limit: int = 50):
        """批量检测账号有效性"""
        load_all()
        with Session(engine) as s:
            q = select(AccountModel)
            if platform:
                q = q.where(AccountModel.platform == platform)
            q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            accounts = s.exec(q.limit(limit)).all()
            graphs = load_account_graphs(s, [int(acc.id or 0) for acc in accounts if acc.id])
            accounts = [
                acc for acc in accounts
                if graphs.get(int(acc.id or 0), {}).get("lifecycle_status") in {"registered", "trial", "subscribed"}
            ]

        results = {"valid": 0, "invalid": 0, "error": 0}
        for acc in accounts:
            try:
                PlatformCls = get(acc.platform)
                plugin = PlatformCls(config=RegisterConfig())
                with Session(engine) as s:
                    current = s.get(AccountModel, acc.id)
                    if not current:
                        continue
                    account_obj = build_platform_account(s, current)
                valid = plugin.check_valid(account_obj)
                with Session(engine) as s:
                    a = s.get(AccountModel, acc.id)
                    if a:
                        a.updated_at = datetime.now(timezone.utc)
                        summary_updates = {"checked_at": _utcnow_iso(), "valid": valid}
                        if hasattr(plugin, "get_last_check_overview"):
                            summary_updates.update(plugin.get_last_check_overview() or {})
                        patch_account_graph(
                            s,
                            a,
                            summary_updates=summary_updates,
                        )
                        s.add(a)
                        s.commit()
                if valid:
                    results["valid"] += 1
                else:
                    results["invalid"] += 1
            except Exception:
                results["error"] += 1
        return results


scheduler = Scheduler()
