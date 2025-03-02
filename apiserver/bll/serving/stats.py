from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum

from typing import Tuple, Optional, Sequence

from elasticsearch import Elasticsearch

from apiserver.apimodels.serving import (
    ServingContainerEntry,
    GetEndpointMetricsHistoryRequest,
    MetricType,
)
from apiserver.apierrors import errors
from apiserver.utilities.dicts import nested_get
from apiserver.bll.query import Builder as QueryBuilder
from apiserver.config_repo import config
from apiserver.es_factory import es_factory


class _AggregationType(Enum):
    avg = "avg"
    sum = "sum"


class ServingStats:
    min_chart_interval = config.get("services.serving.min_chart_interval_sec", 40)
    es: Elasticsearch = es_factory.connect("workers")

    @classmethod
    def _serving_stats_prefix(cls, company_id: str) -> str:
        """Returns the es index prefix for the company"""
        return f"serving_stats_{company_id.lower()}_"

    @staticmethod
    def _get_es_index_suffix():
        """Get the index name suffix for storing current month data"""
        return datetime.now(timezone.utc).strftime("%Y-%m")

    @staticmethod
    def _get_average_value(value) -> Tuple[Optional[float], Optional[int]]:
        if value is None:
            return None, None

        if isinstance(value, (list, tuple)):
            count = len(value)
            if not count:
                return None, None
            return sum(value) / count, count

        return value, 1

    @classmethod
    def log_stats_to_es(
        cls,
        entry: ServingContainerEntry,
    ) -> int:
        """
        Actually writing the worker statistics to Elastic
        :return: The amount of logged documents
        """
        company_id = entry.company_id
        es_index = (
            f"{cls._serving_stats_prefix(company_id)}" f"{cls._get_es_index_suffix()}"
        )

        entry_data = entry.to_struct()
        doc = {
            "timestamp": es_factory.get_timestamp_millis(),
            **{
                field: entry_data.get(field)
                for field in (
                    "container_id",
                    "company_id",
                    "endpoint_url",
                    "requests_num",
                    "requests_min",
                    "uptime_sec",
                    "latency_ms",
                )
            },
        }

        stats = entry_data.get("machine_stats")
        if stats:
            for category in ("cpu", "gpu"):
                usage, num = cls._get_average_value(stats.get(f"{category}_usage"))
                doc.update({f"{category}_usage": usage, f"{category}_num": num})

            for category in ("memory", "gpu_memory"):
                free, _ = cls._get_average_value(stats.get(f"{category}_free"))
                used, _ = cls._get_average_value(stats.get(f"{category}_used"))
                doc.update(
                    {
                        f"{category}_free": free,
                        f"{category}_used": used,
                        f"{category}_total": round((free or 0) + (used or 0), 3),
                    }
                )

            doc.update(
                {
                    field: stats.get(field)
                    for field in ("disk_free_home", "network_rx", "network_tx")
                }
            )

        cls.es.index(index=es_index, document=doc)

        return 1

    @staticmethod
    def round_series(values: Sequence, koeff) -> list:
        return [round(v * koeff, 2) if v else 0 for v in values]

    _mb_to_gb = 1 / 1024
    agg_fields = {
        MetricType.requests: (
            "requests_num",
            "Number of Requests",
            _AggregationType.sum,
            None,
        ),
        MetricType.requests_min: (
            "requests_min",
            "Requests per Minute",
            _AggregationType.sum,
            None,
        ),
        MetricType.latency_ms: (
            "latency_ms",
            "Average Latency (ms)",
            _AggregationType.avg,
            None,
        ),
        MetricType.cpu_count: ("cpu_num", "CPU Count", _AggregationType.sum, None),
        MetricType.gpu_count: ("gpu_num", "GPU Count", _AggregationType.sum, None),
        MetricType.cpu_util: (
            "cpu_usage",
            "Average CPU Load (%)",
            _AggregationType.avg,
            None,
        ),
        MetricType.gpu_util: (
            "gpu_usage",
            "Average GPU Utilization (%)",
            _AggregationType.avg,
            None,
        ),
        MetricType.ram_total: (
            "memory_total",
            "RAM Total (GB)",
            _AggregationType.sum,
            _mb_to_gb,
        ),
        MetricType.ram_used: (
            "memory_used",
            "RAM Used (GB)",
            _AggregationType.sum,
            _mb_to_gb,
        ),
        MetricType.ram_free: (
            "memory_free",
            "RAM Free (GB)",
            _AggregationType.sum,
            _mb_to_gb,
        ),
        MetricType.gpu_ram_total: (
            "gpu_memory_total",
            "GPU RAM Total (GB)",
            _AggregationType.sum,
            _mb_to_gb,
        ),
        MetricType.gpu_ram_used: (
            "gpu_memory_used",
            "GPU RAM Used (GB)",
            _AggregationType.sum,
            _mb_to_gb,
        ),
        MetricType.gpu_ram_free: (
            "gpu_memory_free",
            "GPU RAM Free (GB)",
            _AggregationType.sum,
            _mb_to_gb,
        ),
        MetricType.network_rx: (
            "network_rx",
            "Network Throughput RX (MBps)",
            _AggregationType.sum,
            None,
        ),
        MetricType.network_tx: (
            "network_tx",
            "Network Throughput TX (MBps)",
            _AggregationType.sum,
            None,
        ),
    }

    @classmethod
    def get_endpoint_metrics(
        cls,
        company_id: str,
        metrics_request: GetEndpointMetricsHistoryRequest,
    ) -> dict:
        from_date = metrics_request.from_date
        to_date = metrics_request.to_date
        if from_date >= to_date:
            raise errors.bad_request.FieldsValueError(
                "from_date must be less than to_date"
            )

        metric_type = metrics_request.metric_type
        agg_data = cls.agg_fields.get(metric_type)
        if not agg_data:
            raise NotImplemented(f"Charts for {metric_type} not implemented")

        agg_field, title, agg_type, multiplier = agg_data
        if agg_type == _AggregationType.sum:
            instance_sum_type = "sum_bucket"
        else:
            instance_sum_type = "avg_bucket"

        interval = max(metrics_request.interval, cls.min_chart_interval)
        endpoint_url = metrics_request.endpoint_url
        hist_ret = {
            "computed_interval": interval,
            "total": {
                "title": title,
                "dates": [],
                "values": [],
            },
            "instances": {},
        }
        must_conditions = [
            QueryBuilder.term("company_id", company_id),
            QueryBuilder.term("endpoint_url", endpoint_url),
            QueryBuilder.dates_range(from_date, to_date),
        ]
        query = {"bool": {"must": must_conditions}}
        es_index = f"{cls._serving_stats_prefix(company_id)}*"
        res = cls.es.search(
            index=es_index,
            size=0,
            query=query,
            aggs={"instances": {"terms": {"field": "container_id"}}},
        )
        instance_buckets = nested_get(res, ("aggregations", "instances", "buckets"))
        if not instance_buckets:
            return hist_ret

        instance_keys = {ib["key"] for ib in instance_buckets}
        must_conditions.append(QueryBuilder.terms("container_id", instance_keys))
        query = {"bool": {"must": must_conditions}}
        sample_func = "avg" if metric_type != MetricType.requests else "max"
        aggs = {
            "instances": {
                "terms": {
                    "field": "container_id",
                    "size": max(len(instance_keys), 10),
                },
                "aggs": {
                    "sample": {sample_func: {"field": agg_field}},
                },
            },
            "total_instances": {
                instance_sum_type: {
                    "gap_policy": "insert_zeros",
                    "buckets_path": "instances>sample",
                }
            },
        }
        hist_params = {}
        if metric_type == MetricType.requests:
            hist_params["min_doc_count"] = 1
        else:
            hist_params["extended_bounds"] = {
                "min": int(from_date) * 1000,
                "max": int(to_date) * 1000,
            }
        aggs = {
            "dates": {
                "date_histogram": {
                    "field": "timestamp",
                    "fixed_interval": f"{interval}s",
                    **hist_params,
                },
                "aggs": aggs,
            }
        }

        filter_path = None
        if not metrics_request.instance_charts:
            filter_path = "aggregations.dates.buckets.total_instances"

        data = cls.es.search(
            index=es_index,
            size=0,
            query=query,
            aggs=aggs,
            filter_path=filter_path,
        )
        agg_res = data.get("aggregations")
        if not agg_res:
            return hist_ret

        dates_ = []
        total = []
        instances = defaultdict(list)
        # remove last interval if it's incomplete. Allow 10% tolerance
        last_valid_timestamp = (to_date - 0.9 * interval) * 1000
        for point in agg_res["dates"]["buckets"]:
            date_ = point["key"]
            if date_ > last_valid_timestamp:
                break
            dates_.append(date_)
            total.append(nested_get(point, ("total_instances", "value"), 0))
            if metrics_request.instance_charts:
                found_keys = set()
                for instance in nested_get(point, ("instances", "buckets"), []):
                    instances[instance["key"]].append(
                        nested_get(instance, ("sample", "value"), 0)
                    )
                    found_keys.add(instance["key"])
                for missing_key in instance_keys - found_keys:
                    instances[missing_key].append(0)

        koeff = multiplier if multiplier else 1.0
        hist_ret["total"]["dates"] = dates_
        hist_ret["total"]["values"] = cls.round_series(total, koeff)
        hist_ret["instances"] = {
            key: {
                "title": key,
                "dates": dates_,
                "values": cls.round_series(values, koeff),
            }
            for key, values in sorted(instances.items(), key=lambda p: p[0])
        }

        return hist_ret
