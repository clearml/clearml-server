import operator
from operator import attrgetter
from typing import Sequence, Tuple, Optional, Mapping

import attr
from boltons.iterutils import first, bucketize
from elasticsearch import Elasticsearch
from jsonmodels.fields import StringField, IntField, BoolField, ListField
from jsonmodels.models import Base
from redis.client import StrictRedis

from apiserver.utilities.dicts import nested_get
from .event_common import (
    EventType,
    EventSettings,
    check_empty_data,
    search_company_events,
    get_max_metric_and_variant_counts,
)
from apiserver.apimodels import JsonSerializableMixin
from apiserver.bll.redis_cache_manager import RedisCacheManager
from apiserver.apierrors import errors


class VariantState(Base):
    name: str = StringField(required=True)
    metric: str = StringField(default=None)
    min_iteration: int = IntField()
    max_iteration: int = IntField()


class DebugImageSampleState(Base, JsonSerializableMixin):
    id: str = StringField(required=True)
    iteration: int = IntField()
    variant: str = StringField()
    task: str = StringField()
    metric: str = StringField()
    variant_states: Sequence[VariantState] = ListField([VariantState])
    warning: str = StringField()
    navigate_current_metric = BoolField(default=True)


@attr.s(auto_attribs=True)
class VariantSampleResult(object):
    scroll_id: str = None
    event: dict = None
    min_iteration: int = None
    max_iteration: int = None


