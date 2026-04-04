"""
suppliers/base_supplier.py — Abstract base class for all book suppliers.

WHY THIS EXISTS:
The scanner doesn't care where books come from — CSV, API, or scraper.
Every supplier must implement `get_books()` and return a list of Book objects.
Swap suppliers by changing one line in config.py. No other code changes needed.
"""

from abc import ABC, abstractmethod
from typing import List
from models import Book


class BaseSupplier(ABC):
    """
    All book data sources must inherit from this class and implement get_books().

    This is the "contract" that makes the input layer swappable.
    """

    @abstractmethod
    def get_books(self) -> List[Book]:
        """
        Fetch and return a list of Book objects from this supplier.

        Implementations should:
        - Clean and normalize all data (strip $, convert to float, etc.)
        - Return an empty list (not raise) if the source is unavailable
        - Log warnings for any rows that couldn't be parsed
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
