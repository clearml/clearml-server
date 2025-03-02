import itertools
import math
from collections import defaultdict
from operator import itemgetter
from typing import Sequence, Optional, Union, Tuple, Mapping

import attr
import jsonmodels.fields
from boltons.iterutils import bucketize

from apiserver.apierrors import errors
from apiserver.apimodels.events import (
    MultiTaskScalarMetricsIterHistogramRequest,
    ScalarMetricsIterHistogramRequest,
    MetricEventsRequest,
    MetricEventsResponse,
    MetricEvents,
    IterationEvents,
    TaskMetricsRequest,
    LogEventsRequest,
    LogOrderEnum,
    NextHistorySampleRequest,
    MetricVariants as ApiMetrics,
    TaskPlotsRequest,
    TaskEventsRequest,
    ScalarMetricsIterRawRequest,
    ClearScrollRequest,
    ClearTaskLogRequest,
    SingleValueMetricsRequest,
    GetVariantSampleRequest,
    GetMetricSamplesRequest,
    TaskMetric,
    MultiTaskPlotsRequest,
    MultiTaskMetricsRequest,
    LegacyLogEventsRequest,
    TaskRequest,
    GetMetricsAndVariantsRequest,
    ModelRequest,
    LegacyMetricEventsRequest,
    GetScalarMetricDataRequest,
    VectorMetricsIterHistogramRequest,
    LegacyMultiTaskEventsRequest,
)
from apiserver.bll.event import EventBLL
from apiserver.bll.event.event_bll import LOCKED_TASK_STATUSES
from apiserver.bll.event.event_common import EventType, MetricVariants, TaskCompanies
from apiserver.bll.event.events_iterator import Scroll
from apiserver.bll.event.scalar_key import ScalarKeyEnum, ScalarKey
from apiserver.bll.model import ModelBLL
from apiserver.bll.task import TaskBLL
from apiserver.bll.task.utils import get_task_with_write_access
from apiserver.config_repo import config
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task
from apiserver.service_repo import APICall, endpoint
from apiserver.service_repo.auth import Identity
from apiserver.utilities import json, extract_properties_to_lists

task_bll = TaskBLL()
event_bll = EventBLL()
model_bll = ModelBLL()


def _assert_task_or_model_exists(
    company_id: str, task_ids: Union[str, Sequence[str]], model_events: bool
) -> Union[Sequence[Model], Sequence[Task]]:
    if model_events:
        return model_bll.assert_exists(
            company_id,
            task_ids,
            allow_public=True,
            only=("id", "name", "company", "company_origin"),
        )

    return task_bll.assert_exists(
        company_id,
        task_ids,
        allow_public=True,
        only=("id", "name", "company", "company_origin"),
    )


@endpoint("events.add")
def add(call: APICall, company_id, _):
    data = call.data.copy()
    added, err_count, err_info = event_bll.add_events(
        company_id=company_id,
        identity=call.identity,
        events=[data],
        worker=call.worker,
    )
    call.result.data = dict(added=added, errors=err_count, errors_info=err_info)


@endpoint("events.add_batch")
def add_batch(call: APICall, company_id, _):
    events = call.batched_data
    if events is None or len(events) == 0:
        raise errors.bad_request.BatchContainsNoItems()

    added, err_count, err_info = event_bll.add_events(
        company_id=company_id,
        identity=call.identity,
        events=events,
        worker=call.worker,
    )
    call.result.data = dict(added=added, errors=err_count, errors_info=err_info)


@endpoint("events.get_task_log")
def get_task_log_v1_5(call, company_id, request: LegacyLogEventsRequest):
    task_id = request.task
    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]
    order = request.order
    scroll_id = request.scroll_id
    batch_size = request.batch_size
    events, scroll_id, total_events = event_bll.scroll_task_events(
        task.get_index_company(),
        task_id,
        order,
        event_type=EventType.task_log,
        batch_size=batch_size,
        scroll_id=scroll_id,
    )
    call.result.data = dict(
        events=events, returned=len(events), total=total_events, scroll_id=scroll_id
    )


@endpoint("events.get_task_log", min_version="1.7")
def get_task_log_v1_7(call, company_id, request: LegacyLogEventsRequest):
    task_id = request.task
    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]

    order = request.order
    from_ = call.data.get("from") or "head"
    scroll_id = request.scroll_id
    batch_size = request.batch_size

    scroll_order = "asc" if (from_ == "head") else "desc"

    events, scroll_id, total_events = event_bll.scroll_task_events(
        company_id=task.get_index_company(),
        task_id=task_id,
        order=scroll_order,
        event_type=EventType.task_log,
        batch_size=batch_size,
        scroll_id=scroll_id,
    )

    if scroll_order != order:
        events = events[::-1]

    call.result.data = dict(
        events=events, returned=len(events), total=total_events, scroll_id=scroll_id
    )


