from .mean import MeanAggregation, MedianAggregation
from .ABAggregation import ABAggregation

AGGS_LIST: list[type[ABAggregation]] = [
    MeanAggregation, 
    MedianAggregation
]

AGG_NAME_TO_CLASS: dict[str, type[ABAggregation]] = { c().name: c for c in AGGS_LIST }