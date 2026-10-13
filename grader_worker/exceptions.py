class GraderWorkerException(Exception):
    """Base exception for GraderWorkerAPI related errors."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