@endpoint("events.get_task_log", min_version="2.9", request_data_model=LogEventsRequest)
def get_task_log(call, company_id, request: LogEventsRequest):
    task_id = request.task
    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]

    res = event_bll.events_iterator.get_task_events(
        event_type=EventType.task_log,
        company_id=task.get_index_company(),
        task_id=task_id,
        batch_size=request.batch_size,
        navigate_earlier=request.navigate_earlier,
        from_timestamp=request.from_timestamp,
        metric_variants=_get_metric_variants_from_request(request.metrics),
    )

    if request.order and (
        (request.navigate_earlier and request.order == LogOrderEnum.asc)
        or (not request.navigate_earlier and request.order == LogOrderEnum.desc)
    ):
        res.events.reverse()

    call.result.data = dict(
        events=res.events, returned=len(res.events), total=res.total_events
    )


@endpoint("events.download_task_log")
def download_task_log(call, company_id, request: TaskRequest):
    task_id = request.task
    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]

    line_type = call.data.get("line_type", "json").lower()
    line_format = str(call.data.get("line_format", "{asctime} {worker} {level} {msg}"))

    is_json = line_type == "json"
    if not is_json:
        if not line_format:
            raise errors.bad_request.MissingRequiredFields(
                "line_format is required for plain text lines"
            )

        # validate line format placeholders
        valid_task_log_fields = {"asctime", "timestamp", "level", "worker", "msg"}

        invalid_placeholders = set()
        while True:
            try:
                line_format.format(
                    **dict.fromkeys(valid_task_log_fields | invalid_placeholders)
                )
                break
            except KeyError as e:
                invalid_placeholders.add(e.args[0])
            except Exception as e:
                raise errors.bad_request.FieldsValueError(
                    "invalid line format", error=e.args[0]
                )

        if invalid_placeholders:
            raise errors.bad_request.FieldsValueError(
                "undefined placeholders in line format",
                placeholders=invalid_placeholders,
            )

        # make sure line_format has a trailing newline
        line_format = line_format.rstrip("\n") + "\n"

    def generate():
        scroll_id = None
        batch_size = 1000
        while True:
            log_events, scroll_id, _ = event_bll.scroll_task_events(
                task.get_index_company(),
                task_id,
                order="asc",
                event_type=EventType.task_log,
                batch_size=batch_size,
                scroll_id=scroll_id,
            )
            if not log_events:
                break
            for ev in log_events:
                ev["asctime"] = ev.pop("timestamp")
                if is_json:
                    ev.pop("type")
                    ev.pop("task")
                    yield json.dumps(ev) + "\n"
                else:
                    try:
                        yield line_format.format(**ev)
                    except KeyError as ex:
                        raise errors.bad_request.FieldsValueError(
                            "undefined placeholders in line format",
                            placeholders=[str(ex)],
                        )

            if len(log_events) < batch_size:
                break

    call.result.filename = "task_%s.log" % task_id
    call.result.content_type = "text/plain"
    call.result.raw_data = generate()


@endpoint("events.get_vector_metrics_and_variants")
def get_vector_metrics_and_variants(
    call, company_id, request: GetMetricsAndVariantsRequest
):
    task_id = request.task
    model_events = request.model_events
    task_or_model = _assert_task_or_model_exists(
        company_id,
        task_id,
        model_events=model_events,
    )[0]
    call.result.data = dict(
        metrics=event_bll.get_metrics_and_variants(
            task_or_model.get_index_company(), task_id, EventType.metrics_vector
        )
    )


@endpoint("events.get_scalar_metrics_and_variants")
def get_scalar_metrics_and_variants(
    call, company_id, request: GetMetricsAndVariantsRequest
):
    task_id = request.task
    model_events = request.model_events
    task_or_model = _assert_task_or_model_exists(
        company_id,
        task_id,
        model_events=model_events,
    )[0]
    call.result.data = dict(
        metrics=event_bll.get_metrics_and_variants(
            task_or_model.get_index_company(), task_id, EventType.metrics_scalar
        )
    )


# todo: !!! currently returning 10,000 records. should decide on a better way to control it
@endpoint(
    "events.vector_metrics_iter_histogram",
)
def vector_metrics_iter_histogram(
    call, company_id, request: VectorMetricsIterHistogramRequest
):
    task_id = request.task
    model_events = request.model_events
    task_or_model = _assert_task_or_model_exists(
        company_id,
        task_id,
        model_events=model_events,
    )[0]
    metric = request.metric
    variant = request.variant
    iterations, vectors = event_bll.get_vector_metrics_per_iter(
        task_or_model.get_index_company(), task_id, metric, variant
    )
    call.result.data = dict(
        metric=metric, variant=variant, vectors=vectors, iterations=iterations
    )


