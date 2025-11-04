from typing import Sequence

from jsonmodels import models
from jsonmodels.fields import StringField, BoolField, IntField, EmbeddedField
from jsonmodels.validators import Enum, Length

from apiserver.apimodels import DictField, ListField
from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.batch import BatchRequest, UpdateBatchItem, BatchResponse
from apiserver.database.model.task.task import (
    TaskType,
    ArtifactModes,
    DEFAULT_ARTIFACT_MODE,
    TaskModelTypes,
)
from apiserver.database.utils import get_options


class ArtifactTypeData(models.Base):
    preview = StringField()
    content_type = StringField()
    data_hash = StringField()


class Artifact(models.Base):
    key = StringField(required=True)
    type = StringField(required=True)
    mode = StringField(
        validators=Enum(*get_options(ArtifactModes)), default=DEFAULT_ARTIFACT_MODE
    )
    uri = StringField()
    hash = StringField()
    content_size = IntField()
    timestamp = IntField()
    type_data = EmbeddedField(ArtifactTypeData)
    display_data = ListField([list])


class StartedResponse(UpdateResponse):
    started = IntField()


class EnqueueResponse(UpdateResponse):
    queued = IntField()
    queue_watched = BoolField()


class EnqueueBatchItem(UpdateBatchItem):
    queued: bool = BoolField()


class EnqueueManyResponse(BatchResponse):
    succeeded: Sequence[EnqueueBatchItem] = ListField(EnqueueBatchItem)
    queue_watched = BoolField()


class DequeueResponse(UpdateResponse):
    dequeued = IntField()


class DequeueBatchItem(UpdateBatchItem):
    dequeued: bool = BoolField()


class DequeueManyResponse(BatchResponse):
    succeeded: Sequence[DequeueBatchItem] = ListField(DequeueBatchItem)


class ResetResponse(UpdateResponse):
    dequeued = DictField()
    events = DictField()
    deleted_models = IntField()
    urls = DictField()


class ResetBatchItem(UpdateBatchItem):
    dequeued: bool = BoolField()
    deleted_models = IntField()
    urls = DictField()


class ResetManyResponse(BatchResponse):
    succeeded: Sequence[ResetBatchItem] = ListField(ResetBatchItem)


class TaskRequest(models.Base):
    task = StringField(required=True)


class TaskUpdateRequest(TaskRequest):
    force = BoolField(default=False)


class UpdateRequest(TaskUpdateRequest):
    status_reason = StringField(default="")
    status_message = StringField(default="")


class DequeueRequest(UpdateRequest):
    remove_from_all_queues = BoolField(default=False)
    new_status = StringField()


class StopRequest(UpdateRequest):
    include_pipeline_steps = BoolField(default=False)


class EnqueueRequest(UpdateRequest):
    queue = StringField()
    queue_name = StringField()
    verify_watched_queue = BoolField(default=False)
    update_execution_queue = BoolField(default=True)


class DeleteRequest(UpdateRequest):
    move_to_trash = BoolField(default=False)
    return_file_urls = BoolField(default=False)
    delete_output_models = BoolField(default=True)
    delete_external_artifacts = BoolField(default=True)
    include_pipeline_steps = BoolField(default=False)


class SetRequirementsRequest(TaskRequest):
    requirements = DictField(required=True)


class CompletedRequest(UpdateRequest):
    publish = BoolField(default=False)


class CompletedResponse(UpdateResponse):
    published = IntField(default=0)


class PublishRequest(UpdateRequest):
    publish_model = BoolField(default=True)


class TaskData(models.Base):
    """
    This is a partial description of task can be updated incrementally
    """


class CreateRequest(TaskData):
    name = StringField(required=True)
    type = StringField(required=True, validators=Enum(*get_options(TaskType)))


class PingRequest(TaskRequest):
    pass


class EditRuntimeRequest(TaskUpdateRequest):
    add_or_update = DictField()
    remove = ListField(items_types=[str])


class GetTypesRequest(models.Base):
    projects = ListField(items_types=[str])


class TaskInputModel(models.Base):
    name = StringField()
    model = StringField()


class CloneRequest(TaskRequest):
    new_task_name = StringField()
    new_task_comment = StringField()
    new_task_tags = ListField([str])
    new_task_system_tags = ListField([str])
    new_task_parent = StringField()
    new_task_project = StringField()
    new_task_hyperparams = DictField()
    new_task_configuration = DictField()
    new_task_container = DictField()
    new_task_input_models = ListField([TaskInputModel])
    execution_overrides = DictField()
    validate_references = BoolField(default=False)
    new_project_name = StringField()


class AddOrUpdateArtifactsRequest(TaskUpdateRequest):
    artifacts = ListField([Artifact], validators=Length(minimum_value=1))


class ArtifactId(models.Base):
    key = StringField(required=True)
    mode = StringField(
        validators=Enum(*get_options(ArtifactModes)), default=DEFAULT_ARTIFACT_MODE
    )


