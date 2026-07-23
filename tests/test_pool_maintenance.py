from __future__ import annotations

from sqlmodel import Session

from application import tasks
from core.account_graph import patch_account_graph
from core.db import AccountModel, engine
from core import scheduler as scheduler_module
from infrastructure.config_repository import ConfigRepository


class _Logger:
    def __init__(self):
        self.logs = []
        self.result_data = None
        self.finished = None

    def log(self, message, **_kwargs):
        self.logs.append(message)

    def record_error(self, error):
        self.logs.append(f"error: {error}")

    def set_progress(self, *_args):
        pass

    def set_result_data(self, data):
        self.result_data = data

    def finish(self, status, *, error=""):
        self.finished = (status, error)

    def is_cancel_requested(self):
        return False


def _create_chatgpt_account(email: str) -> int:
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email=email, password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)
        patch_account_graph(session, account, lifecycle_status="registered", summary_updates={"valid": True})
        session.commit()
        return int(account.id or 0)


def test_pool_maintenance_settings_are_persisted():
    repo = ConfigRepository()

    repo.update_flat(
        {
            "chatgpt_pool_maintenance_enabled": "true",
            "chatgpt_pool_target": "100",
            "chatgpt_pool_interval_seconds": "60",
        }
    )

    config = repo.get_flat()
    assert config["chatgpt_pool_maintenance_enabled"] == "true"
    assert config["chatgpt_pool_target"] == "100"
    assert config["chatgpt_pool_interval_seconds"] == "60"


def test_pool_maintenance_queues_exact_shortfall_with_sub2api_upload(monkeypatch):
    account_ids = [_create_chatgpt_account(f"pool-{index}@example.com") for index in range(3)]
    created = []
    logger = _Logger()

    monkeypatch.setattr(tasks, "_run_single_account_check", lambda account_id, _logger: (True, {"account_id": account_id}))
    monkeypatch.setattr(tasks, "create_register_task", lambda payload: created.append(payload) or {"id": "register-1"})

    tasks._execute_pool_maintenance_task(
        {"platform": "chatgpt", "target": 5, "concurrency": 1, "account_ids": account_ids},
        logger,
    )

    assert len(created) == 1
    assert created[0]["count"] == 1
    assert created[0]["executor_type"] == "headless"
    assert created[0]["extra"]["auto_upload_agent_identity_cpa"] is True
    assert created[0]["extra"]["source"] == "pool_maintenance"
    assert logger.result_data["usable"] == len(account_ids)
    assert logger.finished == (tasks.TASK_STATUS_SUCCEEDED, "")


def test_scheduler_creates_registration_directly_when_enabled_and_pool_is_short(monkeypatch):
    values = {
        "chatgpt_pool_maintenance_enabled": "true",
        "chatgpt_pool_target": "100",
        "chatgpt_pool_registration_concurrency": "1",
    }
    created = []
    monkeypatch.setattr(scheduler_module.config_store, "get", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(
        scheduler_module,
        "has_queued_or_active_chatgpt_work",
        lambda: False,
    )
    monkeypatch.setattr(
        scheduler_module,
        "get_chatgpt_pool_status",
        lambda: {"usable": 98},
    )
    monkeypatch.setattr(
        scheduler_module,
        "create_register_task",
        lambda payload: created.append(payload) or {"id": "register-1"},
    )
    monkeypatch.setattr(scheduler_module.task_runtime, "wake_up", lambda: created.append("wake"))

    scheduler = scheduler_module.Scheduler()
    assert scheduler.check_chatgpt_pool_maintenance() is True
    assert created[0]["count"] == 1
    assert created[0]["source"] == "pool_maintenance"
    assert created[0]["extra"]["auto_upload_agent_identity_cpa"] is True
    assert created[1] == "wake"

    values["chatgpt_pool_maintenance_enabled"] = "false"
    assert scheduler.check_chatgpt_pool_maintenance() is False


def test_auto_registration_immediately_enqueues_next_account_until_target(monkeypatch):
    created = []
    logger = _Logger()

    monkeypatch.setattr(
        tasks,
        "create_register_task",
        lambda payload: created.append(payload) or {"id": "retry-1"},
    )
    monkeypatch.setattr(
        "core.config_store.config_store.set_many",
        lambda _values: None,
    )
    monkeypatch.setattr(
        "core.config_store.config_store.get",
        lambda key, default="": {
            "chatgpt_pool_target": "3",
            "chatgpt_pool_maintenance_enabled": "true",
        }.get(key, default),
    )
    monkeypatch.setattr(tasks, "_count_chatgpt_pool_accounts", lambda: 1)

    tasks._continue_auto_pool_registration(
        {
            "platform": "chatgpt",
            "count": 5,
            "concurrency": 3,
            "source": "pool_maintenance",
            "extra": {"identity_provider": "mailbox"},
        },
        tasks.TASK_STATUS_FAILED,
        logger,
    )

    assert created == [{
        "platform": "chatgpt",
        "count": 1,
        "concurrency": 1,
        "source": "pool_maintenance",
        "extra": {"identity_provider": "mailbox", "source": "pool_maintenance"},
    }]
    assert any("自动补量 1/3，已创建 1 个并发注册: retry-1" in line for line in logger.logs)


def test_auto_registration_stops_when_pool_target_is_reached(monkeypatch):
    created = []
    logger = _Logger()

    monkeypatch.setattr(tasks, "create_register_task", lambda payload: created.append(payload))
    monkeypatch.setattr("core.config_store.config_store.set_many", lambda _values: None)
    monkeypatch.setattr(
        "core.config_store.config_store.get",
        lambda key, default="": {
            "chatgpt_pool_target": "3",
            "chatgpt_pool_maintenance_enabled": "true",
        }.get(key, default),
    )
    monkeypatch.setattr(tasks, "_count_chatgpt_pool_accounts", lambda: 3)

    tasks._continue_auto_pool_registration(
        {"platform": "chatgpt", "source": "pool_maintenance"},
        tasks.TASK_STATUS_SUCCEEDED,
        logger,
    )

    assert created == []
    assert "自动补量已达到目标: 3/3" in logger.logs


def test_auto_registration_uses_configured_concurrency_without_exceeding_shortfall(monkeypatch):
    created = []
    logger = _Logger()

    monkeypatch.setattr(tasks, "create_register_task", lambda payload: created.append(payload) or {"id": "retry-2"})
    monkeypatch.setattr("core.config_store.config_store.set_many", lambda _values: None)
    monkeypatch.setattr(
        "core.config_store.config_store.get",
        lambda key, default="": {
            "chatgpt_pool_target": "10",
            "chatgpt_pool_maintenance_enabled": "true",
            "chatgpt_pool_registration_concurrency": "2",
        }.get(key, default),
    )
    monkeypatch.setattr(tasks, "_count_chatgpt_pool_accounts", lambda: 9)

    tasks._continue_auto_pool_registration(
        {"platform": "chatgpt", "source": "pool_maintenance"},
        tasks.TASK_STATUS_SUCCEEDED,
        logger,
    )

    assert created[0]["count"] == 1
    assert created[0]["concurrency"] == 2
