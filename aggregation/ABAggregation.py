from abc import ABC, abstractmethod
from typing import Any, TypeVar

class ABAggregation[T](ABC):
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
    def aggregate(self, data: list[T]) -> T:
        pass