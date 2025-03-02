from jsonmodels import validators
from jsonmodels.fields import StringField, IntField, BoolField, FloatField
from jsonmodels.models import Base

from apiserver.apimodels import ListField, DictField
from apiserver.apimodels.metadata import (
    MetadataItem,
    DeleteMetadata,
    AddOrUpdateMetadata,
)


class GetDefaultResp(Base):
    id = StringField(required=True)
    name = StringField(required=True)


class CreateRequest(Base):
    name = StringField(required=True)
    display_name = StringField()
    tags = ListField(items_types=[str])
    system_tags = ListField(items_types=[str])
    metadata = DictField(value_types=[MetadataItem])


class QueueRequest(Base):
    queue = StringField(required=True)


class GetByIdRequest(QueueRequest):
    max_task_entries = IntField()


class GetAllRequest(Base):
    max_task_entries = IntField()
    search_hidden = BoolField(default=False)


class GetNextTaskRequest(QueueRequest):
    queue = StringField(required=True)
    get_task_info = BoolField(default=False)
    task = StringField()


class DeleteRequest(QueueRequest):
    force = BoolField(default=False)


class UpdateRequest(QueueRequest):
    name = StringField()
    display_name = StringField()
    tags = ListField(items_types=[str])
    system_tags = ListField(items_types=[str])
    metadata = DictField(value_types=[MetadataItem])


class TaskRequest(QueueRequest):
    task = StringField(required=True)


class RemoveTaskRequest(TaskRequest):
    update_task_status = BoolField(default=False)


class AddTaskRequest(TaskRequest):
    update_execution_queue = BoolField(default=True)


class MoveTaskRequest(TaskRequest):
    count = IntField(default=1)


class MoveTaskResponse(Base):
    position = IntField()


class GetMetricsRequest(Base):
    queue_ids = ListField([str])
    from_date = FloatField(required=True, validators=validators.Min(0))
    to_date = FloatField(required=True, validators=validators.Min(0))
    interval = IntField(required=True, validators=validators.Min(1))
    refresh = BoolField(default=False)


class QueueMetrics(Base):
    queue = StringField()
    dates = ListField(int)
    avg_waiting_times = ListField([float, int])
    queue_lengths = ListField(int)


class GetMetricsResponse(Base):
    queues = ListField(QueueMetrics)


class DeleteMetadataRequest(DeleteMetadata):
    queue = StringField(required=True)


class AddOrUpdateMetadataRequest(AddOrUpdateMetadata):
    queue = StringField(required=True)
