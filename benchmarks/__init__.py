"""EQUIPA benchmark utilities.

This package contains tools for running benchmarks against EQUIPA:
- cumulative_db: Cumulative knowledge extraction/merge for Docker containers
"""

from .cumulative_db import CumulativeDB

__all__ = ["CumulativeDB"]
