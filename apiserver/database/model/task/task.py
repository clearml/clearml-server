from typing import Dict, Sequence

from mongoengine import (
    StringField,
    EmbeddedDocumentField,
    EmbeddedDocument,
    DateTimeField,
    IntField,
    ListField,
    LongField,
)

from apiserver.database import Database, strict
from apiserver.database.fields import (
    StrippedStringField,
    SafeMapField,
    SafeDictField,
    UnionField,
    SafeSortedListField,
    EmbeddedDocumentListField,
    NullableStringField,
    NoneType,
)
from apiserver.database.model import AttributedDocument
from apiserver.database.model.base import ProperDictMixin, GetMixin
from apiserver.database.model.model_labels import ModelLabels
from apiserver.database.model.project import Project
from apiserver.database.utils import get_options
from .metrics import MetricEvent, MetricEventStats
from .output import Output

DEFAULT_LAST_ITERATION = 0


class TaskStatus(object):
    created = "created"
    queued = "queued"
    in_progress = "in_progress"
    stopped = "stopped"
    publishing = "publishing"
    published = "published"
    closed = "closed"
    failed = "failed"
    completed = "completed"
    unknown = "unknown"


class TaskStatusMessage(object):
    stopping = "stopping"


class TaskSystemTags(object):
    development = "development"


class Script(EmbeddedDocument, ProperDictMixin):
    binary = StringField(default="python", strip=True)
    repository = StringField(default="", strip=True)
    tag = StringField(strip=True)
    branch = StringField(strip=True)
    version_num = StringField(strip=True)
    entry_point = StringField(default="", strip=True)
    working_dir = StringField(strip=True)
    requirements = SafeDictField()
    diff = StringField()


class ArtifactTypeData(EmbeddedDocument):
    preview = StringField()
    content_type = StringField()
    data_hash = StringField()


class ArtifactModes:
    input = "input"
    output = "output"


DEFAULT_ARTIFACT_MODE = ArtifactModes.output


class Artifact(EmbeddedDocument):
    key = StringField(required=True)
    type = StringField(required=True)
    mode = StringField(
        choices=get_options(ArtifactModes), default=DEFAULT_ARTIFACT_MODE
    )
    uri = StringField()
    hash = StringField()
    content_size = LongField()
    timestamp = LongField()
    type_data = EmbeddedDocumentField(ArtifactTypeData)
    display_data = SafeSortedListField(
        ListField(UnionField((int, float, str, NoneType)))
    )


class ParamsItem(EmbeddedDocument, ProperDictMixin):
    section = StringField(required=True)
    name = StringField(required=True)
    value = StringField(required=True)
    type = StringField()
    description = StringField()


class ConfigurationItem(EmbeddedDocument, ProperDictMixin):
    name = StringField(required=True)
    value = StringField(required=True)
    type = StringField()
    description = StringField()


class TaskModelTypes:
    input = "input"
    output = "output"


TaskModelNames = {
    TaskModelTypes.input: "Input Model",
    TaskModelTypes.output: "Output Model",
}


class ModelItem(EmbeddedDocument, ProperDictMixin):
    name = StringField(required=True)
    model = StringField(required=True, reference_field="Model")
    updated = DateTimeField()


class Models(EmbeddedDocument, ProperDictMixin):
    input: Sequence[ModelItem] = EmbeddedDocumentListField(ModelItem, default=list)
    output: Sequence[ModelItem] = EmbeddedDocumentListField(ModelItem, default=list)


class Execution(EmbeddedDocument, ProperDictMixin):
    meta = {"strict": strict}
    test_split = IntField(default=0)
    parameters = SafeDictField(default=dict)
    model_desc = SafeMapField(StringField(default=""))
    model_labels = ModelLabels()
    framework = StringField()
    artifacts: Dict[str, Artifact] = SafeMapField(field=EmbeddedDocumentField(Artifact))
    queue = StringField(reference_field="Queue")
    """ Queue ID where task was queued """


class TaskType(object):
    training = "training"
    testing = "testing"
    inference = "inference"
    data_processing = "data_processing"
    application = "application"
    monitor = "monitor"
    controller = "controller"
    report = "report"
    optimizer = "optimizer"
    service = "service"
    qc = "qc"
    custom = "custom"


external_task_types = set(get_options(TaskType))


