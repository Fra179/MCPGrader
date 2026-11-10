"""Shared utilities for trusted grading scripts.

This package must remain independent of the coordinator and its dependencies.
The grader makes it importable in the grading script's Python environment.
"""

from .openmp import check_openmp

__all__ = ["check_openmp"]

__version__ = "0.1.0"
