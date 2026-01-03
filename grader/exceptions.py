class GraderException(Exception):
    """Base exception class for grader-related errors."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message