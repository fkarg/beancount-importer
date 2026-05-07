from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable
from collections.abc import Iterator

from beancount_importer.models import SourceTransaction


class AbstractParser(ABC):
    """Base class for bank-specific parsers that need custom Python logic."""

    @property
    @abstractmethod
    def bank_key(self) -> str:
        """Unique identifier matching BankConfig.key."""

    @property
    @abstractmethod
    def header_signature(self) -> frozenset[str]:
        """Expected CSV column names; used for future auto-detection."""

    @abstractmethod
    def parse(self, file_path: str) -> Iterator[SourceTransaction]:
        """Yield parsed transactions from the given CSV file."""


@runtime_checkable
class Parser(Protocol):
    """Structural protocol — any object with parse() qualifies."""

    @property
    def bank_key(self) -> str: ...

    @property
    def header_signature(self) -> frozenset[str]: ...

    def parse(self, file_path: str) -> Iterator[SourceTransaction]: ...
