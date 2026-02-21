from abc import ABC, abstractmethod
from typing import Any

class ABAggregation(ABC):
    @classmethod
    @abstractmethod
    def name(cls) -> str:
        pass    

    @classmethod
    @abstractmethod
    def pretty_name(cls) -> str:
        pass

    @classmethod
    @abstractmethod
    def compatible_with(cls) -> list[type]:
        pass

    @abstractmethod
    def aggregate(self, data: list[Any]) -> dict[str, Any]:
        pass