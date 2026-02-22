from .mean import MeanAggregation, MedianAggregation
from .ABAggregation import ABAggregation as ABAggregationBase

AGGS_LIST: list[type[ABAggregationBase]] = [
    MeanAggregation,
    MedianAggregation,
]

AGG_NAME_TO_CLASS: dict[str, type[ABAggregationBase]] = {
    c.name(): c
    for c in AGGS_LIST
}