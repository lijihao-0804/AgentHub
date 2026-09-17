import logging
import time

from packages.core.config.settings import get_settings
from packages.core.logging.json_logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    logger.info("worker_started", extra={"environment": settings.environment})
    # M0 establishes the worker process boundary. Durable ingestion work starts in M3.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
