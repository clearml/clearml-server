import base64
import hashlib
import re
import zlib
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from typing import Sequence, Set, Tuple, Optional, List, Mapping, Union

import elasticsearch
from boltons.iterutils import chunked_iter
from elasticsearch.helpers import BulkIndexError
from mongoengine import Q
from nested_dict import nested_dict

from apiserver.bll.event.event_common import (
    EventType,
    get_index_name,
    check_empty_data,
    search_company_events,
    delete_company_events,
    MetricVariants,
    get_metric_variants_condition,
    uncompress_plot,
    get_max_metric_and_variant_counts,
    PlotFields,
)
from apiserver.bll.event.events_iterator import EventsIterator, TaskEventsResult
from apiserver.bll.event.history_debug_image_iterator import HistoryDebugImageIterator
from apiserver.bll.event.history_plots_iterator import HistoryPlotsIterator
from apiserver.bll.event.metric_debug_images_iterator import MetricDebugImagesIterator
from apiserver.bll.event.metric_plots_iterator import MetricPlotsIterator
from apiserver.bll.model import ModelBLL
from apiserver.bll.task.utils import get_many_tasks_for_writing
from apiserver.bll.util import parallel_chunked_decorator
from apiserver.database import utils as dbutils
from apiserver.database.model.model import Model
from apiserver.es_factory import es_factory
from apiserver.apierrors import errors
from apiserver.bll.event.event_metrics import EventMetrics
from apiserver.bll.task import TaskBLL
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.task.task import TaskStatus
from apiserver.redis_manager import redman
from apiserver.service_repo.auth import Identity
from apiserver.utilities.dicts import nested_get
from apiserver.utilities.json import loads

# noinspection PyTypeChecker
EVENT_TYPES: Set[str] = set(et.value for et in EventType if et != EventType.all)
LOCKED_TASK_STATUSES = (TaskStatus.publishing, TaskStatus.published)
MAX_LONG = 2**63 - 1
MIN_LONG = -(2**63)


log = config.logger(__file__)
async_task_events_delete = config.get("services.tasks.async_events_delete", False)
async_delete_threshold = config.get(
    "services.tasks.async_events_delete_threshold", 100_000
)


