from grader.grader import Grader
from config import ConfigParser
from logger import build_logger
import sentry_sdk

def main():
    logger = build_logger(log_file="grader.log")

    config = ConfigParser("grader-config.yaml")
    config.parse()

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