class Task(AttributedDocument):
    _field_collation_overrides = {
        "execution.parameters.": AttributedDocument._numeric_locale,
        "last_metrics.": AttributedDocument._numeric_locale,
        "hyperparams.": AttributedDocument._numeric_locale,
    }

    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": [
            "created",
            "started",
            "completed",
            "active_duration",
            "parent",
            "project",
            "last_update",
            "status_changed",
            "models.input.model",
            ("company", "name"),
            ("company", "status", "type"),
            ("company", "last_update", "system_tags"),
            ("company", "type", "system_tags", "status"),
            ("company", "project", "type", "system_tags", "status"),
            ("status", "last_update"),  # for maintenance tasks
            {
                "fields": ["company", "project"],
                "collation": AttributedDocument._numeric_locale,
            },
            # distinct queries support
            ("company", "tags"),
            ("company", "system_tags"),
            ("company", "project", "tags"),
            ("company", "project", "system_tags"),
            ("company", "user"),
            ("company", "project", "user"),
            ("company", "parent"),
            ("company", "project", "parent"),
            ("company", "type"),
            ("company", "project", "type"),
            {
                "name": "%s.task.main_text_index" % Database.backend,
                "fields": [
                    "$name",
                    "$id",
                    "$comment",
                    "$report",
                    "$models.input.model",
                    "$models.output.model",
                    "$script.repository",
                    "$script.entry_point",
                ],
                "default_language": "english",
                "weights": {
                    "name": 10,
                    "id": 10,
                    "comment": 10,
                    "report": 10,
                    "models.output.model": 2,
                    "models.input.model": 2,
                    "script.repository": 1,
                    "script.entry_point": 1,
                },
            },
        ],
    }
    get_all_query_options = GetMixin.QueryParameterOptions(
        list_fields=(
            "id",
            "user",
            "tags",
            "system_tags",
            "type",
            "status",
            "project",
            "parent",
            "hyperparams.*",
            "execution.queue",
            "models.input.model",
        ),
        range_fields=("created", "started", "active_duration", "last_metrics.*", "last_iteration"),
        datetime_fields=("status_changed", "last_update", "last_change"),
        pattern_fields=("name", "comment", "report"),
        fields=("runtime.*",),
    )

    id = StringField(primary_key=True)
    name = StrippedStringField(
        required=True, user_set_allowed=True, sparse=False, min_length=3
    )

    type = StringField(required=True, choices=get_options(TaskType))
    status = StringField(default=TaskStatus.created, choices=get_options(TaskStatus))
    status_reason = StringField()
    status_message = StringField(user_set_allowed=True)
    status_changed = DateTimeField()
    comment = StringField(user_set_allowed=True)
    report = StringField()
    report_assets = ListField(StringField())
    created = DateTimeField(required=True, user_set_allowed=True)
    started = DateTimeField()
    completed = DateTimeField()
    published = DateTimeField()
    active_duration = IntField(default=None)
    parent = StringField(reference_field="Task")
    project = StringField(reference_field=Project, user_set_allowed=True)
    output: Output = EmbeddedDocumentField(Output, default=Output)
    execution: Execution = EmbeddedDocumentField(Execution, default=Execution)
    tags = SafeSortedListField(StringField(required=True), user_set_allowed=True)
    system_tags = SafeSortedListField(StringField(required=True), user_set_allowed=True)
    script: Script = EmbeddedDocumentField(Script, default=Script)
    last_worker = StringField()
    last_worker_report = DateTimeField()
    last_update = DateTimeField()
    last_change = DateTimeField()
    last_iteration = IntField(default=DEFAULT_LAST_ITERATION)
    last_metrics = SafeMapField(field=SafeMapField(EmbeddedDocumentField(MetricEvent)))
    unique_metrics = ListField(StringField(required=True), exclude_by_default=True)
    metric_stats = SafeMapField(field=EmbeddedDocumentField(MetricEventStats))
    company_origin = StringField(exclude_by_default=True)
    duration = IntField()  # obsolete, do not use
    hyperparams = SafeMapField(field=SafeMapField(EmbeddedDocumentField(ParamsItem)))
    configuration = SafeMapField(field=EmbeddedDocumentField(ConfigurationItem))
    runtime = SafeDictField(default=dict)
    models: Models = EmbeddedDocumentField(Models, default=Models)
    container = SafeMapField(field=NullableStringField())
    enqueue_status = StringField(
        choices=get_options(TaskStatus), exclude_by_default=True
    )
    last_changed_by = StringField()

    def get_index_company(self) -> str:
        """
        Returns the company ID used for locating indices containing task data.
        In case the task has a valid company, this is the company ID.
        Otherwise, if the task has a company_origin, this is a task that has been made public and the
         origin company should be used.
        Otherwise, an empty company is used.
        """
        return self.company or self.company_origin or ""
