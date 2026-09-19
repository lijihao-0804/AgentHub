from __future__ import annotations

from apps.api.app import create_app
from packages.core.config.settings import Settings


def test_m7b_experiment_routes_are_registered() -> None:
    app = create_app(Settings(testing=True))
    paths = app.openapi()["paths"]

    assert "/api/v1/workspaces/{workspace_id}/evaluation/experiments" in paths
    assert "/api/v1/workspaces/{workspace_id}/evaluation/experiments/{experiment_id}" in paths
    assert (
        "/api/v1/workspaces/{workspace_id}/evaluation/experiments/{experiment_id}/variants"
        in paths
    )
    assert (
        "/api/v1/workspaces/{workspace_id}/evaluation/experiments/{experiment_id}/finalize"
        in paths
    )
    assert "/api/v1/workspaces/{workspace_id}/evaluation/experiments/{experiment_id}/runs" in paths
    assert "/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}" in paths
    assert (
        "/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/cancel"
        in paths
    )
    assert (
        "/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/progress"
        in paths
    )
    metrics_path = "/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/metrics"
    comparisons_path = (
        "/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/comparisons"
    )
    assert "get" in paths[metrics_path]
    assert "post" in paths[metrics_path]
    assert "get" in paths[comparisons_path]
    assert "post" in paths[comparisons_path]
    assert (
        "/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/comparisons/{comparison_id}"
        in paths
    )

    assert "post" in paths[
        "/api/v1/workspaces/{workspace_id}/evaluation/experiments/{experiment_id}/runs"
    ]
