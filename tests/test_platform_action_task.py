from __future__ import annotations

from concurrent.futures import Future
import time

from sqlmodel import Session

from application import tasks as tasks_module
from core.base_platform import Account
from core.db import TaskModel, engine
from domain.actions import ActionExecutionResult
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module


class _FakeLogger:
    def __init__(self):
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_wait_for_registration_workers_marks_stalled_futures_as_timed_out(monkeypatch):
    completed = Future()
    completed.set_result({"account_id": 1, "email": "ok@example.com"})
    stalled = Future()

    calls = []

    def fake_wait(pending, *, timeout, return_when):
        calls.append((set(pending), timeout, return_when))
        return {completed}, {stalled}

    done, timed_out = tasks_module._wait_for_registration_workers(
        {completed, stalled},
        timeout_seconds=120,
        wait_fn=fake_wait,
    )

    assert done == {completed}
    assert timed_out == set()
    assert calls[0][1] == 120


def test_wait_for_registration_workers_returns_stalled_futures_after_timeout(monkeypatch):
    stalled = Future()

    def fake_wait(pending, *, timeout, return_when):
        return set(), set(pending)

    done, timed_out = tasks_module._wait_for_registration_workers(
        {stalled},
        timeout_seconds=120,
        wait_fn=fake_wait,
    )

    assert done == set()
    assert timed_out == {stalled}


def test_registration_worker_stall_detection_is_per_worker():
    fresh = Future()
    stale = Future()
    now = time.monotonic()

    stalled = tasks_module._find_stalled_registration_workers(
        {fresh: 1, stale: 2},
        {1: now - 5, 2: now - 91},
        timeout_seconds=90,
        now=now,
    )

    assert stalled == {stale}


def test_chatgpt_register_workers_build_distinct_mailboxes(monkeypatch):
    mailboxes = []
    seen_mailboxes = []

    class FakePlatform:
        def __init__(self, mailbox):
            self.mailbox = mailbox

        def register(self, email=None, password=None):
            seen_mailboxes.append(self.mailbox)
            return Account(
                platform="chatgpt",
                email=f"registered-{len(seen_mailboxes)}@example.com",
                password="Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda _platform, _payload, _logger, **kwargs: FakePlatform(kwargs["shared_mailbox"]),
    )
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox",
        lambda *args, **kwargs: mailboxes.append(object()) or mailboxes[-1],
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account: type("SavedAccount", (), {"id": len(seen_mailboxes)})(),
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 2,
            "concurrency": 2,
            "extra": {"identity_provider": "mailbox", "mail_provider": "yyds_mail_api"},
        },
        logger,
    )

    assert len(mailboxes) == 2
    assert len({id(mailbox) for mailbox in seen_mailboxes}) == 2


def test_service_restart_keeps_pending_tasks_queued():
    pending = tasks_module.create_task(
        task_type="platform_action",
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 1, "action_id": "query_state"},
    )
    running = tasks_module.create_task(
        task_type="platform_action",
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 2, "action_id": "query_state"},
    )
    with Session(engine) as session:
        running_model = session.get(TaskModel, running["id"])
        running_model.status = tasks_module.TASK_STATUS_RUNNING
        session.add(running_model)
        session.commit()

    tasks_module.mark_incomplete_tasks_interrupted()

    with Session(engine) as session:
        assert session.get(TaskModel, pending["id"]).status == tasks_module.TASK_STATUS_PENDING
        assert session.get(TaskModel, running["id"]).status == tasks_module.TASK_STATUS_INTERRUPTED


