from __future__ import annotations

from sqlmodel import Session

from application.accounts import AccountsService
from core.account_graph import patch_account_graph
from core.db import AccountModel, engine
from core import lifecycle


def _create_account(email: str) -> int:
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email=email, password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)
        patch_account_graph(session, account, lifecycle_status="registered", summary_updates={"valid": True})
        session.commit()
        return int(account.id or 0)


def test_chatgpt_model_pool_checks_every_account_and_cleans_invalid(monkeypatch):
    first = _create_account("first@example.com")
    second = _create_account("second@example.com")
    calls: list[tuple[str, int]] = []
    cleanup_calls: list[bool] = []

    def fake_probe(email: str, **kwargs):
        calls.append((email, int(kwargs["timeout_seconds"])))
        if email == "first@example.com":
            return {"status": "success", "sub2api_account_id": 101}
        return {"status": "failed", "message": "no model response", "sub2api_account_id": 102}

    monkeypatch.setattr(lifecycle, "probe_sub2api_account_model", fake_probe)
    monkeypatch.setattr(
        AccountsService,
        "cleanup_invalid_chatgpt",
        lambda _self, *, include_sub2api: cleanup_calls.append(include_sub2api)
        or {"deleted_local": 1, "deleted_sub2api": 1},
    )

    result = lifecycle.check_chatgpt_model_pool(max_workers=2, log_fn=lambda _message: None)

    assert set(calls) == {("first@example.com", 15), ("second@example.com", 15)}
    assert cleanup_calls == [True]
    assert result == {"checked": 2, "valid": 1, "invalid": 1, "error": 0}