class GetTaskEventsScroll(Scroll):
    from_key_value = jsonmodels.fields.StringField()
    total = jsonmodels.fields.IntField()
    request: TaskEventsRequest = jsonmodels.fields.EmbeddedField(TaskEventsRequest)


def make_response(
    total: int, returned: int = 0, scroll_id: str = None, **kwargs
) -> dict:
    return {
        "returned": returned,
        "total": total,
        "scroll_id": scroll_id,
        **kwargs,
    }


@endpoint("events.get_task_events", request_data_model=TaskEventsRequest)
def get_task_events(_, company_id, request: TaskEventsRequest):
    task_id = request.task
    task_or_model = _assert_task_or_model_exists(
        company_id,
        task_id,
        model_events=request.model_events,
    )[0]

    key = ScalarKeyEnum.iter
    scalar_key = ScalarKey.resolve(key)

    if not request.scroll_id:
        from_key_value = None if (request.order == LogOrderEnum.desc) else 0
        total = None
    else:
        try:
            scroll = GetTaskEventsScroll.from_scroll_id(request.scroll_id)
        except ValueError:
            raise errors.bad_request.InvalidScrollId(scroll_id=request.scroll_id)

        if scroll.from_key_value is None:
            return make_response(
                scroll_id=request.scroll_id, total=scroll.total, events=[]
            )

        from_key_value = scalar_key.cast_value(scroll.from_key_value)
        total = scroll.total

        scroll.request.batch_size = request.batch_size or scroll.request.batch_size
        request = scroll.request

    navigate_earlier = request.order == LogOrderEnum.desc
    metric_variants = _get_metric_variants_from_request(request.metrics)

    if request.count_total and total is None:
        total = event_bll.events_iterator.count_task_events(
            event_type=request.event_type,
            company_id=task_or_model.get_index_company(),
            task_ids=[task_id],
            metric_variants=metric_variants,
        )

    batch_size = min(
        request.batch_size,
        int(
            config.get("services.events.events_retrieval.max_raw_scalars_size", 10_000)
        ),
    )

    res = event_bll.events_iterator.get_task_events(
        event_type=request.event_type,
        company_id=task_or_model.get_index_company(),
        task_id=task_id,
        batch_size=batch_size,
        key=ScalarKeyEnum.iter,
        navigate_earlier=navigate_earlier,
        from_key_value=from_key_value,
        metric_variants=metric_variants,
    )

    scroll = GetTaskEventsScroll(
        from_key_value=str(res.events[-1][scalar_key.field]) if res.events else None,
        total=total,
        request=request,
    )

    return make_response(
        returned=len(res.events),
        total=total,
        scroll_id=scroll.get_scroll_id(),
        events=res.events,
    )


@endpoint("events.get_scalar_metric_data")
def get_scalar_metric_data(call, company_id, request: GetScalarMetricDataRequest):
    task_id = request.task
    metric = request.metric
    scroll_id = request.scroll_id
    no_scroll = request.no_scroll
    model_events = request.model_events

    task_or_model = _assert_task_or_model_exists(
        company_id,
        task_id,
        model_events=model_events,
    )[0]
    result = event_bll.get_task_events(
        task_or_model.get_index_company(),
        task_id,
        event_type=EventType.metrics_scalar,
        sort=[{"iter": {"order": "desc"}}],
        metrics={metric: []},
        scroll_id=scroll_id,
        no_scroll=no_scroll,
    )

    call.result.data = dict(
        events=result.events,
        returned=len(result.events),
        total=result.total_events,
        scroll_id=result.next_scroll_id,
    )


@endpoint("events.get_task_latest_scalar_values")
def get_task_latest_scalar_values(call, company_id, request: TaskRequest):
    task_id = request.task
    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]
    index_company = task.get_index_company()
    metrics, last_timestamp = event_bll.get_task_latest_scalar_values(
        index_company, task_id
    )
    last_iters = event_bll.get_last_iters(
        company_id=index_company, event_type=EventType.all, task_id=task_id, iters=1
    ).get(task_id)
    call.result.data = dict(
        metrics=metrics,
        last_iter=last_iters[0] if last_iters else 0,
        name=task.name,
        status=task.status,
        last_timestamp=last_timestamp,
    )


