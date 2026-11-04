from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from packages.evaluation import runner as runner_module
from packages.evaluation.runner import ExperimentRunner


@pytest.mark.asyncio
async def test_database_failure_in_heartbeat_stops_the_case_execution(monkeypatch):
    class UnavailableFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise SQLAlchemyError("controlled heartbeat connection failure")

        async def __aexit__(self, *args):
            return False

    class ExecutionTask:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(runner_module.asyncio, "sleep", no_delay)
    task = ExecutionTask()
    await ExperimentRunner(UnavailableFactory())._heartbeat_loop(
        run_id=uuid4(),
        owner="obsolete-worker",
        generation=1,
        settings=SimpleNamespace(
            evaluation_runner_heartbeat_seconds=1, evaluation_runner_lease_seconds=5
        ),
        execution_task=task,
    )
    assert task.cancelled
