import abc
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from operator import itemgetter
from typing import Sequence, Tuple, Optional, Mapping, Callable

import attr
from boltons.iterutils import first
from elasticsearch import Elasticsearch
from jsonmodels.fields import StringField, ListField, IntField
from jsonmodels.models import Base
from redis import StrictRedis

from apiserver.apimodels import JsonSerializableMixin
from apiserver.bll.event.event_common import (
    EventSettings,
    check_empty_data,
    search_company_events,
    EventType,
    get_metric_variants_condition,
    get_max_metric_and_variant_counts,
)
from apiserver.bll.redis_cache_manager import RedisCacheManager
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.task.metrics import MetricEventStats
from apiserver.database.model.task.task import Task
from apiserver.utilities.dicts import nested_get


class VariantState(Base):
    variant: str = StringField(required=True)
    last_invalid_iteration: int = IntField()


class MetricState(Base):
    metric: str = StringField(required=True)
    variants: Sequence[VariantState] = ListField([VariantState], required=True)
    timestamp: int = IntField(default=0)


class TaskScrollState(Base):
    task: str = StringField(required=True)
    metrics: Sequence[MetricState] = ListField([MetricState], required=True)
    last_min_iter: Optional[int] = IntField()
    last_max_iter: Optional[int] = IntField()

    def reset(self):
        """Reset the scrolling state for the metric"""
        self.last_min_iter = self.last_max_iter = None


class MetricEventsScrollState(Base, JsonSerializableMixin):
    id: str = StringField(required=True)
    tasks: Sequence[TaskScrollState] = ListField([TaskScrollState])
    warning: str = StringField()


@attr.s(auto_attribs=True)
class MetricEventsResult(object):
    metric_events: Sequence[tuple] = []
    next_scroll_id: str = None