# todo: should not repeat iter (x-axis) for each metric/variant, JS client should get raw data and fill gaps if needed
@endpoint(
    "events.scalar_metrics_iter_histogram",
    request_data_model=ScalarMetricsIterHistogramRequest,
)
def scalar_metrics_iter_histogram(
    call, company_id, request: ScalarMetricsIterHistogramRequest
):
    task_or_model = _assert_task_or_model_exists(
        company_id, request.task, model_events=request.model_events
    )[0]
    metrics = event_bll.metrics.get_scalar_metrics_average_per_iter(
        company_id=task_or_model.get_index_company(),
        task_id=request.task,
        samples=request.samples,
        key=request.key,
        metric_variants=_get_metric_variants_from_request(request.metrics),
        model_events=request.model_events,
    )
    call.result.data = metrics


def _get_task_or_model_index_companies(
    company_id: str,
    task_ids: Sequence[str],
    model_events=False,
) -> TaskCompanies:
    """
    Returns lists of tasks grouped by company
    """
    tasks_or_models = _assert_task_or_model_exists(
        company_id,
        task_ids,
        model_events=model_events,
    )

    unique_ids = set(task_ids)
    if len(tasks_or_models) < len(unique_ids):
        invalid = tuple(unique_ids - {t.id for t in tasks_or_models})
        error_cls = (
            errors.bad_request.InvalidModelId
            if model_events
            else errors.bad_request.InvalidTaskId
        )
        raise error_cls(company=company_id, ids=invalid)

    return bucketize(tasks_or_models, key=lambda t: t.get_index_company())


@endpoint(
    "events.multi_task_scalar_metrics_iter_histogram",
    request_data_model=MultiTaskScalarMetricsIterHistogramRequest,
)
def multi_task_scalar_metrics_iter_histogram(
    call, company_id, request: MultiTaskScalarMetricsIterHistogramRequest
):
    task_ids = request.tasks
    if isinstance(task_ids, str):
        task_ids = [s.strip() for s in task_ids.split(",")]

    call.result.data = dict(
        metrics=event_bll.metrics.compare_scalar_metrics_average_per_iter(
            companies=_get_task_or_model_index_companies(
                company_id, task_ids, request.model_events
            ),
            samples=request.samples,
            key=request.key,
            metric_variants=_get_metric_variants_from_request(request.metrics),
            model_events=request.model_events,
        )
    )


def _get_single_value_metrics_response(
    companies: TaskCompanies, value_metrics: Mapping[str, Sequence[dict]]
) -> Sequence[dict]:
    task_names = {
        task.id: task.name for task in itertools.chain.from_iterable(companies.values())
    }
    return [
        {"task": task_id, "task_name": task_names.get(task_id), "values": values}
        for task_id, values in value_metrics.items()
    ]


@endpoint("events.get_task_single_value_metrics")
def get_task_single_value_metrics(
    call, company_id: str, request: SingleValueMetricsRequest
):
    companies = _get_task_or_model_index_companies(
        company_id, request.tasks, request.model_events
    )
    call.result.data = dict(
        tasks=_get_single_value_metrics_response(
            companies=companies,
            value_metrics=event_bll.metrics.get_task_single_value_metrics(
                companies=companies,
                metric_variants=_get_metric_variants_from_request(request.metrics),
            ),
        )
    )


@endpoint("events.get_multi_task_plots")
def get_multi_task_plots_v1_7(call, company_id, request: LegacyMultiTaskEventsRequest):
    task_ids = request.tasks
    iters = request.iters
    scroll_id = request.scroll_id

    companies = _get_task_or_model_index_companies(company_id, task_ids)

    # Get last 10K events by iteration and group them by unique metric+variant, returning top events for combination
    result = event_bll.get_task_events(
        company_id=list(companies),
        task_id=task_ids,
        event_type=EventType.metrics_plot,
        sort=[{"iter": {"order": "desc"}}],
        size=10000,
        scroll_id=scroll_id,
    )

    task_names = {
        t.id: t.name for t in itertools.chain.from_iterable(companies.values())
    }
    return_events = _get_top_iter_unique_events_per_task(
        result.events, max_iters=iters, task_names=task_names
    )

    call.result.data = dict(
        plots=return_events,
        returned=len(return_events),
        total=result.total_events,
        scroll_id=result.next_scroll_id,
    )


def _get_multitask_plots(
    companies: TaskCompanies,
    last_iters: int,
    last_iters_per_task_metric: bool,
    request_metrics: Sequence[ApiMetrics] = None,
    scroll_id=None,
    no_scroll=True,
) -> Tuple[dict, int, str]:
    metrics = _get_metric_variants_from_request(request_metrics)
    task_names = {
        t.id: t.name for t in itertools.chain.from_iterable(companies.values())
    }
    result = event_bll.get_task_events(
        company_id=list(companies),
        task_id=list(task_names),
        event_type=EventType.metrics_plot,
        metrics=metrics,
        last_iter_count=last_iters,
        sort=[{"iter": {"order": "desc"}}],
        scroll_id=scroll_id,
        no_scroll=no_scroll,
        size=config.get(
            "services.events.events_retrieval.multi_plots_batch_size", 1000
        ),
        last_iters_per_task_metric=last_iters_per_task_metric,
    )
    return_events = _get_top_iter_unique_events_per_task(
        result.events, max_iters=last_iters, task_names=task_names
    )
    return return_events, result.total_events, result.next_scroll_id


