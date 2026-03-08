import base64
import zlib
from enum import Enum
from typing import Union, Sequence, Mapping, Tuple

from boltons.typeutils import classproperty
from elasticsearch import Elasticsearch

from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.task.task import Task
from apiserver.utilities.dicts import nested_get


class EventType(Enum):
    metrics_scalar = "training_stats_scalar"
    metrics_vector = "training_stats_vector"
    metrics_image = "training_debug_image"
    metrics_plot = "plot"
    task_log = "log"
    all = "*"


SINGLE_SCALAR_ITERATION = -(2 ** 31)
MetricVariants = Mapping[str, Sequence[str]]
TaskCompanies = Mapping[str, Sequence[Task]]


class EventSettings:
    _max_es_allowed_aggregation_buckets = 10000

    @classproperty
    def max_workers(self):
        return config.get("services.events.events_retrieval.max_metrics_concurrency", 4)

    @classproperty
    def state_expiration_sec(self):
        return config.get(
            "services.events.events_retrieval.state_expiration_sec", 3600
        )

    @classproperty
    def max_es_buckets(self):
        percentage = (
            min(
                100,
                config.get(
                    "services.events.events_retrieval.dynamic_metrics_count_threshold",
                    80,
                ),
            )
            / 100
        )
        return int(self._max_es_allowed_aggregation_buckets * percentage)


def get_index_name(company_id: Union[str, Sequence[str]], event_type: str):
    event_type = event_type.lower().replace(" ", "_")
    if isinstance(company_id, str):
        company_id = [company_id]

    return ",".join(f"events-{event_type}-{(c_id or '').lower()}" for c_id in company_id)


def check_empty_data(es: Elasticsearch, company_id: str, event_type: EventType) -> bool:
    es_index = get_index_name(company_id, event_type.value)
    if not es.indices.exists(index=es_index):
        return True
    return False


def search_company_events(
    es: Elasticsearch,
    company_id: Union[str, Sequence[str]],
    event_type: EventType,
    body: dict,
    **kwargs,
) -> dict:
    es_index = get_index_name(company_id, event_type.value)
    return es.search(index=es_index, body=body, **kwargs)


def delete_company_events(
    es: Elasticsearch, company_id: str, event_type: EventType, body: dict, **kwargs
) -> dict:
    es_index = get_index_name(company_id, event_type.value)
    return es.delete_by_query(index=es_index, body=body, conflicts="proceed", **kwargs)


def count_company_events(
    es: Elasticsearch, company_id: str, event_type: EventType, body: dict, **kwargs
) -> dict:
    es_index = get_index_name(company_id, event_type.value)
    return es.count(index=es_index, body=body, **kwargs)


def get_max_metric_and_variant_counts(
    es: Elasticsearch,
    company_id: Union[str, Sequence[str]],
    event_type: EventType,
    query: dict,
    **kwargs,
) -> Tuple[int, int]:
    dynamic = config.get(
        "services.events.events_retrieval.dynamic_metrics_count", False
    )
    max_metrics_count = config.get(
        "services.events.events_retrieval.max_metrics_count", 100
    )
    max_variants_count = config.get(
        "services.events.events_retrieval.max_variants_count", 100
    )
    if not dynamic:
        return max_metrics_count, max_variants_count

    es_req: dict = {
        "size": 0,
        "query": query,
        "aggs": {"metrics_count": {"cardinality": {"field": "metric"}}},
    }
    with translate_errors_context():
        es_res = search_company_events(
            es, company_id=company_id, event_type=event_type, body=es_req, **kwargs,
        )

    metrics_count = nested_get(
        es_res, ("aggregations", "metrics_count", "value"), max_metrics_count
    )
    if not metrics_count:
        return max_metrics_count, max_variants_count

    return metrics_count, int(EventSettings.max_es_buckets / metrics_count)


def get_metric_variants_condition(metric_variants: MetricVariants,) -> Sequence:
    conditions = [
        {
            "bool": {
                "must": [
                    {"term": {"metric": metric}},
                    {"terms": {"variant": variants}},
                ]
            }
        }
        if variants
        else {"term": {"metric": metric}}
        for metric, variants in metric_variants.items()
    ]

    return {"bool": {"should": conditions}}


class PlotFields:
    valid_plot = "valid_plot"
    plot_len = "plot_len"
    plot_str = "plot_str"
    plot_data = "plot_data"
    source_urls = "source_urls"


def uncompress_plot(event: dict):
    plot_data = event.pop(PlotFields.plot_data, None)
    if plot_data and event.get(PlotFields.plot_str) is None:
        event[PlotFields.plot_str] = zlib.decompress(
            base64.b64decode(plot_data)
        ).decode()
