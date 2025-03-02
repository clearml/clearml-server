from apiserver.apimodels.serving import (
    RegisterRequest,
    UnregisterRequest,
    StatusReportRequest,
    GetEndpointDetailsRequest,
    GetEndpointMetricsHistoryRequest,
)
from apiserver.apierrors import errors
from apiserver.service_repo import endpoint, APICall
from apiserver.bll.serving import ServingBLL, ServingStats


serving_bll = ServingBLL()


@endpoint("serving.register_container")
def register_container(call: APICall, company: str, request: RegisterRequest):
    serving_bll.register_serving_container(
        company_id=company, ip=call.real_ip, request=request
    )


@endpoint("serving.unregister_container")
def unregister_container(_: APICall, company: str, request: UnregisterRequest):
    serving_bll.unregister_serving_container(
        company_id=company, container_id=request.container_id
    )


@endpoint("serving.container_status_report")
def container_status_report(call: APICall, company: str, request: StatusReportRequest):
    if not request.endpoint_url:
        raise errors.bad_request.ValidationError(
            "Missing required field 'endpoint_url'"
        )
    serving_bll.container_status_report(
        company_id=company,
        ip=call.real_ip,
        report=request,
    )


@endpoint("serving.get_endpoints")
def get_endpoints(call: APICall, company: str, _):
    call.result.data = {"endpoints": serving_bll.get_endpoints(company)}


@endpoint("serving.get_loading_instances")
def get_loading_instances(call: APICall, company: str, _):
    call.result.data = {"instances": serving_bll.get_loading_instances(company)}


@endpoint("serving.get_endpoint_details")
def get_endpoint_details(
    call: APICall, company: str, request: GetEndpointDetailsRequest
):
    call.result.data = serving_bll.get_endpoint_details(
        company_id=company, endpoint_url=request.endpoint_url
    )


@endpoint("serving.get_endpoint_metrics_history")
def get_endpoint_metrics_history(
    call: APICall, company: str, request: GetEndpointMetricsHistoryRequest
):
    call.result.data = ServingStats.get_endpoint_metrics(
        company_id=company,
        metrics_request=request,
    )