class MetricEventsIterator:
    def __init__(self, redis: StrictRedis, es: Elasticsearch, event_type: EventType):
        self.es = es
        self.event_type = event_type
        self.cache_manager = RedisCacheManager(
            state_class=MetricEventsScrollState,
            redis=redis,
            expiration_interval=EventSettings.state_expiration_sec,
        )

    def get_task_events(
        self,
        companies: Mapping[str, str],
        task_metrics: Mapping[str, dict],
        iter_count: int,
        navigate_earlier: bool = True,
        refresh: bool = False,
        state_id: str = None,
    ) -> MetricEventsResult:
        companies = {
            task_id: company_id
            for task_id, company_id in companies.items()
            if not check_empty_data(
                self.es, company_id=company_id, event_type=self.event_type
            )
        }
        if not companies:
            return MetricEventsResult()

        def init_state(state_: MetricEventsScrollState):
            state_.tasks = self._init_task_states(companies, task_metrics)

        def validate_state(state_: MetricEventsScrollState):
            """
            Validate that the metrics stored in the state are the same
            as requested in the current call.
            Refresh the state if requested
            """
            if refresh:
                self._reinit_outdated_task_states(companies, state_, task_metrics)

        with self.cache_manager.get_or_create_state(
            state_id=state_id, init_state=init_state, validate_state=validate_state
        ) as state:
            res = MetricEventsResult(next_scroll_id=state.id)
            specific_variants_requested = any(
                variants
                for t, metrics in task_metrics.items()
                if metrics
                for m, variants in metrics.items()
            )
            with ThreadPoolExecutor(EventSettings.max_workers) as pool:
                res.metric_events = list(
                    pool.map(
                        partial(
                            self._get_task_metric_events,
                            companies=companies,
                            iter_count=iter_count,
                            navigate_earlier=navigate_earlier,
                            specific_variants_requested=specific_variants_requested,
                        ),
                        state.tasks,
                    )
                )

            return res

    def _reinit_outdated_task_states(
        self,
        companies: Mapping[str, str],
        state: MetricEventsScrollState,
        task_metrics: Mapping[str, dict],
    ):
        """
        Determine the metrics for which new event_type events were added
        since their states were initialized and re-init these states
        """
        tasks = Task.objects(id__in=list(task_metrics)).only("id", "metric_stats")

        def get_last_update_times_for_task_metrics(
            task: Task,
        ) -> Mapping[str, datetime]:
            """For metrics that reported event_type events get mapping of the metric name to the last update times"""
            metric_stats: Mapping[str, MetricEventStats] = task.metric_stats
            if not metric_stats:
                return {}

            requested_metrics = task_metrics[task.id]
            return {
                stats.metric: stats.event_stats_by_type[
                    self.event_type.value
                ].last_update
                for stats in metric_stats.values()
                if self.event_type.value in stats.event_stats_by_type
                and (not requested_metrics or stats.metric in requested_metrics)
            }

        update_times = {
            task.id: get_last_update_times_for_task_metrics(task) for task in tasks
        }
        task_metric_states = {
            task_state.task: {
                metric_state.metric: metric_state for metric_state in task_state.metrics
            }
            for task_state in state.tasks
        }
        task_metrics_to_recalc = {}
        for task, metrics_times in update_times.items():
            old_metric_states = task_metric_states[task]
            metrics_to_recalc = {
                m: task_metrics[task].get(m)
                for m, t in metrics_times.items()
                if m not in old_metric_states or old_metric_states[m].timestamp < t
            }
            if metrics_to_recalc:
                task_metrics_to_recalc[task] = metrics_to_recalc

        updated_task_states = self._init_task_states(companies, task_metrics_to_recalc)

        def merge_with_updated_task_states(
            old_state: TaskScrollState, updates: Sequence[TaskScrollState]
        ) -> TaskScrollState:
            task = old_state.task
            updated_state = first(uts for uts in updates if uts.task == task)
            if not updated_state:
                old_state.reset()
                return old_state

            updated_metrics = [m.metric for m in updated_state.metrics]
            return TaskScrollState(
                task=task,
                metrics=[
                    *updated_state.metrics,
                    *(
                        old_metric
                        for old_metric in old_state.metrics
                        if old_metric.metric not in updated_metrics
                    ),
                ],
            )

        state.tasks = [
            merge_with_updated_task_states(task_state, updated_task_states)
            for task_state in state.tasks
        ]

    def _init_task_states(
        self, companies: Mapping[str, str], task_metrics: Mapping[str, dict]
    ) -> Sequence[TaskScrollState]:
        """
        Returned initialized metric scroll stated for the requested task metrics
        """
        with ThreadPoolExecutor(EventSettings.max_workers) as pool:
            task_metric_states = pool.map(
                partial(self._init_metric_states_for_task, companies=companies),
                task_metrics.items(),
            )

        return [
            TaskScrollState(task=task, metrics=metric_states,)
            for task, metric_states in zip(task_metrics, task_metric_states)
        ]

    @abc.abstractmethod
    def _get_extra_conditions(self) -> Sequence[dict]:
        pass

    @abc.abstractmethod
    def _get_variant_state_aggs(
        self,
    ) -> Tuple[dict, Callable[[dict, VariantState], None]]:
        pass

    def _init_metric_states_for_task(
        self, task_metrics: Tuple[str, dict], companies: Mapping[str, str]
    ) -> Sequence[MetricState]:
        """
        Return metric scroll states for the task filled with the variant states
        for the variants that reported any event_type events
        """
        task, metrics = task_metrics
        company_id = companies[task]
        must = [{"term": {"task": task}}, *self._get_extra_conditions()]
        if metrics:
            must.append(get_metric_variants_condition(metrics))
        query = {"bool": {"must": must}}

        search_args = dict(
            es=self.es, company_id=company_id, event_type=self.event_type
        )
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query, **search_args
        )
        max_variants = int(max_variants // 2)
        variant_state_aggs, fill_variant_state_data = self._get_variant_state_aggs()
        es_req: dict = {
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
                        "last_event_timestamp": {"max": {"field": "timestamp"}},
                        "variants": {
                            "terms": {
                                "field": "variant",
                                "size": max_variants,
                                "order": {"_key": "asc"},
                            },
                            **(
                                {"aggs": variant_state_aggs}
                                if variant_state_aggs
                                else {}
                            ),
                        },
                    },
                }
            },
        }

        with translate_errors_context():
            es_res = search_company_events(body=es_req, **search_args)
        if "aggregations" not in es_res:
            return []

        def init_variant_state(variant: dict):
            """
            Return new variant state for the passed variant bucket
            """
            state = VariantState(variant=variant["key"])
            if fill_variant_state_data:
                fill_variant_state_data(variant, state)

            return state

        return [
            MetricState(
                metric=metric["key"],
                timestamp=nested_get(metric, ("last_event_timestamp", "value")),
                variants=[
                    init_variant_state(variant)
                    for variant in nested_get(metric, ("variants", "buckets"))
                ],
            )
            for metric in nested_get(es_res, ("aggregations", "metrics", "buckets"))
        ]

    @abc.abstractmethod
    def _process_event(self, event: dict) -> dict:
        pass

    @abc.abstractmethod
    def _get_same_variant_events_order(self) -> dict:
        pass

    def _get_task_metric_events(
        self,
        task_state: TaskScrollState,
        companies: Mapping[str, str],
        iter_count: int,
        navigate_earlier: bool,
        specific_variants_requested: bool,
    ) -> Tuple:
        """
        Return task metric events grouped by iterations
        Update task scroll state
        """
        if not task_state.metrics:
            return task_state.task, []

        if task_state.last_max_iter is None:
            # the first fetch is always from the latest iteration to the earlier ones
            navigate_earlier = True

        must_conditions = [
            {"term": {"task": task_state.task}},
            {"terms": {"metric": [m.metric for m in task_state.metrics]}},
            *self._get_extra_conditions(),
        ]

        range_condition = None
        if navigate_earlier and task_state.last_min_iter is not None:
            range_condition = {"lt": task_state.last_min_iter}
        elif not navigate_earlier and task_state.last_max_iter is not None:
            range_condition = {"gt": task_state.last_max_iter}
        if range_condition:
            must_conditions.append({"range": {"iter": range_condition}})

        metrics_count = len(task_state.metrics)
        max_variants = int(EventSettings.max_es_buckets / (metrics_count * iter_count))
        es_req = {
            "size": 0,
            "query": {"bool": {"must": must_conditions}},
            "aggs": {
                "iters": {
                    "terms": {
                        "field": "iter",
                        "size": iter_count,
                        "order": {"_key": "desc" if navigate_earlier else "asc"},
                    },
                    "aggs": {
                        "metrics": {
                            "terms": {
                                "field": "metric",
                                "size": metrics_count,
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
                                        "events": {
                                            "top_hits": {
                                                "sort": self._get_same_variant_events_order(),
                                                "size": 1,
                                            }
                                        }
                                    },
                                }
                            },
                        }
                    },
                }
            },
        }
        with translate_errors_context():
            es_res = search_company_events(
                self.es,
                company_id=companies[task_state.task],
                event_type=self.event_type,
                body=es_req,
            )
        if "aggregations" not in es_res:
            return task_state.task, []

        invalid_iterations = {
            (m.metric, v.variant): v.last_invalid_iteration
            for m in task_state.metrics
            for v in m.variants
        }
        allow_uninitialized = (
            False
            if specific_variants_requested
            else config.get(
                "services.events.events_retrieval.debug_images.allow_uninitialized_variants",
                False,
            )
        )

        def is_valid_event(event: dict) -> bool:
            key = event.get("metric"), event.get("variant")
            if key not in invalid_iterations:
                return allow_uninitialized

            max_invalid = invalid_iterations[key]
            return max_invalid is None or event.get("iter") > max_invalid

        def get_iteration_events(it_: dict) -> Sequence:
            return [
                self._process_event(ev["_source"])
                for m in nested_get(it_, ("metrics", "buckets"))
                for v in nested_get(m, ("variants", "buckets"))
                for ev in nested_get(v, ("events", "hits", "hits"))
                if is_valid_event(ev["_source"])
            ]

        iterations = []
        for it in nested_get(es_res, ("aggregations", "iters", "buckets")):
            events = get_iteration_events(it)
            if events:
                iterations.append({"iter": it["key"], "events": events})

        if not navigate_earlier:
            iterations.sort(key=itemgetter("iter"), reverse=True)
        if iterations:
            task_state.last_max_iter = iterations[0]["iter"]
            task_state.last_min_iter = iterations[-1]["iter"]

        return task_state.task, iterations
