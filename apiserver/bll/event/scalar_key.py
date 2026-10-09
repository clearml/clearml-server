"""
Module for polymorphism over different types of X axes in scalar aggregations
"""
from abc import ABC, abstractmethod
from enum import auto

from typing import Any

from apiserver.utilities import extract_properties_to_lists
from apiserver.utilities.stringenum import StringEnum
from apiserver.config_repo import config

log = config.logger(__file__)


class ScalarKeyEnum(StringEnum):
    """
    String enum representing X axes key
    """

    iter = auto()
    timestamp = auto()
    iso_time = auto()


class ScalarKey(ABC):
    """
    Abstract scalar key
    """

    _enum_to_key = {}
    average_key = "avg_key"

    @property
    @abstractmethod
    def enum_value(self) -> ScalarKeyEnum:
        """
        Enum value accepted in API requests
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Key name. Used as arbitrary internal key in elasticsearch queries
        """
        pass

    @property
    @abstractmethod
    def field(self) -> str:
        """
        Event key to aggregate by
        """
        pass

    @abstractmethod
    def get_aggregation(self, interval: int) -> dict:
        """
        Get aggregation for this type of key
        :param interval: elasticsearch aggregation interval
        """
        pass

    def _average_key_aggregation(self, **kwargs) -> dict:
        """
        Sub aggregation for the average key of the events in a bucket.
        It is returned as the bucket x value since the bucket key is the bucket
        lower bound, which is lower than the key of any event in a sparse bucket
        """
        return {"aggs": {self.average_key: {"avg": {"field": self.field, **kwargs}}}}

    def __init_subclass__(cls, **kwargs):
        """
        Save a mapping from enum values to key class
        """
        if cls.enum_value not in ScalarKeyEnum:
            raise ValueError(f"{cls.enum_value!r} not in {ScalarKeyEnum.__name__}")
        if cls.enum_value in cls._enum_to_key:
            log.warning(
                f"'{cls.enum_value.value}' is already registered to {ScalarKey.__name__}"
            )
        cls._enum_to_key[cls.enum_value] = cls

    @classmethod
    def resolve(cls, key: ScalarKeyEnum):
        """
        Create a key instance from enum instance
        """
        return cls._enum_to_key[key]()

    def get_iterations_data(self, iter_buckets: dict) -> dict:
        """
        Convert a list of bucket entries to `x`s array and `y`s array
        """
        return extract_properties_to_lists(
            ("x", "y"),
            iter_buckets[self.name]["buckets"],
            self._get_iterations_data_single,
        )

    def _get_iterations_data_single(self, iter_data):
        """
        Extract x value and y value from a single bucket item
        """
        return (
            round(iter_data[self.average_key]["value"]),
            iter_data["avg_val"]["value"],
        )

    def cast_value(self, value: Any) -> Any:
        """Cast value to appropriate type"""
        return value


class TimestampKey(ScalarKey):
    """
    Aggregate by timestamp in milliseconds since epoch
    """

    name = "timestamp"
    field = "timestamp"
    enum_value = ScalarKeyEnum.timestamp

    def get_aggregation(self, interval: int) -> dict:
        return {
            self.name: {
                "date_histogram": {
                    "field": "timestamp",
                    "fixed_interval": f"{interval}ms",
                    "min_doc_count": 1,
                },
                **self._average_key_aggregation(),
            }
        }

    def cast_value(self, value: Any) -> int:
        return int(value)


class IterKey(ScalarKey):
    """
    Aggregate by iteration number
    """

    name = "iters"
    field = "iter"
    enum_value = ScalarKeyEnum.iter

    def get_aggregation(self, interval: int) -> dict:
        return {
            self.name: {
                "histogram": {
                    "field": "iter",
                    "interval": interval,
                    "min_doc_count": 1,
                },
                **self._average_key_aggregation(),
            }
        }

    def cast_value(self, value: Any) -> int:
        return int(value)


class ISOTimeKey(ScalarKey):
    """
    Aggregate by time formatted as ISO strings
    """

    name = "iso_time"
    field = "timestamp"
    enum_value = ScalarKeyEnum.iso_time
    time_format = "strict_date_time"

    def get_aggregation(self, interval: int) -> dict:
        return {
            self.name: {
                "date_histogram": {
                    "field": "timestamp",
                    "fixed_interval": f"{interval}ms",
                    "min_doc_count": 1,
                    "format": self.time_format,
                },
                **self._average_key_aggregation(format=self.time_format),
            }
        }

    def _get_iterations_data_single(self, iter_data):
        return (
            iter_data[self.average_key]["value_as_string"],
            iter_data["avg_val"]["value"],
        )
