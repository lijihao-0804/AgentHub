from pathlib import Path
from uuid import uuid4

from _pytest.fixtures import FixtureFunctionDefinition

from tests.integration import test_context_policy_m2 as context_policy
from tests.integration import test_measured_reliability as measured_reliability
from tests.integration import test_thread_history_snapshot_m3 as history_snapshot
from tests.integration.test_m4c_agent_runtime import _context


class RecordingTempPathFactory:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.basenames: list[str] = []

    def mktemp(self, basename: str) -> Path:
        self.basenames.append(basename)
        return self.directory


def test_context_catalog_fixture_uses_pytest_tmp_factory_without_temp_env(monkeypatch) -> None:
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    expected_directory = Path("agenthub-context-policy-m2-regression")
    temp_path_factory = RecordingTempPathFactory(expected_directory)

    directory = context_policy.catalog_dir.__wrapped__(temp_path_factory)

    assert directory == expected_directory
    assert temp_path_factory.basenames == ["agenthub-context-policy-m2"]


def test_history_database_fixtures_are_explicitly_registered() -> None:
    assert isinstance(history_snapshot.db_factory, FixtureFunctionDefinition)
    assert isinstance(history_snapshot.migrated_database, FixtureFunctionDefinition)


def test_measured_positive_identity_adds_workspace_read_without_weakening_denial() -> None:
    base_context = _context(uuid4(), uuid4(), uuid4())

    positive_context = measured_reliability._with_positive_action_permissions(base_context)

    assert {"approve_action", "workspace_read"} <= positive_context.permissions
    assert "workspace_read" not in base_context.permissions
    assert "approve_action" not in base_context.permissions