@endpoint("events.get_multi_task_plots", min_version="1.8")
def get_multi_task_plots(call, company_id, request: MultiTaskPlotsRequest):
    companies = _get_task_or_model_index_companies(
        company_id, request.tasks, model_events=request.model_events
    )
    return_events, total_events, next_scroll_id = _get_multitask_plots(
        companies=companies,
        last_iters=request.iters,
        scroll_id=request.scroll_id,
        no_scroll=request.no_scroll,
        last_iters_per_task_metric=request.last_iters_per_task_metric,
        request_metrics=request.metrics,
    )
    call.result.data = dict(
        plots=return_events,
        returned=len(return_events),
        total=total_events,
        scroll_id=next_scroll_id,
    )


@endpoint("events.get_task_plots")
def get_task_plots_v1_7(call, company_id, request: LegacyMetricEventsRequest):
    task_id = request.task
    iters = request.iters
    scroll_id = request.scroll_id

    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]
    # events, next_scroll_id, total_events = event_bll.get_task_events(
    #     company, task_id,
    #     event_type="plot",
    #     sort=[{"iter": {"order": "desc"}}],
    #     last_iter_count=iters,
    #     scroll_id=scroll_id)

    # get last 10K events by iteration and group them by unique metric+variant, returning top events for combination
    result = event_bll.get_task_events(
        task.get_index_company(),
        task_id,
        event_type=EventType.metrics_plot,
        sort=[{"iter": {"order": "desc"}}],
        size=10000,
        scroll_id=scroll_id,
    )

    return_events = _get_top_iter_unique_events(result.events, max_iters=iters)

    call.result.data = dict(
        plots=return_events,
        returned=len(return_events),
        total=result.total_events,
        scroll_id=result.next_scroll_id,
    )


def _get_metric_variants_from_request(
    req_metrics: Sequence[ApiMetrics],
) -> Optional[MetricVariants]:
    if not req_metrics:
        return None

    return {m.metric: m.variants for m in req_metrics}


@endpoint(
    "events.get_task_plots", min_version="1.8", request_data_model=TaskPlotsRequest
)
def get_task_plots(call, company_id, request: TaskPlotsRequest):
    task_id = request.task
    iters = request.iters

    task_or_model = _assert_task_or_model_exists(
        company_id, task_id, model_events=request.model_events
    )[0]
    result = event_bll.get_task_plots(
        task_or_model.get_index_company(),
        task_id=task_id,
        last_iterations_per_plot=iters,
        metric_variants=_get_metric_variants_from_request(request.metrics),
    )

    return_events = result.events

    call.result.data = dict(
        plots=return_events,
        returned=len(return_events),
        total=result.total_events,
        scroll_id=result.next_scroll_id,
    )


def _task_metrics_dict_from_request(req_metrics: Sequence[TaskMetric]) -> dict:
    task_metrics = defaultdict(dict)
    for tm in req_metrics:
        task_metrics[tm.task][tm.metric] = tm.variants
    for metrics in task_metrics.values():
        if None in metrics:
            metrics.clear()

    return task_metrics


def _get_metrics_response(metric_events: Sequence[tuple]) -> Sequence[MetricEvents]:
    return [
        MetricEvents(
            task=task,
            iterations=[
                IterationEvents(iter=iteration["iter"], events=iteration["events"])
                for iteration in iterations
            ],
        )
        for (task, iterations) in metric_events
    ]


@endpoint(
    "events.plots",
    request_data_model=MetricEventsRequest,
    response_data_model=MetricEventsResponse,
)
def task_plots(call, company_id, request: MetricEventsRequest):
    task_metrics = _task_metrics_dict_from_request(request.metrics)
    task_ids = list(task_metrics)
    task_or_models = _assert_task_or_model_exists(
        company_id, task_ids=task_ids, model_events=request.model_events
    )
    result = event_bll.plots_iterator.get_task_events(
        companies={t.id: t.get_index_company() for t in task_or_models},
        task_metrics=task_metrics,
        iter_count=request.iters,
        navigate_earlier=request.navigate_earlier,
        refresh=request.refresh,
        state_id=request.scroll_id,
    )

    call.result.data_model = MetricEventsResponse(
        scroll_id=result.next_scroll_id,
        metrics=_get_metrics_response(result.metric_events),
    )