class HistoryDebugImageIterator:
    event_type = EventType.metrics_image

    def __init__(self, redis: StrictRedis, es: Elasticsearch):
        self.es = es
        self.cache_manager = RedisCacheManager(
            state_class=DebugImageSampleState,
            redis=redis,
            expiration_interval=EventSettings.state_expiration_sec,
        )

    def get_next_sample(
        self,
        company_id: str,
        task: str,
        state_id: str,
        navigate_earlier: bool,
        next_iteration: bool,
    ) -> VariantSampleResult:
        """
        Get the sample for next/prev variant on the current iteration
        If does not exist then try getting sample for the first/last variant from next/prev iteration
        """
        res = VariantSampleResult(scroll_id=state_id)
        state = self.cache_manager.get_state(state_id)
        if not state or state.task != task:
            raise errors.bad_request.InvalidScrollId(scroll_id=state_id)

        if check_empty_data(self.es, company_id=company_id, event_type=self.event_type):
            return res

        if next_iteration:
            event = self._get_next_for_another_iteration(
                company_id=company_id, navigate_earlier=navigate_earlier, state=state
            )
        else:
            # noinspection PyArgumentList
            event = first(
                f(company_id=company_id, navigate_earlier=navigate_earlier, state=state)
                for f in (
                    self._get_next_for_current_iteration,
                    self._get_next_for_another_iteration,
                )
            )
        if not event:
            return res

        self._fill_res_and_update_state(event=event, res=res, state=state)
        self.cache_manager.set_state(state=state)
        return res

    @staticmethod
    def _fill_res_and_update_state(
        event: dict, res: VariantSampleResult, state: DebugImageSampleState
    ):
        state.variant = event["variant"]
        state.metric = event["metric"]
        state.iteration = event["iter"]
        res.event = event
        var_state = first(
            vs
            for vs in state.variant_states
            if vs.name == state.variant and vs.metric == state.metric
        )
        if var_state:
            res.min_iteration = var_state.min_iteration
            res.max_iteration = var_state.max_iteration

    @staticmethod
    def _get_metric_conditions(variants: Sequence[VariantState]) -> dict:
        metrics = bucketize(variants, key=attrgetter("metric"))

        def _get_variants_conditions(metric_variants: Sequence[VariantState]) -> dict:
            variants_conditions = [
                {
                    "bool": {
                        "must": [
                            {"term": {"variant": v.name}},
                            {"range": {"iter": {"gte": v.min_iteration}}},
                        ]
                    }
                }
                for v in metric_variants
            ]
            return {"bool": {"should": variants_conditions}}

        metrics_conditions = [
            {
                "bool": {
                    "must": [
                        {"term": {"metric": metric}},
                        _get_variants_conditions(metric_variants),
                    ]
                }
            }
            for metric, metric_variants in metrics.items()
        ]
        return {"bool": {"should": metrics_conditions}}

    def _get_next_for_current_iteration(
        self, company_id: str, navigate_earlier: bool, state: DebugImageSampleState
    ) -> Optional[dict]:
        """
        Get the sample for next (if navigate_earlier is False) or previous variant sorted by name for the same iteration
        Only variants for which the iteration falls into their valid range are considered
        Return None if no such variant or sample is found
        """
        if state.navigate_current_metric:
            variants = [
                var_state
                for var_state in state.variant_states
                if var_state.metric == state.metric
            ]
        else:
            variants = state.variant_states

        cmp = operator.lt if navigate_earlier else operator.gt
        variants = [
            var_state
            for var_state in variants
            if cmp((var_state.metric, var_state.name), (state.metric, state.variant))
            and var_state.min_iteration <= state.iteration
        ]
        if not variants:
            return

        must_conditions = [
            {"term": {"task": state.task}},
            {"term": {"iter": state.iteration}},
            self._get_metric_conditions(variants),
            {"exists": {"field": "url"}},
        ]
        order = "desc" if navigate_earlier else "asc"
        es_req = {
            "size": 1,
            "sort": [{"metric": order}, {"variant": order}, {"url": "desc"}],
            "query": {"bool": {"must": must_conditions}},
        }

        es_res = search_company_events(
            self.es,
            company_id=company_id,
            event_type=self.event_type,
            body=es_req,
        )

        hits = nested_get(es_res, ("hits", "hits"))
        if not hits:
            return

        return hits[0]["_source"]

    def _get_next_for_another_iteration(
        self, company_id: str, navigate_earlier: bool, state: DebugImageSampleState
    ) -> Optional[dict]:
        """
        Get the sample for the first variant for the next iteration (if navigate_earlier is set to False)
        or from the last variant for the previous iteration (otherwise)
        The variants for which the sample falls in invalid range are discarded
        If no suitable sample is found then None is returned
        """
        if state.navigate_current_metric:
            variants = [
                var_state
                for var_state in state.variant_states
                if var_state.metric == state.metric
            ]
        else:
            variants = state.variant_states

        if navigate_earlier:
            range_operator = "lt"
            order = "desc"
            variants = [
                var_state
                for var_state in variants
                if var_state.min_iteration < state.iteration
            ]
        else:
            range_operator = "gt"
            order = "asc"
            variants = variants

        if not variants:
            return

        must_conditions = [
            {"term": {"task": state.task}},
            self._get_metric_conditions(variants),
            {"range": {"iter": {range_operator: state.iteration}}},
            {"exists": {"field": "url"}},
        ]
        es_req = {
            "size": 1,
            "sort": [{"iter": order}, {"metric": order}, {"variant": order}, {"url": "desc"}],
            "query": {"bool": {"must": must_conditions}},
        }
        es_res = search_company_events(
            self.es,
            company_id=company_id,
            event_type=self.event_type,
            body=es_req,
        )

        hits = nested_get(es_res, ("hits", "hits"))
        if not hits:
            return

        return hits[0]["_source"]

    def get_sample_for_variant(
        self,
        company_id: str,
        task: str,
        metric: str,
        variant: str,
        iteration: Optional[int] = None,
        refresh: bool = False,
        state_id: str = None,
        navigate_current_metric: bool = True,
    ) -> VariantSampleResult:
        """
        Get the sample for the requested iteration or the latest before it
        If the iteration is not passed then get the latest event
        """
        res = VariantSampleResult()
        if check_empty_data(self.es, company_id=company_id, event_type=self.event_type):
            return res

        def init_state(state_: DebugImageSampleState):
            state_.task = task
            state_.metric = metric
            state_.navigate_current_metric = navigate_current_metric
            self._reset_variant_states(company_id=company_id, state=state_)

        def validate_state(state_: DebugImageSampleState):
            if (
                state_.task != task
                or state_.navigate_current_metric != navigate_current_metric
                or (state_.navigate_current_metric and state_.metric != metric)
            ):
                raise errors.bad_request.InvalidScrollId(
                    "Task and metric stored in the state do not match the passed ones",
                    scroll_id=state_.id,
                )
            # fix old variant states:
            for vs in state_.variant_states:
                if vs.metric is None:
                    vs.metric = metric
            if refresh:
                self._reset_variant_states(company_id=company_id, state=state_)

        state: DebugImageSampleState
        with self.cache_manager.get_or_create_state(
            state_id=state_id, init_state=init_state, validate_state=validate_state,
        ) as state:
            res.scroll_id = state.id

            var_state = first(
                vs
                for vs in state.variant_states
                if vs.name == variant and vs.metric == metric
            )
            if not var_state:
                return res

            res.min_iteration = var_state.min_iteration
            res.max_iteration = var_state.max_iteration

            must_conditions = [
                {"term": {"task": task}},
                {"term": {"metric": metric}},
                {"term": {"variant": variant}},
                {"exists": {"field": "url"}},
            ]
            if iteration is not None:
                must_conditions.append(
                    {
                        "range": {
                            "iter": {"lte": iteration, "gte": var_state.min_iteration}
                        }
                    }
                )
            else:
                must_conditions.append(
                    {"range": {"iter": {"gte": var_state.min_iteration}}}
                )

            es_req = {
                "size": 1,
                "sort": [{"iter": "desc"}, {"url": "desc"}],
                "query": {"bool": {"must": must_conditions}},
            }

            es_res = search_company_events(
                self.es,
                company_id=company_id,
                event_type=self.event_type,
                body=es_req,
            )

            hits = nested_get(es_res, ("hits", "hits"))
            if not hits:
                return res

            self._fill_res_and_update_state(
                event=hits[0]["_source"], res=res, state=state
            )
            return res

    def _reset_variant_states(self, company_id: str, state: DebugImageSampleState):
        metrics = self._get_metric_variant_iterations(
            company_id=company_id,
            task=state.task,
            metric=state.metric if state.navigate_current_metric else None,
        )
        state.variant_states = [
            VariantState(
                metric=metric,
                name=var_name,
                min_iteration=min_iter,
                max_iteration=max_iter,
            )
            for metric, variants in metrics.items()
            for var_name, min_iter, max_iter in variants
        ]

    def _get_metric_variant_iterations(
        self, company_id: str, task: str, metric: str,
    ) -> Mapping[str, Sequence[Tuple[str, int, int]]]:
        """
        Return valid min and max iterations that the task reported events of the required type
        """
        must = [
            {"term": {"task": task}},
            {"exists": {"field": "url"}},
        ]
        if metric is not None:
            must.append({"term": {"metric": metric}})
        query = {"bool": {"must": must}}

        search_args = dict(
            es=self.es, company_id=company_id, event_type=self.event_type,
        )
        max_metrics, max_variants = get_max_metric_and_variant_counts(
            query=query, **search_args
        )
        max_variants = int(max_variants // 2)
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
                        "variants": {
                            "terms": {
                                "field": "variant",
                                "size": max_variants,
                                "order": {"_key": "asc"},
                            },
                            "aggs": {
                                "last_iter": {"max": {"field": "iter"}},
                                "urls": {
                                    # group by urls and choose the minimal iteration
                                    # from all the maximal iterations per url
                                    "terms": {
                                        "field": "url",
                                        "order": {"max_iter": "asc"},
                                        "size": 1,
                                    },
                                    "aggs": {
                                        # find max iteration for each url
                                        "max_iter": {"max": {"field": "iter"}}
                                    },
                                },
                            },
                        }
                    },
                }
            },
        }

        es_res = search_company_events(body=es_req, **search_args)

        def get_variant_data(variant_bucket: dict) -> Tuple[str, int, int]:
            variant = variant_bucket["key"]
            urls = nested_get(variant_bucket, ("urls", "buckets"))
            min_iter = int(urls[0]["max_iter"]["value"])
            max_iter = int(variant_bucket["last_iter"]["value"])
            return variant, min_iter, max_iter

        return {
            metric_bucket["key"]: [
                get_variant_data(variant_bucket)
                for variant_bucket in nested_get(metric_bucket, ("variants", "buckets"))
            ]
            for metric_bucket in nested_get(
                es_res, ("aggregations", "metrics", "buckets")
            )
        }
