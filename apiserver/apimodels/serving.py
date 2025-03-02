from enum import Enum
from typing import Sequence

from jsonmodels.models import Base
from jsonmodels.fields import (
    StringField,
    EmbeddedField,
    DateTimeField,
    IntField,
    FloatField,
    BoolField,
)
from jsonmodels import validators
from jsonmodels.validators import Min

from apiserver.apimodels import ListField, JsonSerializableMixin, SafeStringField
from apiserver.apimodels import ActualEnumField
from apiserver.config_repo import config
from .workers import MachineStats


class ReferenceItem(Base):
    type = StringField(
        required=True,
        validators=validators.Enum("app_id", "app_instance", "model", "task", "url"),
    )
    value = StringField(required=True)


class ServingModel(Base):
    container_id = StringField(required=True)
    endpoint_name = StringField(required=True)
    endpoint_url = StringField()  # can be not existing yet at registration time
    model_name = StringField(required=True)
    model_source = StringField()
    model_version = StringField()
    preprocess_artifact = StringField()
    input_type = StringField()
    input_size = SafeStringField()
    tags = ListField(str)
    system_tags = ListField(str)
    reference: Sequence[ReferenceItem] = ListField(ReferenceItem)


class RegisterRequest(ServingModel):
    timeout = IntField(
        default=int(
            config.get("services.serving.default_container_timeout_sec", 10 * 60)
        ),
        validators=[Min(1)],
    )
    """ registration timeout in seconds (default is 10min) """


class UnregisterRequest(Base):
    container_id = StringField(required=True)


class StatusReportRequest(ServingModel):
    uptime_sec = IntField()
    requests_num = IntField()
    requests_min = FloatField()
    latency_ms = IntField()
    machine_stats: MachineStats = EmbeddedField(MachineStats)


class ServingContainerEntry(StatusReportRequest, JsonSerializableMixin):
    key = StringField(required=True)
    company_id = StringField(required=True)
    ip = StringField()
    register_time = DateTimeField(required=True)
    register_timeout = IntField(required=True)
    last_activity_time = DateTimeField(required=True)


class GetEndpointDetailsRequest(Base):
    endpoint_url = StringField(required=True)


class MetricType(Enum):
    requests = "requests"
    requests_min = "requests_min"
    latency_ms = "latency_ms"
    cpu_count = "cpu_count"
    gpu_count = "gpu_count"
    cpu_util = "cpu_util"
    gpu_util = "gpu_util"
    ram_total = "ram_total"
    ram_used = "ram_used"
    ram_free = "ram_free"
    gpu_ram_total = "gpu_ram_total"
    gpu_ram_used = "gpu_ram_used"
    gpu_ram_free = "gpu_ram_free"
    network_rx = "network_rx"
    network_tx = "network_tx"


class GetEndpointMetricsHistoryRequest(Base):
    from_date = FloatField(required=True, validators=Min(0))
    to_date = FloatField(required=True, validators=Min(0))
    interval = IntField(required=True, validators=Min(1))
    endpoint_url = StringField(required=True)
    metric_type = ActualEnumField(MetricType, default=MetricType.requests)
    instance_charts = BoolField(default=True)