@endpoint("events.debug_images")
def get_debug_images_v1_7(call, company_id, request: LegacyMetricEventsRequest):
    task_id = request.task
    iters = request.iters
    scroll_id = request.scroll_id

    task = task_bll.assert_exists(
        company_id, task_id, allow_public=True, only=("company", "company_origin")
    )[0]
    # events, next_scroll_id, total_events = event_bll.get_task_events(
    #     company, task_id,
    #     event_type="training_debug_image",
    #     sort=[{"iter": {"order": "desc"}}],
    #     last_iter_count=iters,
    #     scroll_id=scroll_id)

    # get last 10K events by iteration and group them by unique metric+variant, returning top events for combination
    result = event_bll.get_task_events(
        task.get_index_company(),
        task_id,
        event_type=EventType.metrics_image,
        sort=[{"iter": {"order": "desc"}}],
        size=10000,
        scroll_id=scroll_id,
    )

    return_events = _get_top_iter_unique_events(result.events, max_iters=iters)

    call.result.data = dict(
        task=task_id,
        images=return_events,
        returned=len(return_events),
        total=result.total_events,
        scroll_id=result.next_scroll_id,
    )


@endpoint("events.debug_images", min_version="1.8")
def get_debug_images_v1_8(call, company_id, request: LegacyMetricEventsRequest):
    task_id = request.task
    iters = request.iters
    scroll_id = request.scroll_id
    model_events = request.model_events

    tasks_or_model = _assert_task_or_model_exists(
        company_id,
        task_id,
        model_events=model_events,
    )[0]
    result = event_bll.get_task_events(
        tasks_or_model.get_index_company(),
        task_id,
        event_type=EventType.metrics_image,
        sort=[{"iter": {"order": "desc"}}],
        last_iter_count=iters,
        scroll_id=scroll_id,
    )

    return_events = result.events

    call.result.data = dict(
        task=task_id,
        images=return_events,
        returned=len(return_events),
        total=result.total_events,
        scroll_id=result.next_scroll_id,
    )


@endpoint(
    "events.debug_images",
    min_version="2.7",
    request_data_model=MetricEventsRequest,
    response_data_model=MetricEventsResponse,
)
def get_debug_images(call, company_id, request: MetricEventsRequest):
    task_metrics = _task_metrics_dict_from_request(request.metrics)
    task_ids = list(task_metrics)
    task_or_models = _assert_task_or_model_exists(
        company_id, task_ids=task_ids, model_events=request.model_events
    )
    result = event_bll.debug_images_iterator.get_task_events(
        companies={t.id: t.get_index_company() for t in task_or_models},
        task_metrics=task_metrics,
        iter_count=request.iters,
        navigate_earlier=request.navigate_earlier,
        refresh=request.refresh,
        state_id=request.scroll_id,
    )

    call.result.data_model = MetricEventsResponse(
        scroll_id=result.next_scroll_id,
        metrics=_get_metrics_response(result.metric_events),
    )


@endpoint(
    "events.get_debug_image_sample",
    min_version="2.12",
    request_data_model=GetVariantSampleRequest,
)
def get_debug_image_sample(call, company_id, request: GetVariantSampleRequest):
    task_or_model = _assert_task_or_model_exists(
        company_id,
        request.task,
        model_events=request.model_events,
    )[0]
    res = event_bll.debug_image_sample_history.get_sample_for_variant(
        company_id=task_or_model.get_index_company(),
        task=request.task,
        metric=request.metric,
        variant=request.variant,
        iteration=request.iteration,
        refresh=request.refresh,
        state_id=request.scroll_id,
        navigate_current_metric=request.navigate_current_metric,
    )
    call.result.data = attr.asdict(res, recurse=False)


@endpoint(
    "events.next_debug_image_sample",
    min_version="2.12",
    request_data_model=NextHistorySampleRequest,
)
def next_debug_image_sample(call, company_id, request: NextHistorySampleRequest):
    task_or_model = _assert_task_or_model_exists(
        company_id,
        request.task,
        model_events=request.model_events,
    )[0]
    res = event_bll.debug_image_sample_history.get_next_sample(
        company_id=task_or_model.get_index_company(),
        task=request.task,
        state_id=request.scroll_id,
        navigate_earlier=request.navigate_earlier,
        next_iteration=request.next_iteration,
    )
    call.result.data = attr.asdict(res, recurse=False)