class DeleteArtifactsRequest(TaskUpdateRequest):
    artifacts = ListField([ArtifactId], validators=Length(minimum_value=1))


class ResetRequest(UpdateRequest):
    clear_all = BoolField(default=False)
    return_file_urls = BoolField(default=False)
    delete_output_models = BoolField(default=True)
    delete_external_artifacts = BoolField(default=True)


class MultiTaskRequest(models.Base):
    tasks = ListField([str], validators=Length(minimum_value=1))


class GetHyperParamsRequest(MultiTaskRequest):
    pass


class HyperParamItem(models.Base):
    section = StringField(required=True, validators=Length(minimum_value=1))
    name = StringField(required=True, validators=Length(minimum_value=1))
    value = StringField(required=True)
    type = StringField()
    description = StringField()


class ReplaceHyperparams(object):
    none = "none"
    section = "section"
    all = "all"


class EditHyperParamsRequest(TaskUpdateRequest):
    hyperparams: Sequence[HyperParamItem] = ListField(
        [HyperParamItem], validators=Length(minimum_value=1)
    )
    replace_hyperparams = StringField(
        validators=Enum(*get_options(ReplaceHyperparams)),
        default=ReplaceHyperparams.none,
    )


class HyperParamKey(models.Base):
    section = StringField(required=True, validators=Length(minimum_value=1))
    name = StringField(nullable=True)


class DeleteHyperParamsRequest(TaskUpdateRequest):
    hyperparams: Sequence[HyperParamKey] = ListField(
        [HyperParamKey], validators=Length(minimum_value=1)
    )


class GetConfigurationsRequest(MultiTaskRequest):
    names = ListField([str])


class GetConfigurationNamesRequest(MultiTaskRequest):
    skip_empty = BoolField(default=True)


class Configuration(models.Base):
    name = StringField(required=True, validators=Length(minimum_value=1))
    value = StringField(required=True)
    type = StringField()
    description = StringField()


class EditConfigurationRequest(TaskUpdateRequest):
    configuration: Sequence[Configuration] = ListField(
        [Configuration], validators=Length(minimum_value=1)
    )
    replace_configuration = BoolField(default=False)


class DeleteConfigurationRequest(TaskUpdateRequest):
    configuration: Sequence[str] = ListField([str], validators=Length(minimum_value=1))


class ArchiveRequest(MultiTaskRequest):
    status_reason = StringField(default="")
    status_message = StringField(default="")
    include_pipeline_steps = BoolField(default=False)


class ArchiveResponse(models.Base):
    archived = IntField()


class TaskBatchRequest(BatchRequest):
    status_reason = StringField(default="")
    status_message = StringField(default="")


class ArchiveManyRequest(TaskBatchRequest):
    include_pipeline_steps = BoolField(default=False)


class UnarchiveManyRequest(TaskBatchRequest):
    include_pipeline_steps = BoolField(default=False)


class StopManyRequest(TaskBatchRequest):
    force = BoolField(default=False)
    include_pipeline_steps = BoolField(default=False)


class DequeueManyRequest(TaskBatchRequest):
    remove_from_all_queues = BoolField(default=False)
    new_status = StringField()


class EnqueueManyRequest(TaskBatchRequest):
    queue = StringField()
    queue_name = StringField()
    validate_tasks = BoolField(default=False)
    verify_watched_queue = BoolField(default=False)


class DeleteManyRequest(TaskBatchRequest):
    move_to_trash = BoolField(default=False)
    return_file_urls = BoolField(default=False)
    delete_output_models = BoolField(default=True)
    force = BoolField(default=False)
    delete_external_artifacts = BoolField(default=True)
    include_pipeline_steps = BoolField(default=False)


class ResetManyRequest(TaskBatchRequest):
    clear_all = BoolField(default=False)
    return_file_urls = BoolField(default=False)
    delete_output_models = BoolField(default=True)
    force = BoolField(default=False)
    delete_external_artifacts = BoolField(default=True)


class PublishManyRequest(TaskBatchRequest):
    publish_model = BoolField(default=True)
    force = BoolField(default=False)


class AddUpdateModelRequest(TaskRequest):
    name = StringField(required=True)
    model = StringField(required=True)
    type = StringField(required=True, validators=Enum(*get_options(TaskModelTypes)))
    iteration = IntField()


class ModelItemKey(models.Base):
    name = StringField(required=True)
    type = StringField(required=True, validators=Enum(*get_options(TaskModelTypes)))


class DeleteModelsRequest(TaskRequest):
    models: Sequence[ModelItemKey] = ListField(
        [ModelItemKey], validators=Length(minimum_value=1)
    )


class GetAllReq(models.Base):
    allow_public = BoolField(default=True)
    search_hidden = BoolField(default=False)


class UpdateTagsRequest(BatchRequest):
    add_tags = ListField([str])
    remove_tags = ListField([str])
