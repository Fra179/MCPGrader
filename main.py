from grader.grader import Grader
from config import ConfigParser
from logger import build_logger
from check_locks import lock_status
import sentry_sdk

def assert_locks_supported(path: str):
    status = lock_status(path)

    assert status in ("cluster", "ok"), f"Unsafe locking on {path}: {status}. The grader can't be executed on this filesystem. Please use a local filesystem or a network filesystem with proper locking support"

def main():
    logger = build_logger(log_file="grader.log")

    config = ConfigParser("grader-config.yaml")
    config.parse()

    # The run lock lives in working_dir: check that locking works there and, on a shared
    # filesystem, that it's consistent across nodes
    assert_locks_supported(config.config.grader.working_dir)

    # Sentry for observability
    if config.config.grader.sentry_dsn:
        sentry_sdk.init(
            dsn=config.config.grader.sentry_dsn,
        )
        logger.info("Sentry SDK initialized.")
    else:
        logger.info("No Sentry DSN provided; skipping Sentry initialization.")

    grader = Grader(config.config, logger)
    grader.grade()

if __name__ == '__main__':
    main()