@endpoint(
    "events.get_plot_sample",
    request_data_model=GetMetricSamplesRequest,
)
def get_plot_sample(call, company_id, request: GetMetricSamplesRequest):
    task_or_model = _assert_task_or_model_exists(
        company_id,
        request.task,
        model_events=request.model_events,
    )[0]
    res = event_bll.plot_sample_history.get_samples_for_metric(
        company_id=task_or_model.get_index_company(),
        task=request.task,
        metric=request.metric,
        iteration=request.iteration,
        refresh=request.refresh,
        state_id=request.scroll_id,
        navigate_current_metric=request.navigate_current_metric,
    )
    call.result.data = attr.asdict(res, recurse=False)


@endpoint(
    "events.next_plot_sample",
    request_data_model=NextHistorySampleRequest,
)
def next_plot_sample(call, company_id, request: NextHistorySampleRequest):
    task_or_model = _assert_task_or_model_exists(
        company_id,
        request.task,
        model_events=request.model_events,
    )[0]
    res = event_bll.plot_sample_history.get_next_sample(
        company_id=task_or_model.get_index_company(),
        task=request.task,
        state_id=request.scroll_id,
        navigate_earlier=request.navigate_earlier,
        next_iteration=request.next_iteration,
    )
    call.result.data = attr.asdict(res, recurse=False)


@endpoint("events.get_task_metrics", request_data_model=TaskMetricsRequest)
def get_task_metrics(call: APICall, company_id, request: TaskMetricsRequest):
    task_or_models = _assert_task_or_model_exists(
        company_id,
        request.tasks,
        model_events=request.model_events,
    )
    res = event_bll.metrics.get_task_metrics(
        task_or_models[0].get_index_company(),
        task_ids=request.tasks,
        event_type=request.event_type,
    )
    call.result.data = {
        "metrics": [{"task": task, "metrics": metrics} for (task, metrics) in res]
    }


@endpoint("events.get_multi_task_metrics")
def get_multi_task_metrics(call: APICall, company_id, request: MultiTaskMetricsRequest):
    companies = _get_task_or_model_index_companies(
        company_id, request.tasks, model_events=request.model_events
    )
    if not companies:
        return {"metrics": []}

    metrics = event_bll.metrics.get_multi_task_metrics(
        companies=companies, event_type=request.event_type
    )
    res = [
        {
            "metric": m,
            "variants": sorted(vars_),
        }
        for m, vars_ in metrics.items()
    ]
    call.result.data = {"metrics": sorted(res, key=itemgetter("metric"))}


def _validate_task_for_events_update(
    company_id: str, task_id: str, identity: Identity, allow_locked: bool
):
    task = get_task_with_write_access(
        task_id=task_id,
        company_id=company_id,
        identity=identity,
        only=("id", "status"),
    )
    if not allow_locked and task.status in LOCKED_TASK_STATUSES:
        raise errors.bad_request.InvalidTaskId(
            replacement_msg="Cannot update events for a published task",
            company=company_id,
            id=task_id,
        )


@endpoint("events.delete_for_task")
def delete_for_task(call, company_id, request: TaskRequest):
    task_id = request.task
    allow_locked = call.data.get("allow_locked", False)

    _validate_task_for_events_update(
        company_id=company_id,
        task_id=task_id,
        identity=call.identity,
        allow_locked=allow_locked,
    )

    call.result.data = dict(
        deleted=event_bll.delete_task_events(company_id, task_id, wait_for_delete=True)
    )


def _validate_model_for_events_update(
    company_id: str, model_id: str, allow_locked: bool
):
    model = model_bll.assert_exists(company_id, model_id, only=("id", "ready"))[0]
    if not allow_locked and model.ready:
        raise errors.bad_request.InvalidModelId(
            replacement_msg="Cannot update events for a published model",
            company=company_id,
            id=model_id,
        )


@endpoint("events.delete_for_model")
def delete_for_model(call: APICall, company_id: str, request: ModelRequest):
    model_id = request.model
    allow_locked = call.data.get("allow_locked", False)

    _validate_model_for_events_update(
        company_id=company_id, model_id=model_id, allow_locked=allow_locked
    )

    call.result.data = dict(
        deleted=event_bll.delete_task_events(
            company_id, model_id, model=True, wait_for_delete=True
        )
    )


@endpoint("events.clear_task_log")
def clear_task_log(call: APICall, company_id: str, request: ClearTaskLogRequest):
    task_id = request.task

    _validate_task_for_events_update(
        company_id=company_id,
        task_id=task_id,
        identity=call.identity,
        allow_locked=request.allow_locked,
    )

    call.result.data = dict(
        deleted=event_bll.clear_task_log(
            company_id=company_id,
            task_id=task_id,
            threshold_sec=request.threshold_sec,
            exclude_metrics=request.exclude_metrics,
            include_metrics=request.include_metrics,
        )
    )


