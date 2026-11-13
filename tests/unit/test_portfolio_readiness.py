"""Readiness cannot be satisfied by a different Celery worker."""

import sys
from types import SimpleNamespace

from scripts import portfolio_readiness


def test_worker_probe_rejects_a_foreign_worker_pong(monkeypatch):
    seen = []

    def ping(**kwargs):
        seen.append(kwargs)
        return [{"unrelated-worker@host": {"ok": "pong"}}]

    monkeypatch.setattr(portfolio_readiness, "configure", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "apps.worker.celery_app",
        SimpleNamespace(
            celery_app=SimpleNamespace(conf=SimpleNamespace(), control=SimpleNamespace(ping=ping))
        ),
    )
    assert not portfolio_readiness.worker_probe(123)
    assert seen[0]["destination"] == [portfolio_readiness.worker_name(123)]
