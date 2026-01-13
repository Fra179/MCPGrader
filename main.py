from grader.grader import Grader
from config import ConfigParser
from logger import build_logger
import sentry_sdk
import os

def main():
    logger = build_logger(log_file="grader.log")

    config = ConfigParser("grader-config.yaml")
    config.parse()

    token = os.getenv("GH_PAT") or config.config.grader.github_pat
    if not token:
        logger.error("GitHub Personal Access Token (PAT) not provided. Set it via GH_PAT environment variable or in the config file.")
        return

    # Sentry for observability
    if config.config.grader.sentry_dsn:
        sentry_sdk.init(
            dsn=config.config.grader.sentry_dsn,
        )
        logger.info("Sentry SDK initialized.")
    else:
        logger.info("No Sentry DSN provided; skipping Sentry initialization.")

    grader = Grader(config.config, token, logger)
    grader.grade()

if __name__ == '__main__':
    main()