def _get_top_iter_unique_events_per_task(
    events, max_iters: int, task_names: Mapping[str, str]
):
    key_fields = ("metric", "variant", "task")
    unique_events = itertools.chain.from_iterable(
        itertools.islice(group, max_iters)
        for _, group in itertools.groupby(
            sorted(events, key=itemgetter(*(key_fields + ("iter",))), reverse=True),
            key=itemgetter(*key_fields),
        )
    )

    def collect(evs, fields):
        if not fields:
            evs = list(evs)
            return {"name": task_names.get(evs[0].get("task")), "plots": evs}
        return {
            str(k): collect(group, fields[1:])
            for k, group in itertools.groupby(evs, key=itemgetter(fields[0]))
        }

    collect_fields = ("metric", "variant", "task", "iter")
    return collect(
        sorted(unique_events, key=itemgetter(*collect_fields), reverse=True),
        collect_fields,
    )


def _get_top_iter_unique_events(events, max_iters):
    top_unique_events = defaultdict(lambda: [])
    for ev in events:
        key = ev.get("metric", "") + ev.get("variant", "")
        evs = top_unique_events[key]
        if len(evs) < max_iters:
            evs.append(ev)
    unique_events = list(
        itertools.chain.from_iterable(list(top_unique_events.values()))
    )
    unique_events.sort(key=lambda e: e["iter"], reverse=True)
    return unique_events


class ScalarMetricsIterRawScroll(Scroll):
    from_key_value = jsonmodels.fields.StringField()
    total = jsonmodels.fields.IntField()
    request: ScalarMetricsIterRawRequest = jsonmodels.fields.EmbeddedField(
        ScalarMetricsIterRawRequest
    )


@endpoint("events.scalar_metrics_iter_raw", min_version="2.16")
def scalar_metrics_iter_raw(
    call: APICall, company_id: str, request: ScalarMetricsIterRawRequest
):
    key = request.key or ScalarKeyEnum.iter
    scalar_key = ScalarKey.resolve(key)
    if request.batch_size and request.batch_size < 0:
        raise errors.bad_request.ValidationError(
            "batch_size should be non negative number"
        )

    if not request.scroll_id:
        from_key_value = None
        total = None
        request.batch_size = request.batch_size or 10_000
    else:
        try:
            scroll = ScalarMetricsIterRawScroll.from_scroll_id(request.scroll_id)
        except ValueError:
            raise errors.bad_request.InvalidScrollId(scroll_id=request.scroll_id)

        if scroll.from_key_value is None:
            return make_response(
                scroll_id=request.scroll_id, total=scroll.total, variants={}
            )

        from_key_value = scalar_key.cast_value(scroll.from_key_value)
        total = scroll.total
        request.batch_size = request.batch_size or scroll.request.batch_size

    task_id = request.task
    task_or_model = _assert_task_or_model_exists(
        company_id, task_id, model_events=request.model_events
    )[0]
    metric_variants = _get_metric_variants_from_request([request.metric])

    if request.count_total and total is None:
        total = event_bll.events_iterator.count_task_events(
            event_type=EventType.metrics_scalar,
            company_id=task_or_model.get_index_company(),
            task_ids=[task_id],
            metric_variants=metric_variants,
        )

    batch_size = min(
        request.batch_size,
        int(
            config.get("services.events.events_retrieval.max_raw_scalars_size", 200_000)
        ),
    )

    events = []
    for iteration in range(0, math.ceil(batch_size / 10_000)):
        res = event_bll.events_iterator.get_task_events(
            event_type=EventType.metrics_scalar,
            company_id=task_or_model.get_index_company(),
            task_id=task_id,
            batch_size=min(batch_size, 10_000),
            navigate_earlier=False,
            from_key_value=from_key_value,
            metric_variants=metric_variants,
            key=key,
        )
        if not res.events:
            break
        events.extend(res.events)
        from_key_value = str(events[-1][scalar_key.field])

    key = str(key)
    variants = {
        variant: extract_properties_to_lists(
            ["value", scalar_key.field], events, target_keys=["y", key]
        )
        for variant, events in bucketize(events, key=itemgetter("variant")).items()
    }

    call.kpis["events"] = len(events)

    scroll = ScalarMetricsIterRawScroll(
        from_key_value=str(events[-1][scalar_key.field]) if events else None,
        total=total,
        request=request,
    )

    return make_response(
        returned=len(events),
        total=total,
        scroll_id=scroll.get_scroll_id(),
        variants=variants,
    )


@endpoint("events.clear_scroll", min_version="2.18")
def clear_scroll(_, __, request: ClearScrollRequest):
    if request.scroll_id:
        event_bll.clear_scroll(request.scroll_id)
