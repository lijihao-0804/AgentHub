from apps.worker.celery_app import celery_app


def main() -> None:
    celery_app.worker_main(["worker", "--loglevel=INFO", "--concurrency=1"])


if __name__ == "__main__":
    main()
