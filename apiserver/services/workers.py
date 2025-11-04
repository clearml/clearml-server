from apiserver.apierrors.errors import bad_request
from apiserver.apimodels.workers import (
    WorkerRequest,
    StatusReportRequest,
    GetAllRequest,
    GetAllResponse,
    RegisterRequest,
    GetStatsRequest,
    MetricCategory,
    GetMetricKeysRequest,
    GetMetricKeysResponse,
    GetStatsResponse,
    WorkerStatistics,
    MetricStats,
    AggregationStats,
    GetActivityReportRequest,
    GetActivityReportResponse,
    ActivityReportSeries,
    GetCountRequest,
    MetricResourceSeries,
)
from apiserver.bll.workers import WorkerBLL
from apiserver.config_repo import config
from apiserver.service_repo import APICall, endpoint
from apiserver.utilities import extract_properties_to_lists

log = config.logger(__file__)

worker_bll = WorkerBLL()


@endpoint(
    "workers.get_all",
    min_version="2.4",
    request_data_model=GetAllRequest,
    response_data_model=GetAllResponse,
)
def get_all(call: APICall, company_id: str, request: GetAllRequest):
    call.result.data_model = GetAllResponse(
        workers=worker_bll.get_all_with_projection(
            company_id,
            request.last_seen,
            tags=request.tags,
            system_tags=request.system_tags,
            worker_pattern=request.worker_pattern,
        )
    )


@endpoint(
    "workers.get_count", request_data_model=GetCountRequest,
)
def get_all(call: APICall, company_id: str, request: GetCountRequest):
    call.result.data = {
        "count": worker_bll.get_count(
            company_id,
            request.last_seen,
            tags=request.tags,
            system_tags=request.system_tags,
            worker_pattern=request.worker_pattern,
        )
    }


@endpoint("workers.register", min_version="2.4", request_data_model=RegisterRequest)
def register(call: APICall, company_id, request: RegisterRequest):
    worker = request.worker
    timeout = request.timeout
    queues = request.queues

    if not timeout:
        timeout = config.get("apiserver.workers.default_timeout", 10 * 60)

    if not timeout or timeout <= 0:
        raise bad_request.WorkerRegistrationFailed(
            "invalid timeout", timeout=timeout, worker=worker
        )

    worker_bll.register_worker(
        company_id=company_id,
        user_id=call.identity.user,
        worker=worker,
        ip=call.real_ip,
        queues=queues,
        timeout=timeout,
        tags=request.tags,
        system_tags=request.system_tags,
        resources=request.resources,
    )


@endpoint("workers.unregister", min_version="2.4", request_data_model=WorkerRequest)
def unregister(call: APICall, company_id, req_model: WorkerRequest):
    worker_bll.unregister_worker(company_id, call.identity.user, req_model.worker)


@endpoint(
    "workers.status_report", min_version="2.4", request_data_model=StatusReportRequest
)
def status_report(call: APICall, company_id, request: StatusReportRequest):
    worker_bll.status_report(
        company_id=company_id,
        user_id=call.identity.user,
        ip=call.real_ip,
        report=request,
        tags=request.tags,
        system_tags=request.system_tags,
    )


@endpoint(
    "workers.get_metric_keys",
    min_version="2.4",
    request_data_model=GetMetricKeysRequest,
    response_data_model=GetMetricKeysResponse,
    validate_schema=True,
)
def get_metric_keys(
    call: APICall, company_id, req_model: GetMetricKeysRequest
) -> GetMetricKeysResponse:
    ret = worker_bll.stats.get_worker_stats_keys(
        company_id, worker_ids=req_model.worker_ids
    )
    return GetMetricKeysResponse(
        categories=[MetricCategory(name=k, metric_keys=v) for k, v in ret.items()]
    )


@endpoint(
    "workers.get_activity_report",
    min_version="2.4",
    request_data_model=GetActivityReportRequest,
    response_data_model=GetActivityReportResponse,
    validate_schema=True,
)
def get_activity_report(
    call: APICall, company_id, req_model: GetActivityReportRequest
) -> GetActivityReportResponse:
    def get_activity_series(active_only: bool = False) -> ActivityReportSeries:
        ret = worker_bll.stats.get_activity_report(
            company_id=company_id,
            from_date=req_model.from_date,
            to_date=req_model.to_date,
            interval=req_model.interval,
            active_only=active_only,
        )
        if not ret:
            return ActivityReportSeries(dates=[], counts=[])
        count_by_date = extract_properties_to_lists(["date", "count"], ret)
        return ActivityReportSeries(
            dates=count_by_date["date"], counts=count_by_date["count"]
        )

    return GetActivityReportResponse(
        total=get_activity_series(), active=get_activity_series(active_only=True)
    )


@endpoint(
    "workers.get_stats",
    min_version="2.4",
    response_data_model=GetStatsResponse,
    validate_schema=True,
)
def get_stats(call: APICall, company_id, request: GetStatsRequest):
    ret = worker_bll.stats.get_worker_stats(company_id, request)

    def _get_agg_stats(
        aggregation: str,
        stats: dict,
    ) -> AggregationStats:
        resource_series = []
        if "resource_series" in stats:
            for name, values in stats["resource_series"].items():
                resource_series.append(
                    MetricResourceSeries(
                        name=name,
                        values=values
                    )
                )
        return AggregationStats(
            aggregation=aggregation,
            dates=stats["dates"],
            values=stats["values"],
            resource_series=resource_series,
        )

    return GetStatsResponse(
        workers=[
            WorkerStatistics(
                worker=worker,
                metrics=[
                    MetricStats(
                        metric=metric,
                        stats=[
                            _get_agg_stats(aggregation, a_stats)
                            for aggregation, a_stats in m_stats.items()
                        ]
                    )
                    for metric, m_stats in w_stats.items()
                ],
            )
            for worker, w_stats in ret.items()
        ]
    )
