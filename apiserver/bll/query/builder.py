from typing import Optional, Sequence, Iterable, Union

from apiserver.config_repo import config

log = config.logger(__file__)

RANGE_IGNORE_VALUE = -1


class Builder:
    @staticmethod
    def dates_range(
        from_date: Optional[Union[int, float]] = None,
        to_date: Optional[Union[int, float]] = None,
    ) -> dict:
        assert (
            from_date or to_date
        ), "range condition requires that at least one of from_date or to_date specified"
        conditions = {}
        if from_date:
            conditions["gte"] = int(from_date)
        if to_date:
            conditions["lte"] = int(to_date)
        return {
            "range": {
                "timestamp": {
                    **conditions,
                    "format": "epoch_second",
                }
            }
        }

    @staticmethod
    def terms(field: str, values: Iterable) -> dict:
        if isinstance(values, str):
            assert not isinstance(values, str), "apparently 'term' should be used here"
        return {"terms": {field: list(values)}}
    @staticmethod
    def term(field: str, value) -> dict:
        return {"term": {field: value}}

    @staticmethod
    def normalize_range(
        range_: Sequence[Union[int, float]],
        ignore_value: Union[int, float] = RANGE_IGNORE_VALUE,
    ) -> Optional[Sequence[Union[int, float]]]:
        if not range_ or set(range_) == {ignore_value}:
            return None
        if len(range_) < 2:
            return [range_[0]] * 2
        return range_
