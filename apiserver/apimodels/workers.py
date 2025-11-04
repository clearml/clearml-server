from enum import Enum

import six
from jsonmodels import validators
from jsonmodels.fields import (
    StringField,
    EmbeddedField,
    DateTimeField,
    IntField,
    FloatField,
    BoolField,
)
from jsonmodels.models import Base

from apiserver.apimodels import ListField, EnumField, JsonSerializableMixin, ActualEnumField
from apiserver.config_repo import config


class WorkerRequest(Base):
    worker = StringField(required=True)
    tags = ListField(str)
    system_tags = ListField(str)

class Resources(Base):
    cpu_usage = FloatField()
    gpu_usage = FloatField()

class RegisterRequest(WorkerRequest):
    timeout = IntField(
        default=int(config.get("services.workers.default_worker_timeout_sec", 10 * 60))
    )
    """ registration timeout in seconds (default is 10min) """
    queues = ListField(six.string_types)  # list of queues this worker listens to
    resources = EmbeddedField(Resources)


class MachineStats(Base):
    cpu_usage = ListField(six.integer_types + (float,))
    cpu_temperature = ListField(six.integer_types + (float,))
    gpu_usage = ListField(six.integer_types + (float,))
    gpu_temperature = ListField(six.integer_types + (float,))
    gpu_memory_free = ListField(six.integer_types + (float,))
    gpu_memory_used = ListField(six.integer_types + (float,))
    memory_used = FloatField()
    memory_free = FloatField()
    network_tx = FloatField()
    network_rx = FloatField()
    disk_free_home = FloatField()
    disk_free_temp = FloatField()
    disk_read = FloatField()
    disk_write = FloatField()


class StatusReportRequest(WorkerRequest):
    task = StringField()  # task the worker is running on
    queue = StringField()  # queue from which task was taken
    queues = ListField(
        str
    )  # list of queues this worker listens to. if None, this will not update the worker's queues list.
    timestamp = IntField(required=True)
    machine_stats = EmbeddedField(MachineStats)


class IdNameEntry(Base):
    id = StringField(required=True)
    name = StringField()


class WorkerEntry(Base, JsonSerializableMixin):
    key = StringField()  # not required due to migration issues
    id = StringField(required=True)
    user = EmbeddedField(IdNameEntry)
    company = EmbeddedField(IdNameEntry)
    ip = StringField()
    task = EmbeddedField(IdNameEntry)
    project = EmbeddedField(IdNameEntry)
    queue = StringField()  # queue from which current task was taken
    queues = ListField(str)  # list of queues this worker listens to
    register_time = DateTimeField(required=True)
    register_timeout = IntField(required=True)
    last_activity_time = DateTimeField(required=True)
    last_report_time = DateTimeField()
    tags = ListField(str)
    system_tags = ListField(str)


class CurrentTaskEntry(IdNameEntry):
    running_time = IntField()
    last_iteration = IntField()


class QueueEntry(IdNameEntry):
    display_name = StringField()
    next_task = EmbeddedField(IdNameEntry)
    num_tasks = IntField()


class WorkerResponseEntry(WorkerEntry):
    task = EmbeddedField(CurrentTaskEntry)
    queue = EmbeddedField(QueueEntry)
    queues = ListField(QueueEntry)


class GetAllRequest(Base):
    last_seen = IntField(default=3600)
    tags = ListField(str)
    system_tags = ListField(str)
    worker_pattern = StringField()


class GetAllResponse(Base):
    workers = ListField(WorkerResponseEntry)


class GetCountRequest(GetAllRequest):
    last_seen = IntField(default=0)


class StatsBase(Base):
    worker_ids = ListField(str)


class StatsReportBase(StatsBase):
    from_date = FloatField(required=True, validators=validators.Min(0))
    to_date = FloatField(required=True, validators=validators.Min(0))
    interval = IntField(required=True, validators=validators.Min(1))


class AggregationType(Enum):
    avg = "avg"
    min = "min"
    max = "max"


class StatItem(Base):
    key = StringField(required=True)
    aggregation = ActualEnumField(AggregationType, default=AggregationType.avg)


class GetStatsRequest(StatsReportBase):
    items = ListField(
        StatItem, required=True, validators=validators.Length(minimum_value=1)
    )
    split_by_variant = BoolField(default=False)
    split_by_resource = BoolField(default=False)


class MetricResourceSeries(Base):
    name = StringField()
    values = ListField(float)


class AggregationStats(Base):
    aggregation = EnumField(AggregationType)
    dates = ListField(int)
    values = ListField(float)
    resource_series = ListField(MetricResourceSeries)


class MetricStats(Base):
    metric = StringField()
    variant = StringField()
    stats = ListField(AggregationStats)


class WorkerStatistics(Base):
    worker = StringField()
    metrics = ListField(MetricStats)


class GetStatsResponse(Base):
    workers = ListField(WorkerStatistics)


class GetMetricKeysRequest(StatsBase):
    pass


class MetricCategory(Base):
    name = StringField()
    metric_keys = ListField(str)


class GetMetricKeysResponse(Base):
    categories = ListField(MetricCategory)


class GetActivityReportRequest(StatsReportBase):
    pass


class ActivityReportSeries(Base):
    dates = ListField(int)
    counts = ListField(int)


class GetActivityReportResponse(Base):
    total = EmbeddedField(ActivityReportSeries)
    active = EmbeddedField(ActivityReportSeries)
