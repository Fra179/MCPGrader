from .ABAggregation import ABAggregation

class MeanAggregation(ABAggregation):
    @classmethod
    def name(cls) -> str:
        return "mean"

    @classmethod
    def pretty_name(cls) -> str:
        return "Mean"

    @classmethod
    def compatible_with(cls) -> list[type]:
        return [float, int]

    def aggregate(self, data: list[float]) -> dict[str, float]:
        if not data:
            return {"mean": 0.0}
        mean_value = sum(data) / len(data)
        return {"mean": mean_value}
    
class MedianAggregation(ABAggregation):
    @classmethod
    def name(cls) -> str:
        return "median"

    @classmethod
    def pretty_name(cls) -> str:
        return "Median"

    @classmethod
    def compatible_with(cls) -> list[type]:
        return [float, int]

    def aggregate(self, data: list[float]) -> dict[str, float]:
        if not data:
            return {"median": 0.0}
        sorted_data = sorted(data)
        n = len(sorted_data)
        median_value = (sorted_data[n // 2] if n % 2 == 1 else (sorted_data[n // 2 - 1] + sorted_data[n // 2]) / 2)
        return {"median": median_value}