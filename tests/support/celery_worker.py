from __future__ import annotations

from apps.worker.celery_app import celery_app
from apps.worker.tasks import knowledge as knowledge_tasks
from tests.support.knowledge import fake_indexing_components


def main() -> None:
    # This replacement exists only in the test worker subprocess. The production
    # Celery task always injects its real BGE/Sparse/Reranker composition.
    knowledge_tasks._production_indexing_components = fake_indexing_components
    celery_app.worker_main(
        [
            "worker",
            "--loglevel=WARNING",
            "--pool=solo",
            "--concurrency=1",
        ]
    )


if __name__ == "__main__":
    main()