class EventBLL(object):
    event_id_fields = ("task", "iter", "metric", "variant", "key")
    empty_scroll = "FFFF"
    img_source_regex = re.compile(
        r"['\"]source['\"]:\s?['\"]([a-z][a-z0-9+\-.]*://.*?)['\"]",
        flags=re.IGNORECASE,
    )
    _task_event_query = {
        "bool": {
            "should": [
                {"term": {"model_event": False}},
                {"bool": {"must_not": [{"exists": {"field": "model_event"}}]}},
            ]
        }
    }
    _model_event_query = {"term": {"model_event": True}}

    def __init__(self, events_es=None, redis=None):
        self.es = events_es or es_factory.connect("events")
        self.redis = redis or redman.connection("apiserver")
        self._metrics = EventMetrics(self.es)
        self._skip_iteration_for_metric = set(
            config.get("services.events.ignore_iteration.metrics", [])
        )
        self.debug_images_iterator = MetricDebugImagesIterator(
            es=self.es, redis=self.redis
        )
        self.debug_image_sample_history = HistoryDebugImageIterator(
            es=self.es, redis=self.redis
        )
        self.plots_iterator = MetricPlotsIterator(es=self.es, redis=self.redis)
        self.plot_sample_history = HistoryPlotsIterator(es=self.es, redis=self.redis)
        self.events_iterator = EventsIterator(es=self.es)

    @property
    def metrics(self) -> EventMetrics:
        return self._metrics

    @staticmethod
    def _get_valid_entities(
        company_id, ids: Mapping[str, bool], identity: Identity, model=False
    ) -> Set:
        """Verify that task or model exists and can be updated"""
        if not ids:
            return set()

        with translate_errors_context():
            allow_locked = {id_ for id_, allowed in ids.items() if allowed}
            not_locked = {id_ for id_, allowed in ids.items() if not allowed}
            res = set()
            allow_locked_q = Q()
            not_locked_q = (
                Q(ready__ne=True) if model else Q(status__nin=LOCKED_TASK_STATUSES)
            )
            for requested_ids, locked_q in (
                (allow_locked, allow_locked_q),
                (not_locked, not_locked_q),
            ):
                if not requested_ids:
                    continue

                query = Q(id__in=requested_ids) & locked_q
                if model:
                    ids = Model.objects(query & Q(company=company_id)).scalar("id")
                else:
                    ids = {
                        t.id
                        for t in get_many_tasks_for_writing(
                            company_id=company_id,
                            identity=identity,
                            query=query,
                            only=("id",),
                            throw_on_forbidden=False,
                        )
                    }

                res.update(ids)

            return res

    def add_events(
        self,
        company_id: str,
        identity: Identity,
        events: Sequence[dict],
        worker: str,
    ) -> Tuple[int, int, dict]:
        user_id = identity.user
        task_ids = {}
        model_ids = {}
        for event in events:
            if event.get("model_event", False):
                model = event.pop("model", None)
                if model is not None:
                    event["task"] = model
                entity_ids = model_ids
            else:
                event["model_event"] = False
                entity_ids = task_ids

            id_ = event.get("task")
            allow_locked = event.pop("allow_locked", False)
            if not id_:
                continue

            allowed_for_entity = entity_ids.get(id_)
            if allowed_for_entity is None:
                entity_ids[id_] = allow_locked
            elif allowed_for_entity != allow_locked:
                raise errors.bad_request.ValidationError(
                    f"Inconsistent allow_locked setting in the passed events for {id_}"
                )

        found_in_both = set(task_ids).intersection(set(model_ids))
        if found_in_both:
            raise errors.bad_request.ValidationError(
                "Inconsistent model_event setting in the passed events",
                tasks=found_in_both,
            )
        valid_models = self._get_valid_entities(
            company_id, ids=model_ids, identity=identity, model=True
        )
        valid_tasks = self._get_valid_entities(
            company_id, ids=task_ids, identity=identity
        )

        actions: List[dict] = []
        used_task_ids = set()
        used_model_ids = set()
        task_iteration = defaultdict(lambda: 0)
        task_last_scalar_events = nested_dict(
            3, dict
        )  # task_id -> metric_hash -> variant_hash -> MetricEvent
        task_last_events = nested_dict(
            3, dict
        )  # task_id -> metric_hash -> event_type -> MetricEvent
        errors_per_type = defaultdict(int)
        invalid_iteration_error = f"Iteration number should not exceed {MAX_LONG}"

        for event in events:
            x_axis_label = event.pop("x_axis_label", None)

            # remove spaces from event type
            event_type = event.get("type")
            if event_type is None:
                errors_per_type["Event must have a 'type' field"] += 1
                continue

            event_type = event_type.replace(" ", "_")
            if event_type not in EVENT_TYPES:
                errors_per_type[f"Invalid event type {event_type}"] += 1
                continue

            model_event = event["model_event"]
            if model_event and event_type == EventType.task_log.value:
                errors_per_type[f"Task log events are not supported for models"] += 1
                continue

            task_or_model_id = event.get("task")
            if task_or_model_id is None:
                errors_per_type["Event must have a 'task' field"] += 1
                continue

            if (model_event and task_or_model_id not in valid_models) or (
                not model_event and task_or_model_id not in valid_tasks
            ):
                errors_per_type[
                    f"Invalid {'model' if model_event else 'task'} id {task_or_model_id}"
                ] += 1
                continue

            event["type"] = event_type

            # @timestamp indicates the time the event is written, not when it happened
            event["@timestamp"] = es_factory.get_es_timestamp_str()

            # for backward bomba-tavili-tea
            if "ts" in event:
                event["timestamp"] = event.pop("ts")

            # set timestamp and worker if not sent
            if "timestamp" not in event:
                event["timestamp"] = es_factory.get_timestamp_millis()

            if "worker" not in event:
                event["worker"] = worker

            # force iter to be a long int
            iter = event.get("iter")
            if iter is not None:
                iter = int(iter)
                if iter > MAX_LONG or iter < MIN_LONG:
                    errors_per_type[invalid_iteration_error] += 1
                    continue
                event["iter"] = iter

            # used to have "values" to indicate array. no need anymore
            if "values" in event:
                event["value"] = event["values"]
                del event["values"]

            event["metric"] = event.get("metric") or ""
            event["variant"] = event.get("variant") or ""

            index_name = get_index_name(company_id, event_type)
            es_action = {
                "_op_type": "index",  # overwrite if exists with same ID
                "_index": index_name,
                "_source": event,
            }

            # for "log" events, don't assign custom _id - whatever is sent, is written (not overwritten)
            if event_type != EventType.task_log.value:
                es_action["_id"] = self._get_event_id(event)
            else:
                es_action["_id"] = dbutils.id()

            if (
                iter is not None
                and event.get("metric") not in self._skip_iteration_for_metric
            ):
                task_iteration[task_or_model_id] = max(
                    iter, task_iteration[task_or_model_id]
                )

            if model_event:
                used_model_ids.add(task_or_model_id)
            else:
                used_task_ids.add(task_or_model_id)
                self._update_last_metric_events_for_task(
                    last_events=task_last_events[task_or_model_id],
                    event=event,
                )
            if event_type == EventType.metrics_scalar.value:
                self._update_last_scalar_events_for_task(
                    last_events=task_last_scalar_events[task_or_model_id],
                    event=event,
                    x_axis_label=x_axis_label,
                )

            actions.append(es_action)

        plot_actions = [
            action["_source"]
            for action in actions
            if action["_source"]["type"] == EventType.metrics_plot.value
        ]
        if plot_actions:
            self.validate_and_compress_plots(
                plot_actions,
                validate_json=config.get("services.events.validate_plot_str", False),
                compression_threshold=config.get(
                    "services.events.plot_compression_threshold", 100_000
                ),
            )

        added = 0
        with translate_errors_context():
            if actions:
                chunk_size = 500
                # TODO: replace it with helpers.parallel_bulk in the future once the parallel pool leak is fixed
                # noinspection PyTypeChecker
                with closing(
                    elasticsearch.helpers.streaming_bulk(
                        self.es,
                        actions,
                        chunk_size=chunk_size,
                        # thread_count=8,
                        refresh=True,
                    )
                ) as it:
                    for success, info in it:
                        if success:
                            added += 1
                        else:
                            errors_per_type["Error when indexing events batch"] += 1

            now = datetime.utcnow()
            for model_id in used_model_ids:
                ModelBLL.update_statistics(
                    company_id=company_id,
                    user_id=user_id,
                    model_id=model_id,
                    last_update=now,
                    last_iteration_max=task_iteration.get(model_id),
                    last_scalar_events=task_last_scalar_events.get(model_id),
                )
            remaining_tasks = set()
            for task_id in used_task_ids:
                # Update related tasks. For reasons of performance, we prefer to update
                # all of them and not only those who's events were successful
                updated = self._update_task(
                    company_id=company_id,
                    user_id=user_id,
                    task_id=task_id,
                    now=now,
                    iter_max=task_iteration.get(task_id),
                    last_scalar_events=task_last_scalar_events.get(task_id),
                    last_events=task_last_events.get(task_id),
                )
                if not updated:
                    remaining_tasks.add(task_id)
                    continue

            if remaining_tasks:
                TaskBLL.set_last_update(
                    remaining_tasks,
                    company_id=company_id,
                    user_id=user_id,
                    last_update=now,
                )

            # this is for backwards compatibility with streaming bulk throwing exception on those
            invalid_iterations_count = errors_per_type.get(invalid_iteration_error)
            if invalid_iterations_count:
                raise BulkIndexError(
                    f"{invalid_iterations_count} document(s) failed to index.",
                    [{"_index": invalid_iteration_error}],
                )

        if not added:
            raise errors.bad_request.EventsNotAdded(**errors_per_type)

        errors_count = sum(errors_per_type.values())
        return added, errors_count, errors_per_type

    @parallel_chunked_decorator(chunk_size=10)
    def validate_and_compress_plots(
        self,
        plot_events: Sequence[dict],
        validate_json: bool,
        compression_threshold: int,
    ):
        for event in plot_events:
            validate = validate_json and not event.pop("skip_validation", False)
            plot_str = event.get(PlotFields.plot_str)
            if not plot_str:
                event[PlotFields.plot_len] = 0
                if validate:
                    event[PlotFields.valid_plot] = False
                continue

            plot_len = len(plot_str)
            event[PlotFields.plot_len] = plot_len
            if validate:
                event[PlotFields.valid_plot] = self._is_valid_json(plot_str)

            urls = {match for match in self.img_source_regex.findall(plot_str)}
            if urls:
                event[PlotFields.source_urls] = list(urls)

            if compression_threshold and plot_len >= compression_threshold:
                event[PlotFields.plot_data] = base64.encodebytes(
                    zlib.compress(plot_str.encode(), level=1)
                ).decode("ascii")
                event.pop(PlotFields.plot_str, None)

    @parallel_chunked_decorator(chunk_size=10)
    def uncompress_plots(self, plot_events: Sequence[dict]):
        for event in plot_events:
            uncompress_plot(event)

    @staticmethod
    def _is_valid_json(text: str) -> bool:
        """Check str for valid json"""
        if not text:
            return False
        try:
            loads(text)
        except Exception:
            return False
        return True

    def _update_last_scalar_events_for_task(self, last_events, event, x_axis_label=None):
        """
        Update last_events structure with the provided event details if this event is more
        recent than the currently stored event for its metric/variant combination.

        last_events contains [hashed_metric_name -> hashed_variant_name -> event]. Keys are hashed to avoid mongodb
        key conflicts due to invalid characters and/or long field names.
        """
        value = event.get("value")
        if value is None:
            return

        metric = event.get("metric") or ""
        variant = event.get("variant") or ""
        metric_hash = dbutils.hash_field_name(metric)
        variant_hash = dbutils.hash_field_name(variant)

        last_event = last_events[metric_hash][variant_hash]
        last_event["metric"] = metric
        last_event["variant"] = variant
        last_event["count"] = last_event.get("count", 0) + 1
        last_event["total"] = last_event.get("total", 0) + value

        event_iter = event.get("iter", 0)
        event_timestamp = event.get("timestamp", 0)
        if (event_iter, event_timestamp) >= (
            last_event.get("iter", event_iter),
            last_event.get("timestamp", event_timestamp),
        ):
            last_event["value"] = value
            last_event["iter"] = event_iter
            last_event["timestamp"] = event_timestamp
            if x_axis_label is not None:
                last_event["x_axis_label"] = x_axis_label

        first_value_iter = last_event.get("first_value_iter")
        if first_value_iter is None or event_iter < first_value_iter:
            last_event["first_value"] = value
            last_event["first_value_iter"] = event_iter

        last_event_min_value = last_event.get("min_value")
        if last_event_min_value is None or value < last_event_min_value:
            last_event["min_value"] = value
            last_event["min_value_iter"] = event_iter

        last_event_max_value = last_event.get("max_value")
        if last_event_max_value is None or value > last_event_max_value:
            last_event["max_value"] = value
            last_event["max_value_iter"] = event_iter

    def _update_last_metric_events_for_task(self, last_events, event):
        """
        Update last_events structure with the provided event details if this event is more
        recent than the currently stored event for its metric/event_type combination.
        last_events contains [metric_name -> event_type -> event]
        """
        metric = event.get("metric") or ""
        event_type = event.get("type")
        if not event_type:
            return

        timestamp = last_events[metric][event_type].get("timestamp", None)
        if timestamp is None or timestamp < event["timestamp"]:
            last_events[metric][event_type] = event

    def _update_task(
        self,
        company_id: str,
        user_id: str,
        task_id: str,
        now: datetime,
        iter_max=None,
        last_scalar_events=None,
        last_events=None,
    ):
        """
        Update task information in DB with aggregated results after handling event(s) related to this task.

        This updates the task with the highest iteration value encountered during the last events update, as well
        as the latest metric/variant scalar values reported (according to the report timestamp) and the task's last
        update time.
        """
        if iter_max is None and not last_events and not last_scalar_events:
            return False

        return TaskBLL.update_statistics(
            task_id=task_id,
            company_id=company_id,
            user_id=user_id,
            last_update=now,
            last_iteration_max=iter_max,
            last_scalar_events=last_scalar_events,
            last_events=last_events,
        )

    def _get_event_id(self, event):
        id_values = (
            str(event[field]) for field in self.event_id_fields if field in event
        )
        return hashlib.md5("-".join(id_values).encode()).hexdigest()

    def scroll_task_events(
        self,
        company_id: str,
        task_id: str,
        order: str,
        event_type: EventType,
        batch_size=10000,
        scroll_id=None,
    ):
        if scroll_id == self.empty_scroll:
            return [], scroll_id, 0

        if scroll_id:
            with translate_errors_context():
                es_res = self.es.scroll(scroll_id=scroll_id, scroll="1h")
        else:
            size = min(batch_size, 10000)
            if check_empty_data(self.es, company_id=company_id, event_type=event_type):
                return [], None, 0

            es_req = {
                "size": size,
                "sort": {"timestamp": {"order": order}},
                "query": {"bool": {"must": [{"term": {"task": task_id}}]}},
            }

            with translate_errors_context():
                es_res = search_company_events(
                    self.es,
                    company_id=company_id,
                    event_type=event_type,
                    body=es_req,
                    scroll="1h",
                )

        events, total_events, next_scroll_id = self._get_events_from_es_res(es_res)
        if event_type in (EventType.metrics_plot, EventType.all):
            self.uncompress_plots(events)

        return events, next_scroll_id, total_events

    def get_task_plots(
        self,
        company_id: str,
        task_id: str,
        last_iterations_per_plot: int,
        metric_variants: MetricVariants = None,
    ):
        event_type = EventType.metrics_plot
        if check_empty_data(self.es, company_id, event_type):
            return TaskEventsResult()

        plot_valid_condition = {
            "bool": {
                "should": [
                    {"term": {PlotFields.valid_plot: True}},
                    {
                        "bool": {
                            "must_not": {"exists": {"field": PlotFields.valid_plot}}
                        }
                    },
                ]
            }
        }
        must = [plot_valid_condition, {"term": {"task": task_id}}]
        if metric_variants:
            must.append(get_metric_variants_condition(metric_variants))

        query = {"bool": {"must": must}}
        search_args = dict(es=self.es, company_id=company_id, event_type=event_type)
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query,
            **search_args,
        )
        max_variants = int(max_variants // last_iterations_per_plot)

        es_req = {
            "sort": [{"iter": {"order": "desc"}}],
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
                                "events": {
                                    "top_hits": {
                                        "sort": {"iter": {"order": "desc"}},
                                        "size": last_iterations_per_plot,
                                    }
                                }
                            },
                        }
                    },
                }
            },
        }

        with translate_errors_context():
            es_response = search_company_events(body=es_req, ignore=404, **search_args)

        aggs_result = es_response.get("aggregations")
        if not aggs_result:
            return TaskEventsResult()

        events = [
            hit["_source"]
            for metrics_bucket in aggs_result["metrics"]["buckets"]
            for variants_bucket in metrics_bucket["variants"]["buckets"]
            for hit in variants_bucket["events"]["hits"]["hits"]
        ]
        self.uncompress_plots(events)
        return TaskEventsResult(events=events, total_events=len(events))

    def _get_events_from_es_res(self, es_res: dict) -> Tuple[list, int, Optional[str]]:
        """
        Return events and next scroll id from the scrolled query
        Release the scroll once it is exhausted
        """
        total_events = nested_get(es_res, ("hits", "total", "value"), default=0)
        events = [
            doc["_source"] for doc in nested_get(es_res, ("hits", "hits"), default=[])
        ]
        next_scroll_id = es_res.get("_scroll_id")
        if next_scroll_id and not events:
            self.clear_scroll(next_scroll_id)
            next_scroll_id = self.empty_scroll

        return events, total_events, next_scroll_id

    def get_debug_image_urls(
        self, company_id: str, task_ids: Sequence[str], after_key: dict = None
    ) -> Tuple[Sequence[str], Optional[dict]]:
        if not task_ids or check_empty_data(
            self.es, company_id, EventType.metrics_image
        ):
            return [], None

        es_req = {
            "size": 0,
            "aggs": {
                "debug_images": {
                    "composite": {
                        "size": 1000,
                        **({"after": after_key} if after_key else {}),
                        "sources": [{"url": {"terms": {"field": "url"}}}],
                    }
                }
            },
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"task": task_ids}},
                        {"exists": {"field": "url"}},
                    ]
                }
            },
        }

        es_response = search_company_events(
            self.es,
            company_id=company_id,
            event_type=EventType.metrics_image,
            body=es_req,
        )
        res = nested_get(es_response, ("aggregations", "debug_images"))
        if not res:
            return [], None

        return [bucket["key"]["url"] for bucket in res["buckets"]], res.get("after_key")

    def get_plot_image_urls(
        self, company_id: str, task_ids: Sequence[str], scroll_id: Optional[str]
    ) -> Tuple[Sequence[dict], Optional[str]]:
        if (
            scroll_id == self.empty_scroll
            or not task_ids
            or check_empty_data(self.es, company_id, EventType.metrics_plot)
        ):
            return [], None

        if scroll_id:
            es_res = self.es.scroll(scroll_id=scroll_id, scroll="10m")
        else:
            if check_empty_data(self.es, company_id, EventType.metrics_plot):
                return [], None

            es_req = {
                "size": 1000,
                "_source": [PlotFields.source_urls],
                "query": {
                    "bool": {
                        "must": [
                            {"terms": {"task": task_ids}},
                            {"exists": {"field": PlotFields.source_urls}},
                        ]
                    }
                },
            }
            es_res = search_company_events(
                self.es,
                company_id=company_id,
                event_type=EventType.metrics_plot,
                body=es_req,
                scroll="10m",
            )

        events, _, next_scroll_id = self._get_events_from_es_res(es_res)
        return events, next_scroll_id

    def get_task_events(
        self,
        company_id: Union[str, Sequence[str]],
        task_id: Union[str, Sequence[str]],
        event_type: EventType,
        metrics: MetricVariants = None,
        last_iter_count=None,
        sort=None,
        size=500,
        scroll_id=None,
        no_scroll=False,
        last_iters_per_task_metric=False,
    ) -> TaskEventsResult:
        if scroll_id == self.empty_scroll:
            return TaskEventsResult()

        if scroll_id:
            with translate_errors_context():
                es_res = self.es.scroll(scroll_id=scroll_id, scroll="1h")
        else:
            company_ids = [company_id] if isinstance(company_id, str) else company_id
            company_ids = [
                c_id
                for c_id in set(company_ids)
                if not check_empty_data(self.es, c_id, event_type)
            ]
            if not company_ids:
                return TaskEventsResult()

            task_ids = [task_id] if isinstance(task_id, str) else task_id

            must = []
            if metrics:
                must.append(get_metric_variants_condition(metrics))

            if last_iter_count is None:
                must.append({"terms": {"task": task_ids}})
            else:
                if last_iters_per_task_metric:
                    task_metric_iters = self.get_last_iters_per_metric(
                        company_id=company_ids,
                        event_type=event_type,
                        task_id=task_ids,
                        iters=last_iter_count,
                        metrics=metrics,
                    )
                    should = [
                        {
                            "bool": {
                                "must": [
                                    {"term": {"task": task}},
                                    {"term": {"metric": metric}},
                                    {"terms": {"iter": last_iters}},
                                ]
                            }
                        }
                        for (task, metric), last_iters in task_metric_iters.items()
                        if last_iters
                    ]
                else:
                    tasks_iters = self.get_last_iters(
                        company_id=company_ids,
                        event_type=event_type,
                        task_id=task_ids,
                        iters=last_iter_count,
                        metrics=metrics,
                    )
                    should = [
                        {
                            "bool": {
                                "must": [
                                    {"term": {"task": task}},
                                    {"terms": {"iter": last_iters}},
                                ]
                            }
                        }
                        for task, last_iters in tasks_iters.items()
                        if last_iters
                    ]
                if not should:
                    return TaskEventsResult()
                must.append({"bool": {"should": should}})

            if sort is None:
                sort = [{"timestamp": {"order": "asc"}}]

            es_req = {
                "sort": sort,
                "size": min(size, 10000),
                "query": {"bool": {"must": must}},
            }

            with translate_errors_context():
                es_res = search_company_events(
                    self.es,
                    company_id=company_ids,
                    event_type=event_type,
                    body=es_req,
                    ignore=404,
                    **({} if no_scroll else {"scroll": "1h"}),
                )

        events, total_events, next_scroll_id = self._get_events_from_es_res(es_res)
        if event_type in (EventType.metrics_plot, EventType.all):
            self.uncompress_plots(events)

        return TaskEventsResult(
            events=events, next_scroll_id=next_scroll_id, total_events=total_events
        )

    def get_metrics_and_variants(
        self, company_id: str, task_id: str, event_type: EventType
    ):
        if check_empty_data(self.es, company_id=company_id, event_type=event_type):
            return {}

        query = {"bool": {"must": [{"term": {"task": task_id}}]}}
        search_args = dict(es=self.es, company_id=company_id, event_type=event_type)
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query,
            **search_args,
        )
        es_req = {
            "size": 0,
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
                            }
                        }
                    },
                }
            },
            "query": query,
        }

        with translate_errors_context():
            es_res = search_company_events(body=es_req, **search_args)

        metrics = {}
        for metric_bucket in es_res["aggregations"]["metrics"].get("buckets"):
            metric = metric_bucket["key"]
            metrics[metric] = [
                b["key"] for b in metric_bucket["variants"].get("buckets")
            ]

        return metrics

    def get_task_latest_scalar_values(
        self, company_id, task_id
    ) -> Tuple[Sequence[dict], int]:
        event_type = EventType.metrics_scalar
        if check_empty_data(self.es, company_id=company_id, event_type=event_type):
            return [], 0

        query = {
            "bool": {
                "must": [
                    {"query_string": {"query": "value:>0"}},
                    {"term": {"task": task_id}},
                ]
            }
        }
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
                                "last_value": {
                                    "top_hits": {
                                        "docvalue_fields": ["value"],
                                        "_source": "value",
                                        "size": 1,
                                        "sort": [{"iter": {"order": "desc"}}],
                                    }
                                },
                                "last_timestamp": {"max": {"field": "@timestamp"}},
                                "last_10_value": {
                                    "top_hits": {
                                        "docvalue_fields": ["value"],
                                        "_source": "value",
                                        "size": 10,
                                        "sort": [{"iter": {"order": "desc"}}],
                                    }
                                },
                            },
                        }
                    },
                }
            },
            "_source": {"excludes": []},
        }
        with translate_errors_context():
            es_res = search_company_events(body=es_req, **search_args)

        metrics = []
        max_timestamp = 0
        for metric_bucket in es_res["aggregations"]["metrics"].get("buckets"):
            metric_summary = dict(name=metric_bucket["key"], variants=[])
            for variant_bucket in metric_bucket["variants"].get("buckets"):
                variant_name = variant_bucket["key"]
                last_value = variant_bucket["last_value"]["hits"]["hits"][0]["fields"][
                    "value"
                ][0]
                last_10_value = variant_bucket["last_10_value"]["hits"]["hits"][0][
                    "fields"
                ]["value"][0]
                timestamp = variant_bucket["last_timestamp"]["value"]
                max_timestamp = max(timestamp, max_timestamp)
                metric_summary["variants"].append(
                    dict(
                        name=variant_name,
                        last_value=last_value,
                        last_10_value=last_10_value,
                    )
                )
            metrics.append(metric_summary)
        return metrics, max_timestamp

    def get_vector_metrics_per_iter(self, company_id, task_id, metric, variant):
        event_type = EventType.metrics_vector
        if check_empty_data(self.es, company_id=company_id, event_type=event_type):
            return [], []

        es_req = {
            "size": 10000,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"task": task_id}},
                        {"term": {"metric": metric}},
                        {"term": {"variant": variant}},
                    ]
                }
            },
            "_source": ["iter", "value"],
            "sort": ["iter"],
        }
        with translate_errors_context():
            es_res = search_company_events(
                self.es, company_id=company_id, event_type=event_type, body=es_req
            )

        vectors = []
        iterations = []
        for hit in es_res["hits"]["hits"]:
            vectors.append(hit["_source"]["value"])
            iterations.append(hit["_source"]["iter"])

        return iterations, vectors

    def get_last_iters_per_metric(
        self,
        company_id: Union[str, Sequence[str]],
        event_type: EventType,
        task_id: Union[str, Sequence[str]],
        iters: int,
        metrics: MetricVariants = None,
    ) -> Mapping[Tuple[str, str], Sequence]:
        company_ids = [company_id] if isinstance(company_id, str) else company_id
        company_ids = [
            c_id
            for c_id in set(company_ids)
            if not check_empty_data(self.es, c_id, event_type)
        ]
        if not company_ids:
            return {}

        task_ids = [task_id] if isinstance(task_id, str) else task_id
        must = [{"terms": {"task": task_ids}}]
        if metrics:
            must.append(get_metric_variants_condition(metrics))

        max_tasks = min(len(task_ids), 1000)
        max_metrics = 10_000 // (max_tasks * iters)
        es_req: dict = {
            "size": 0,
            "aggs": {
                "tasks": {
                    "terms": {"field": "task", "size": max_tasks},
                    "aggs": {
                        "metrics": {
                            "terms": {"field": "metric", "size": max_metrics},
                            "aggs": {
                                "iters": {
                                    "terms": {
                                        "field": "iter",
                                        "size": iters,
                                        "order": {"_key": "desc"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "query": {"bool": {"must": must}},
        }

        with translate_errors_context():
            es_res = search_company_events(
                self.es,
                company_id=company_ids,
                event_type=event_type,
                body=es_req,
            )
        if "aggregations" not in es_res:
            return {}

        return {
            (tb["key"], mb["key"]): [ib["key"] for ib in mb["iters"]["buckets"]]
            for tb in es_res["aggregations"]["tasks"]["buckets"]
            for mb in tb["metrics"]["buckets"]
        }

    def get_last_iters(
        self,
        company_id: Union[str, Sequence[str]],
        event_type: EventType,
        task_id: Union[str, Sequence[str]],
        iters: int,
        metrics: MetricVariants = None,
    ) -> Mapping[str, Sequence]:
        company_ids = [company_id] if isinstance(company_id, str) else company_id
        company_ids = [
            c_id
            for c_id in set(company_ids)
            if not check_empty_data(self.es, c_id, event_type)
        ]
        if not company_ids:
            return {}

        task_ids = [task_id] if isinstance(task_id, str) else task_id
        must = [{"terms": {"task": task_ids}}]
        if metrics:
            must.append(get_metric_variants_condition(metrics))

        max_tasks = min(len(task_ids), 1000)
        es_req: dict = {
            "size": 0,
            "aggs": {
                "tasks": {
                    "terms": {"field": "task", "size": max_tasks},
                    "aggs": {
                        "iters": {
                            "terms": {
                                "field": "iter",
                                "size": iters,
                                "order": {"_key": "desc"},
                            }
                        }
                    },
                }
            },
            "query": {"bool": {"must": must}},
        }

        with translate_errors_context():
            es_res = search_company_events(
                self.es,
                company_id=company_ids,
                event_type=event_type,
                body=es_req,
            )

        if "aggregations" not in es_res:
            return {}

        return {
            tb["key"]: [ib["key"] for ib in tb["iters"]["buckets"]]
            for tb in es_res["aggregations"]["tasks"]["buckets"]
        }

    @staticmethod
    def _get_events_deletion_params(async_delete: bool) -> dict:
        if async_delete:
            return {
                "wait_for_completion": False,
                "requests_per_second": config.get(
                    "services.events.max_async_deleted_events_per_sec", 1000
                ),
            }

        return {"refresh": True}

    def delete_task_events(
        self,
        company_id,
        task_ids: Union[str, Sequence[str]],
        wait_for_delete: bool,
        model=False,
    ):
        """
        Delete task events. No check is done for tasks write access
        so it should be checked by the calling code
        """
        if isinstance(task_ids, str):
            task_ids = [task_ids]
        deleted = 0
        with translate_errors_context():
            async_delete = async_task_events_delete and not wait_for_delete
            if async_delete and len(task_ids) < 100:
                total = self.events_iterator.count_task_events(
                    event_type=EventType.all,
                    company_id=company_id,
                    task_ids=task_ids,
                )
                if total <= async_delete_threshold:
                    async_delete = False
            for tasks in chunked_iter(task_ids, 100):
                es_req = {"query": {"terms": {"task": tasks}}}
                es_res = delete_company_events(
                    es=self.es,
                    company_id=company_id,
                    event_type=EventType.all,
                    body=es_req,
                    **self._get_events_deletion_params(async_delete),
                )
                if not async_delete:
                    deleted += es_res.get("deleted", 0)

        if not async_delete:
            return deleted

    def clear_task_log(
        self,
        company_id: str,
        task_id: str,
        threshold_sec: int = None,
        include_metrics: Sequence[str] = None,
        exclude_metrics: Sequence[str] = None,
    ):
        if check_empty_data(
            self.es, company_id=company_id, event_type=EventType.task_log
        ):
            return 0

        with translate_errors_context():
            must = [{"term": {"task": task_id}}]
            sort = None
            if threshold_sec:
                timestamp_ms = int(threshold_sec * 1000)
                must.append(
                    {
                        "range": {
                            "timestamp": {
                                "lt": (es_factory.get_timestamp_millis() - timestamp_ms)
                            }
                        }
                    }
                )
                sort = {"timestamp": {"order": "desc"}}

            if include_metrics:
                must.append({"terms": {"metric": include_metrics}})

            more_conditions = {}
            if exclude_metrics:
                more_conditions = {"must_not": [{"terms": {"metric": exclude_metrics}}]}

            es_req = {
                "query": {"bool": {"must": must, **more_conditions}},
                **({"sort": sort} if sort else {}),
            }
            es_res = delete_company_events(
                es=self.es,
                company_id=company_id,
                event_type=EventType.task_log,
                body=es_req,
                refresh=True,
            )
            return es_res.get("deleted", 0)

    def clear_scroll(self, scroll_id: str):
        if scroll_id == self.empty_scroll:
            return
        # noinspection PyBroadException
        try:
            self.es.clear_scroll(scroll_id=scroll_id)
        except elasticsearch.exceptions.NotFoundError:
            pass
        except elasticsearch.exceptions.RequestError:
            pass
        except Exception as ex:
            log.exception("Failed clearing scroll %s", scroll_id)
