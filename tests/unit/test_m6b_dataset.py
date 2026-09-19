from pathlib import Path

from benchmarks.observability.schema import EXPECTED_CASE_COUNT, load_dataset


def test_m6b_failure_scenario_dataset_is_bounded_and_runtime_scoped() -> None:
    path = Path("benchmarks/observability/dataset.json")
    dataset = load_dataset(path)
    assert len(dataset.cases) == EXPECTED_CASE_COUNT
    assert all(case.runtime_path.startswith("AgentRunService") for case in dataset.cases)
    assert all("prompt" not in case.scenario.lower() for case in dataset.cases)
