from collections import defaultdict
from concurrent.futures.thread import ThreadPoolExecutor
from functools import partial
from typing import Optional, Sequence

from apiserver.apierrors import errors
from apiserver.apierrors.errors import bad_request
from apiserver.apimodels.workers import AggregationType, GetStatsRequest, StatItem
from apiserver.bll.query import Builder as QueryBuilder
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context

log = config.logger(__file__)


class WorkerStats:
    min_chart_interval = config.get("services.workers.min_chart_interval_sec", 40)
    _max_metrics_concurrency = config.get("services.events.events_retrieval.max_metrics_concurrency", 4)

    def __init__(self, es):
        self.es = es

    @staticmethod
    def worker_stats_prefix_for_company(company_id: str) -> str:
        """Returns the es index prefix for the company"""
        return f"worker_stats_{company_id.lower()}_"

    def search_company_stats(self, company_id: str, es_req: dict) -> dict:
        return self.es.search(
            index=f"{self.worker_stats_prefix_for_company(company_id)}*",
            body=es_req,
        )

    def get_worker_stats_keys(
        self, company_id: str, worker_ids: Optional[Sequence[str]]
    ) -> dict:
        """
        Get dictionary of metric types grouped by categories
        :param company_id: company id
        :param worker_ids: optional list of workers to get metric types from.
        If not specified them metrics for all the company workers returned
        :return:
        """
        es_req = {
            "size": 0,
            "aggs": {
                "categories": {
                    "terms": {"field": "category"},
                    "aggs": {"metrics": {"terms": {"field": "metric"}}},
                }
            },
        }
        if worker_ids:
            es_req["query"] = QueryBuilder.terms("worker", worker_ids)

        res = self.search_company_stats(company_id, es_req)

        if not res["hits"]["total"]["value"]:
            raise bad_request.WorkerStatsNotFound(
                f"No statistic metrics found for the company {company_id} and workers {worker_ids}"
            )

        return {
            category["key"]: [
                metric["key"] for metric in category["metrics"]["buckets"]
            ]
            for category in res["aggregations"]["categories"]["buckets"]
        }

    def _get_worker_stats_per_metric(
        self,
        metric_item: StatItem,
        company_id: str,
        from_date: float,
        to_date: float,
        interval: int,
        split_by_resource: bool,
        worker_ids: Sequence[str],
    ):
        agg_types_to_es = {
            AggregationType.avg: "avg",
            AggregationType.min: "min",
            AggregationType.max: "max",
        }
        agg = {
            metric_item.aggregation.value: {
                agg_types_to_es[metric_item.aggregation]: {"field": "value", "missing": 0.0 }
            }
        }
        split_by_resource = split_by_resource and metric_item.key.startswith("gpu_")
        if split_by_resource:
            split_aggs = {"split": {"terms": {"field": "variant"}, "aggs": agg}}
        else:
            split_aggs = {}

        es_req = {
            "size": 0,
            "aggs": {
                "workers": {
                    "terms": {"field": "worker"},
                    "aggs": {
                        "dates": {
                            "date_histogram": {
                                "field": "timestamp",
                                "fixed_interval": f"{interval}s",
                                "extended_bounds": {
                                    "min": int(from_date) * 1000,
                                    "max": int(to_date) * 1000,
                                },
                            },
                            "aggs": {
                                **agg,
                                **split_aggs,
                            },
                        }
                    },
                }
            },
        }

        query_terms = [
            QueryBuilder.dates_range(from_date, to_date),
            QueryBuilder.term("metric", metric_item.key),
        ]
        if worker_ids:
            query_terms.append(QueryBuilder.terms("worker", worker_ids))
        es_req["query"] = {"bool": {"must": query_terms}}

        with translate_errors_context():
            data = self.search_company_stats(company_id, es_req)

        cutoff_date = (
            to_date - 0.9 * interval
        ) * 1000  # do not return the point for the incomplete last interval
        return self._extract_results(
            data, metric_item, split_by_resource, cutoff_date
        )

    def get_worker_stats(self, company_id: str, request: GetStatsRequest) -> dict:
        """
        Get statistics for company workers metrics in the specified time range
        Returned as date histograms for different aggregation types
        grouped by worker, metric type (and optionally metric variant)
        Buckets with no metrics are not returned
        Note: all the statistics are retrieved as one ES query
        """
        from_date = request.from_date
        to_date = request.to_date
        if from_date >= to_date:
            raise errors.bad_request.FieldsValueError(
                "from_date must be less than to_date"
            )

        interval = max(request.interval, self.min_chart_interval)
        with ThreadPoolExecutor(self._max_metrics_concurrency) as pool:
            res = list(
                pool.map(
                    partial(
                        self._get_worker_stats_per_metric,
                        company_id=company_id,
                        from_date=from_date,
                        to_date=to_date,
                        interval=interval,
                        split_by_resource=request.split_by_resource,
                        worker_ids=request.worker_ids,
                    ),
                    request.items,
                )
            )

        ret = defaultdict(lambda: defaultdict(dict))
        for workers in res:
            for worker, metrics in workers.items():
                for metric, stats in metrics.items():
                    ret[worker][metric].update(stats)

        return ret

    @staticmethod
    def _extract_results(
        data: dict,
        metric_item: StatItem,
        split_by_resource: bool,
        cutoff_date,
    ) -> dict:
        """
        Clean results returned from elastic search (remove "aggregations", "buckets" etc.),
        leave only aggregation types requested by the user and return a clean dictionary
        :param data: aggregation data retrieved from ES
        """
        if "aggregations" not in data:
            return {}

        def extract_metric_results(metric: dict) -> dict:
            aggregation = metric_item.aggregation.value
            date_buckets = metric["dates"]["buckets"]
            length = len(date_buckets)
            while length > 0 and date_buckets[length - 1]["key"] >= cutoff_date:
                length -= 1

            dates = [None] * length
            agg_values = [0.0] * length
            resource_series = defaultdict(lambda: [0.0] * length)

            for idx in range(0, length):
                date = date_buckets[idx]
                dates[idx] = date["key"]
                if aggregation in date:
                    agg_values[idx] = date[aggregation]["value"] or 0.0

                if split_by_resource and "split" in date:
                    for resource in date["split"]["buckets"]:
                        series = resource_series[resource["key"]]
                        if aggregation in resource:
                            series[idx] = resource[aggregation]["value"] or 0.0

            if len(resource_series) == 1:
                resource_series = {}

            return {
                "dates": dates,
                "values": agg_values,
                **(
                    {"resource_series": resource_series} if resource_series else {}
                ),
            }

        return {
            worker["key"]: {
                metric_item.key: {
                    metric_item.aggregation.value: extract_metric_results(worker)
                }
            }
            for worker in data["aggregations"]["workers"]["buckets"]
        }

    def get_activity_report(
        self,
        company_id: str,
        from_date: float,
        to_date: float,
        interval: int,
        active_only: bool,
    ) -> Sequence[dict]:
        """
        Get statistics for company workers metrics in the specified time range
        Returned as date histograms for different aggregation types
        grouped by worker, metric type (and optionally metric variant)
        Note: all the statistics are retrieved using one ES query
        """
        if from_date >= to_date:
            raise bad_request.FieldsValueError("from_date must be less than to_date")
        interval = max(interval, self.min_chart_interval)

        must = [QueryBuilder.dates_range(from_date, to_date)]
        if active_only:
            must.append({"exists": {"field": "task"}})

        es_req = {
            "size": 0,
            "aggs": {
                "dates": {
                    "date_histogram": {
                        "field": "timestamp",
                        "fixed_interval": f"{interval}s",
                        "extended_bounds": {
                          "min": int(from_date) * 1000,
                          "max": int(to_date) * 1000,
                        }
                    },
                    "aggs": {"workers_count": {"cardinality": {"field": "worker"}}},
                }
            },
            "query": {"bool": {"must": must}},
        }

        with translate_errors_context():
            data = self.search_company_stats(company_id, es_req)

        if "aggregations" not in data:
            return {}

        ret = [
            dict(date=date["key"], count=date["workers_count"]["value"])
            for date in data["aggregations"]["dates"]["buckets"]
        ]

        if ret and ret[-1]["date"] > (to_date - 0.9 * interval):
            # remove last interval if it's incomplete. Allow 10% tolerance
            ret.pop()

        return ret
