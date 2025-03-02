import itertools
import math
from collections import defaultdict
from concurrent.futures.thread import ThreadPoolExecutor
from functools import partial
from operator import itemgetter
from typing import Sequence, Tuple, Mapping

from boltons.iterutils import bucketize
from elasticsearch import Elasticsearch

from apiserver.bll.event.event_common import (
    EventType,
    EventSettings,
    search_company_events,
    check_empty_data,
    MetricVariants,
    get_metric_variants_condition,
    get_max_metric_and_variant_counts,
    SINGLE_SCALAR_ITERATION,
    TaskCompanies,
)
from apiserver.bll.event.scalar_key import ScalarKey, ScalarKeyEnum
from apiserver.bll.query import Builder as QueryBuilder
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task
from apiserver.utilities.dicts import nested_get

log = config.logger(__file__)


class EventMetrics:
    MAX_AGGS_ELEMENTS_COUNT = 50
    MAX_SAMPLE_BUCKETS = 6000

    def __init__(self, es: Elasticsearch):
        self.es = es

    def get_scalar_metrics_average_per_iter(
        self,
        company_id: str,
        task_id: str,
        samples: int,
        key: ScalarKeyEnum,
        metric_variants: MetricVariants = None,
        model_events: bool = False,
    ) -> dict:
        """
        Get scalar metric histogram per metric and variant
        The amount of points in each histogram should not exceed
        the requested samples
        """
        event_type = EventType.metrics_scalar
        if check_empty_data(self.es, company_id=company_id, event_type=event_type):
            return {}

        return self._get_scalar_average_per_iter_core(
            task_id=task_id,
            company_id=company_id,
            event_type=event_type,
            samples=samples,
            key=ScalarKey.resolve(key),
            metric_variants=metric_variants,
            model_events=model_events,
        )

    def _get_scalar_average_per_iter_core(
        self,
        task_id: str,
        company_id: str,
        event_type: EventType,
        samples: int,
        key: ScalarKey,
        run_parallel: bool = True,
        metric_variants: MetricVariants = None,
        model_events: bool = False,
    ) -> dict:
        intervals = self._get_task_metric_intervals(
            company_id=company_id,
            event_type=event_type,
            task_id=task_id,
            samples=samples,
            field=key.field,
            metric_variants=metric_variants,
        )
        if not intervals:
            return {}
        interval_groups = self._group_task_metric_intervals(intervals)

        get_scalar_average = partial(
            self._get_scalar_average,
            task_id=task_id,
            company_id=company_id,
            event_type=event_type,
            key=key,
        )
        if run_parallel:
            with ThreadPoolExecutor(max_workers=EventSettings.max_workers) as pool:
                metrics = itertools.chain.from_iterable(
                    pool.map(get_scalar_average, interval_groups)
                )
        else:
            metrics = itertools.chain.from_iterable(
                get_scalar_average(group) for group in interval_groups
            )

        ret = defaultdict(dict)
        if not metrics:
            return ret

        last_metrics = {}
        cls_ = Model if model_events else Task
        task = cls_.objects(id=task_id).only("last_metrics").first()
        if task and task.last_metrics:
            for m_data in task.last_metrics.values():
                for v_data in m_data.values():
                    last_metrics[(v_data.metric, v_data.variant)] = v_data

        for metric_key, metric_values in metrics:
            for variant_key, data in metric_values.items():
                last_metrics_data = last_metrics.get((metric_key, variant_key))
                if last_metrics_data and last_metrics_data.x_axis_label is not None:
                    data["x_axis_label"] = last_metrics_data.x_axis_label
            ret[metric_key].update(metric_values)

        return ret

    def compare_scalar_metrics_average_per_iter(
        self,
        companies: TaskCompanies,
        samples,
        key: ScalarKeyEnum,
        metric_variants: MetricVariants = None,
        model_events: bool = False,
    ):
        """
        Compare scalar metrics for different tasks per metric and variant
        The amount of points in each histogram should not exceed the requested samples
        """
        event_type = EventType.metrics_scalar
        companies = {
            company_id: tasks
            for company_id, tasks in companies.items()
            if not check_empty_data(
                self.es, company_id=company_id, event_type=event_type
            )
        }
        if not companies:
            return {}

        get_scalar_average_per_iter = partial(
            self._get_scalar_average_per_iter_core,
            event_type=event_type,
            samples=samples,
            key=ScalarKey.resolve(key),
            metric_variants=metric_variants,
            run_parallel=False,
            model_events=model_events,
        )
        task_ids, company_ids = zip(
            *(
                (t.id, t.company)
                for t in itertools.chain.from_iterable(companies.values())
            )
        )
        with ThreadPoolExecutor(max_workers=EventSettings.max_workers) as pool:
            task_metrics = zip(
                task_ids, pool.map(get_scalar_average_per_iter, task_ids, company_ids)
            )

        task_names = {
            t.id: t.name for t in itertools.chain.from_iterable(companies.values())
        }
        res = defaultdict(lambda: defaultdict(dict))
        for task_id, task_data in task_metrics:
            task_name = task_names[task_id]
            for metric_key, metric_data in task_data.items():
                for variant_key, variant_data in metric_data.items():
                    variant_data["name"] = task_name
                    res[metric_key][variant_key][task_id] = variant_data

        return res

    def get_task_single_value_metrics(
        self,
        companies: TaskCompanies,
        metric_variants: MetricVariants = None,
    ) -> Mapping[str, Sequence[dict]]:
        """
        For the requested tasks return all the events delivered for the single iteration (-2**31)
        """
        companies = {
            company_id: [t.id for t in tasks]
            for company_id, tasks in companies.items()
            if not check_empty_data(
                self.es, company_id=company_id, event_type=EventType.metrics_scalar
            )
        }
        if not companies:
            return {}

        with ThreadPoolExecutor(max_workers=EventSettings.max_workers) as pool:
            task_events = list(
                itertools.chain.from_iterable(
                    pool.map(
                        partial(
                            self._get_task_single_value_metrics,
                            metric_variants=metric_variants,
                        ),
                        companies.items(),
                    )
                ),
            )

        def _get_value(event: dict):
            return {
                field: event.get(field)
                for field in ("metric", "variant", "value", "timestamp")
            }

        return {
            task: [_get_value(e) for e in events]
            for task, events in bucketize(task_events, itemgetter("task")).items()
        }

    def _get_task_single_value_metrics(
        self, tasks: Tuple[str, Sequence[str]], metric_variants: MetricVariants = None
    ) -> Sequence[dict]:
        company_id, task_ids = tasks
        must = [
            {"terms": {"task": task_ids}},
            {"term": {"iter": SINGLE_SCALAR_ITERATION}},
        ]
        if metric_variants:
            must.append(get_metric_variants_condition(metric_variants))

        es_req = {
            "size": 10000,
            "query": {"bool": {"must": must}},
        }
        with translate_errors_context():
            es_res = search_company_events(
                body=es_req,
                es=self.es,
                company_id=company_id,
                event_type=EventType.metrics_scalar,
            )
            if not es_res["hits"]["total"]["value"]:
                return []

        return [hit["_source"] for hit in es_res["hits"]["hits"]]

    MetricInterval = Tuple[str, str, int, int]
    MetricIntervalGroup = Tuple[int, Sequence[Tuple[str, str]]]

    @classmethod
    def _group_task_metric_intervals(
        cls, intervals: Sequence[MetricInterval]
    ) -> Sequence[MetricIntervalGroup]:
        """
        Group task metric intervals so that the following conditions are meat:
            - All the metrics in the same group have the same interval (with 10% rounding)
            - The amount of metrics in the group does not exceed MAX_AGGS_ELEMENTS_COUNT
            - The total count of samples in the group does not exceed MAX_SAMPLE_BUCKETS
        """
        metric_interval_groups = []
        interval_group = []
        group_interval_upper_bound = 0
        group_max_interval = 0
        group_samples = 0
        for metric, variant, interval, size in sorted(intervals, key=itemgetter(2)):
            if (
                interval > group_interval_upper_bound
                or (group_samples + size) > cls.MAX_SAMPLE_BUCKETS
                or len(interval_group) >= cls.MAX_AGGS_ELEMENTS_COUNT
            ):
                if interval_group:
                    metric_interval_groups.append((group_max_interval, interval_group))
                    interval_group = []
                group_max_interval = interval
                group_interval_upper_bound = interval + int(interval * 0.1)
                group_samples = 0
            interval_group.append((metric, variant))
            group_samples += size
            group_max_interval = max(group_max_interval, interval)
        if interval_group:
            metric_interval_groups.append((group_max_interval, interval_group))

        return metric_interval_groups

    def _get_task_metric_intervals(
        self,
        company_id: str,
        event_type: EventType,
        task_id: str,
        samples: int,
        field: str = "iter",
        metric_variants: MetricVariants = None,
    ) -> Sequence[MetricInterval]:
        """
        Calculate interval per task metric variant so that the resulting
        amount of points does not exceed sample.
        Return the list og metric variant intervals as the following tuple:
        (metric, variant, interval, samples)
        """
        must = self._task_conditions(task_id)
        if metric_variants:
            must.append(get_metric_variants_condition(metric_variants))
        query = {"bool": {"must": must}}
        search_args = dict(es=self.es, company_id=company_id, event_type=event_type)
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query,
            **search_args,
        )
        max_variants = int(max_variants // 2)
        es_req = {
            "size": 0,
            "query": query,
            "aggs": {
                "metrics": {
                    "terms": {
                        "field": "metric",
                        "size": max_metrics,
                        "order": {"_key": "asc"},
                    },
                    "aggs": {
                        "variants": {
                            "terms": {
                                "field": "variant",
                                "size": max_variants,
                                "order": {"_key": "asc"},
                            },
                            "aggs": {
                                "count": {"value_count": {"field": field}},
                                "min_index": {"min": {"field": field}},
                                "max_index": {"max": {"field": field}},
                            },
                        }
                    },
                }
            },
        }

        es_res = search_company_events(body=es_req, **search_args)

        aggs_result = es_res.get("aggregations")
        if not aggs_result:
            return []

        return [
            self._build_metric_interval(metric["key"], variant["key"], variant, samples)
            for metric in aggs_result["metrics"]["buckets"]
            for variant in metric["variants"]["buckets"]
        ]

    @staticmethod
    def _build_metric_interval(
        metric: str, variant: str, data: dict, samples: int
    ) -> Tuple[str, str, int, int]:
        """
        Calculate index interval per metric_variant variant so that the
        total amount of intervals does not exceeds the samples
        Return the interval and resulting amount of intervals
        """
        count = nested_get(data, ("count", "value"), default=0)
        if count < samples:
            return metric, variant, 1, count

        min_index = nested_get(data, ("min_index", "value"), default=0)
        max_index = nested_get(data, ("max_index", "value"), default=min_index)
        index_range = max_index - min_index + 1
        interval = max(1, math.ceil(float(index_range) / samples))
        max_samples = math.ceil(float(index_range) / interval)
        return (
            metric,
            variant,
            interval,
            max_samples,
        )

    MetricData = Tuple[str, dict]

    def _get_scalar_average(
        self,
        metrics_interval: MetricIntervalGroup,
        task_id: str,
        company_id: str,
        event_type: EventType,
        key: ScalarKey,
    ) -> Sequence[MetricData]:
        """
        Retrieve scalar histograms per several metric variants that share the same interval
        """
        interval, metrics = metrics_interval
        aggregation = self._add_aggregation_average(key.get_aggregation(interval))
        query = self._get_task_metrics_query(task_id=task_id, metrics=metrics)
        search_args = dict(es=self.es, company_id=company_id, event_type=event_type)
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query,
            **search_args,
        )
        max_variants = int(max_variants // 2)
        es_req = {
            "size": 0,
            "query": query,
            "aggs": {
                "metrics": {
                    "terms": {
                        "field": "metric",
                        "size": max_metrics,
                        "order": {"_key": "asc"},
                    },
                    "aggs": {
                        "variants": {
                            "terms": {
                                "field": "variant",
                                "size": max_variants,
                                "order": {"_key": "asc"},
                            },
                            "aggs": aggregation,
                        }
                    },
                }
            },
        }

        with translate_errors_context():
            es_res = search_company_events(body=es_req, **search_args)

        aggs_result = es_res.get("aggregations")
        if not aggs_result:
            return {}

        metrics = [
            (
                metric["key"],
                {
                    variant["key"]: {
                        "name": variant["key"],
                        **key.get_iterations_data(variant),
                    }
                    for variant in metric["variants"]["buckets"]
                },
            )
            for metric in aggs_result["metrics"]["buckets"]
        ]
        return metrics

    @staticmethod
    def _add_aggregation_average(aggregation):
        average_agg = {"avg_val": {"avg": {"field": "value"}}}
        return {
            key: {**value, "aggs": {**value.get("aggs", {}), **average_agg}}
            for key, value in aggregation.items()
        }

    @staticmethod
    def _task_conditions(task_id: str) -> list:
        return [
            {"term": {"task": task_id}},
            {"range": {"iter": {"gt": SINGLE_SCALAR_ITERATION}}},
        ]

    @classmethod
    def _get_task_metrics_query(
        cls,
        task_id: str,
        metrics: Sequence[Tuple[str, str]],
    ):
        must = cls._task_conditions(task_id)
        if metrics:
            should = [
                {
                    "bool": {
                        "must": [
                            {"term": {"metric": metric}},
                            {"term": {"variant": variant}},
                        ]
                    }
                }
                for metric, variant in metrics
            ]
            must.append({"bool": {"should": should}})

        return {"bool": {"must": must}}

    def get_multi_task_metrics(self, companies: TaskCompanies, event_type: EventType) -> Mapping[str, list]:
        """
        For the requested tasks return reported metrics and variants
        """
        tasks_ids = {
            company: [t.id for t in tasks]
            for company, tasks in companies.items()
        }
        with ThreadPoolExecutor(EventSettings.max_workers) as pool:
            companies_res: Sequence = list(
                pool.map(
                    partial(
                        self._get_multi_task_metrics,
                        event_type=event_type,
                    ),
                    tasks_ids.items(),
                )
            )

        if len(companies_res) == 1:
            return companies_res[0]

        res = defaultdict(set)
        for c_res in companies_res:
            for m, vars_ in c_res.items():
                res[m].update(vars_)

        return {
            k: list(v)
            for k, v in res.items()
        }

    def _get_multi_task_metrics(
        self, company_tasks: Tuple[str, Sequence[str]], event_type: EventType
    ) -> Mapping[str, list]:
        company_id, task_ids = company_tasks
        if check_empty_data(self.es, company_id, event_type):
            return {}

        search_args = dict(
            es=self.es,
            company_id=company_id,
            event_type=event_type,
        )
        query = QueryBuilder.terms("task", task_ids)
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query,
            **search_args,
        )
        es_req = {
            "size": 0,
            "query": query,
            "aggs": {
                "metrics": {
                    "terms": {
                        "field": "metric",
                        "size": max_metrics,
                        "order": {"_key": "asc"},
                    },
                    "aggs": {
                        "variants": {
                            "terms": {
                                "field": "variant",
                                "size": max_variants,
                                "order": {"_key": "asc"},
                            },
                        }
                    }
                }
            },
        }

        es_res = search_company_events(
            body=es_req,
            **search_args,
        )
        aggs_result = es_res.get("aggregations")
        if not aggs_result:
            return {}

        return {
            mb["key"]: [vb["key"] for vb in mb["variants"]["buckets"]]
            for mb in aggs_result["metrics"]["buckets"]
        }

    def get_task_metrics(
        self, company_id, task_ids: Sequence, event_type: EventType
    ) -> Sequence:
        """
        For the requested tasks return reported metrics per task
        """
        if check_empty_data(self.es, company_id, event_type):
            return {}

        with ThreadPoolExecutor(EventSettings.max_workers) as pool:
            res = pool.map(
                partial(
                    self._get_task_metrics,
                    company_id=company_id,
                    event_type=event_type,
                ),
                task_ids,
            )
        return list(zip(task_ids, res))

    def _get_task_metrics(
        self, task_id: str, company_id: str, event_type: EventType
    ) -> Sequence:
        es_req = {
            "size": 0,
            "query": {"bool": {"must": self._task_conditions(task_id)}},
            "aggs": {
                "metrics": {
                    "terms": {
                        "field": "metric",
                        "size": EventSettings.max_es_buckets,
                        "order": {"_key": "asc"},
                    }
                }
            },
        }

        es_res = search_company_events(
            self.es, company_id=company_id, event_type=event_type, body=es_req
        )

        return [
            metric["key"]
            for metric in nested_get(es_res, ("aggregations", "metrics", "buckets"), default=[])
        ]