def test_platform_action_task_passes_task_logger_to_runtime(monkeypatch):
    seen = {}

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            seen["log_fn"] = log_fn
            seen["cancel_check"] = cancel_check
            if log_fn:
                log_fn("checkout step log")
            return ActionExecutionResult(ok=True, data={"message": "summary"})

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert getattr(seen["log_fn"], "__name__", "") == "log"
    assert getattr(seen["cancel_check"], "__self__", None) is logger
    assert getattr(seen["cancel_check"], "__name__", "") == "is_cancel_requested"
    assert seen["cancel_check"]() is False
    assert ("log", "checkout step log", {}) in logger.events
    assert logger.result_data == {"message": "summary"}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_register_task_succeeds_after_successful_registration(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr(
        tasks_module,
        "_upload_registered_agent_identity",
        lambda account_id: {
            "account_id": account_id,
            "ok": True,
            "message": "Sub2API 数据导入成功",
            "filename": "registered.json",
            "target": "sub2api",
        },
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "auto_download_agent_identity": True,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data == {
        "success": 1,
        "fail": 0,
        "account_ids": [123],
        "accounts": [
                {
                    "account_id": 123,
                    "email": "registered@example.com",
                    "agent_identity_upload": {
                        "account_id": 123,
                        "ok": True,
                        "message": "Sub2API 数据导入成功",
                        "filename": "registered.json",
                        "target": "sub2api",
                    },
                }
            ],
            "auto_download_agent_identity": True,
            "auto_upload_agent_identity_cpa": True,
            "agent_identity_uploads": [
                {
                    "account_id": 123,
                    "ok": True,
                    "message": "Sub2API 数据导入成功",
                    "filename": "registered.json",
                    "target": "sub2api",
                }
            ],
        }
    assert any(event[0] == "success" for event in logger.events)
    assert not any(
        "cannot access local variable 'extra'" in str(event)
        for event in logger.events
    )


def test_register_api_preserves_protocol_outlook_pool(client, monkeypatch):
    captured = {}

    def fake_create(payload):
        captured.update(payload)
        return {"task_id": "task_protocol"}

    monkeypatch.setattr("api.task_commands.command_service.create_register_task", fake_create)
    pool_text = "user@outlook.com----mail-pass----client-id----refresh-token"

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 1,
            "executor_type": "protocol",
            "extra": {
                "local_ms_pool_text": pool_text,
                "auto_download_agent_identity": True,
            },
        },
    )

    assert response.status_code == 200
    assert captured["executor_type"] == "protocol"
    assert captured["extra"]["mail_provider"] == "local_ms_pool"
    assert captured["extra"]["local_ms_pool_text"] == pool_text
    assert captured["extra"]["auto_download_agent_identity"] is True


def test_register_api_rejects_protocol_without_outlook_pool(client):
    response = client.post(
        "/api/tasks/register",
        json={"executor_type": "protocol", "count": 1, "extra": {}},
    )

    assert response.status_code == 400
    assert "Outlook" in response.json()["detail"]


def test_platform_action_task_finishes_cancelled_without_starting_runtime(monkeypatch):
    class FakeRuntime:
        def execute_action(self, *args, **kwargs):
            raise AssertionError("runtime should not start after cancellation")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_action_task_marks_cancelled_after_runtime_cancel(monkeypatch):
    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            assert cancel_check() is False
            logger.cancel_requested = True
            return ActionExecutionResult(ok=False, error="任务已取消")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_runtime_wires_log_fn_to_platform(monkeypatch):
    logs = []
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt"})()

        def add(self, model):
            pass

        def commit(self):
            pass

    class FakePlatform:
        def __init__(self, config=None):
            self._log_fn = print

        def set_logger(self, logger):
            self._log_fn = logger

        def set_cancel_checker(self, checker):
            seen["cancel_check"] = checker

        def execute_action(self, action_id, account, params):
            self._log_fn("runtime platform log")
            assert self.is_cancel_requested() is False
            return {"ok": True, "data": {"message": "ok"}}

        def is_cancel_requested(self):
            return seen["cancel_check"]()

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", lambda *args, **kwargs: None)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="query_state",
            params={},
        ),
        log_fn=logs.append,
        cancel_check=lambda: False,
    )

    assert result.ok is True
    assert logs == ["runtime platform log"]
    assert seen["cancel_check"]() is False
