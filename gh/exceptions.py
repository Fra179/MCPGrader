class GitHubException(Exception):
    """Base exception for GitHubAPI related errors."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

class ClassroomNotFoundException(GitHubException):
    """Exception raised when a classroom is not found."""
    def __init__(self, identifier: str):
        message = f"Classroom with identifier '{identifier}' not found."
        super().__init__(message)
        self.message = message