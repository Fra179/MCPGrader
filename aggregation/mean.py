from .ABAggregation import ABAggregation

class MeanAggregation(ABAggregation[float]):
    @classmethod
    def name(cls) -> str:
        return "mean"

    @classmethod
    def pretty_name(cls) -> str:
        return "Mean"

    @classmethod
    def compatible_with(cls) -> list[type]:
        return [float, int]

    def aggregate(self, data: list[float]) -> float:
        if not data:
            return 0.0
        return sum(data) / len(data)
    
class MedianAggregation(ABAggregation[float]):
    @classmethod
    def name(cls) -> str:
        return "median"

    @classmethod
    def pretty_name(cls) -> str:
        return "Median"

    @classmethod
    def compatible_with(cls) -> list[type]:
        return [float, int]

    def aggregate(self, data: list[float]) -> float:
        if not data:
            return 0.0
        sorted_data = sorted(data)
        n = len(sorted_data)
        return (sorted_data[n // 2] if n % 2 == 1 else (sorted_data[n // 2 - 1] + sorted_data[n // 2]) / 2)