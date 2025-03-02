from operator import attrgetter
from typing import Optional, Sequence

from boltons.iterutils import bucketize

from apiserver.apierrors.errors import bad_request
from apiserver.apimodels.workers import AggregationType, GetStatsRequest, StatItem
from apiserver.bll.query import Builder as QueryBuilder
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context

log = config.logger(__file__)


class WorkerStats:
    min_chart_interval = config.get("services.workers.min_chart_interval_sec", 40)

    def __init__(self, es):
        self.es = es

    @staticmethod
    def worker_stats_prefix_for_company(company_id: str) -> str:
        """Returns the es index prefix for the company"""
        return f"worker_stats_{company_id.lower()}_"

    def _search_company_stats(self, company_id: str, es_req: dict) -> dict:
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

        res = self._search_company_stats(company_id, es_req)

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
            raise bad_request.FieldsValueError("from_date must be less than to_date")

        interval = max(request.interval, self.min_chart_interval)

        def get_dates_agg() -> dict:
            es_to_agg_types = (
                ("avg", AggregationType.avg.value),
                ("min", AggregationType.min.value),
                ("max", AggregationType.max.value),
            )

            return {
                "dates": {
                    "date_histogram": {
                        "field": "timestamp",
                        "fixed_interval": f"{interval}s",
                        "extended_bounds": {
                          "min": int(from_date) * 1000,
                          "max": int(to_date) * 1000,
                        }
                    },
                    "aggs": {
                        agg_type: {es_agg: {"field": "value"}}
                        for es_agg, agg_type in es_to_agg_types
                    },
                }
            }

        def get_variants_agg() -> dict:
            return {
                "variants": {"terms": {"field": "variant"}, "aggs": get_dates_agg()}
            }

        es_req = {
            "size": 0,
            "aggs": {
                "workers": {
                    "terms": {"field": "worker"},
                    "aggs": {
                        "metrics": {
                            "terms": {"field": "metric"},
                            "aggs": get_variants_agg()
                            if request.split_by_variant
                            else get_dates_agg(),
                        }
                    },
                }
            },
        }

        query_terms = [
            QueryBuilder.dates_range(from_date, to_date),
            QueryBuilder.terms("metric", {item.key for item in request.items}),
        ]
        if request.worker_ids:
            query_terms.append(QueryBuilder.terms("worker", request.worker_ids))
        es_req["query"] = {"bool": {"must": query_terms}}

        with translate_errors_context():
            data = self._search_company_stats(company_id, es_req)

        cutoff_date = (to_date - 0.9 * interval) * 1000  # do not return the point for the incomplete last interval
        return self._extract_results(data, request.items, request.split_by_variant, cutoff_date)

    @staticmethod
    def _extract_results(
        data: dict, request_items: Sequence[StatItem], split_by_variant: bool, cutoff_date
    ) -> dict:
        """
        Clean results returned from elastic search (remove "aggregations", "buckets" etc.),
        leave only aggregation types requested by the user and return a clean dictionary
        :param data: aggregation data retrieved from ES
        :param request_items: aggs types requested by the user
        :param split_by_variant: if False then aggregate by metric type, otherwise metric type + variant
        """
        if "aggregations" not in data:
            return {}

        items_by_key = bucketize(request_items, key=attrgetter("key"))
        aggs_per_metric = {
            key: [item.aggregation for item in items]
            for key, items in items_by_key.items()
        }

        def extract_date_stats(date: dict, metric_key) -> dict:
            return {
                "date": date["key"],
                "count": date["doc_count"],
                **{agg: date[agg]["value"] or 0.0 for agg in aggs_per_metric[metric_key]},
            }

        def extract_metric_results(
            metric_or_variant: dict, metric_key: str
        ) -> Sequence[dict]:
            return [
                extract_date_stats(date, metric_key)
                for date in metric_or_variant["dates"]["buckets"]
                if date["key"] <= cutoff_date
            ]

        def extract_variant_results(metric: dict) -> dict:
            metric_key = metric["key"]
            return {
                variant["key"]: extract_metric_results(variant, metric_key)
                for variant in metric["variants"]["buckets"]
            }

        def extract_worker_results(worker: dict) -> dict:
            return {
                metric["key"]: extract_variant_results(metric)
                if split_by_variant
                else extract_metric_results(metric, metric["key"])
                for metric in worker["metrics"]["buckets"]
            }

        return {
            worker["key"]: extract_worker_results(worker)
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
            data = self._search_company_stats(company_id, es_req)

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
