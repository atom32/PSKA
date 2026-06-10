from __future__ import annotations

from abc import ABC, abstractmethod

from pska.models import ArchiveRecord


class Collector(ABC):
    @abstractmethod
    def collect(self, url: str) -> ArchiveRecord:
        raise NotImplementedError
