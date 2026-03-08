from datetime import datetime

from pyhocon.config_tree import NoneValue

from apiserver.apierrors import errors
from apiserver.apimodels.server import (
    ReportStatsOptionRequest,
    ReportStatsOptionResponse,
    GetConfigRequest,
)
from apiserver.bll.statistics.stats_reporter import StatisticsReporter
from apiserver.config_repo import config
from apiserver.config.info import get_version, get_build_number, get_commit_number
from apiserver.database.errors import translate_errors_context
from apiserver.database.model import Company
from apiserver.database.model.company import ReportStatsOption
from apiserver.database.model.settings import Settings, SettingKeys
from apiserver.service_repo import ServiceRepo, APICall, endpoint


@endpoint("server.get_stats")
def get_stats(call: APICall):
    call.result.data = StatisticsReporter.get_statistics(
        company_id=call.identity.company
    )


@endpoint("server.config")
def get_config(call: APICall, _, request: GetConfigRequest):
    path = request.path
    if path:
        c = dict(config.get(path))
    else:
        c = config.to_dict()

    def remove_none_value(x):
        """
        Pyhocon bug in Python 3: leaves dummy "NoneValue"s in tree,
        see: https://github.com/chimpler/pyhocon/issues/111
        """
        if isinstance(x, dict):
            return {key: remove_none_value(value) for key, value in x.items()}
        if isinstance(x, list):
            return list(map(remove_none_value, x))
        if isinstance(x, NoneValue):
            return None
        return x

    c.pop("secure", None)

    call.result.data = remove_none_value(c)


@endpoint("server.endpoints")
def get_endpoints(call: APICall):
    call.result.data = ServiceRepo.endpoints_summary()


@endpoint("server.info")
def info(call: APICall):
    call.result.data = {
        "version": get_version(),
        "build": get_build_number(),
        "commit": get_commit_number(),
    }


@endpoint("server.info", min_version="2.8")
def info_2_8(call: APICall):
    info(call)
    call.result.data["uid"] = Settings.get_by_key(SettingKeys.server__uuid)


@endpoint("server.info", min_version="2.12")
def info_2_12(call: APICall):
    info(call)
    call.result.data["api_version"] = str(ServiceRepo.max_endpoint_version())


@endpoint(
    "server.report_stats_option",
    request_data_model=ReportStatsOptionRequest,
    response_data_model=ReportStatsOptionResponse,
)
def report_stats(call: APICall, company: str, request: ReportStatsOptionRequest):
    if not StatisticsReporter.supported:
        result = ReportStatsOptionResponse(supported=False)
    else:
        enabled = request.enabled
        with translate_errors_context():
            query = Company.objects(id=company)
            if enabled is None:
                stats_option = query.first().defaults.stats_option
            else:
                stats_option = ReportStatsOption(
                    enabled=enabled,
                    enabled_time=datetime.utcnow(),
                    enabled_version=get_version(),
                    enabled_user=call.identity.user,
                )
                updated = query.update(defaults__stats_option=stats_option)
                if not updated:
                    raise errors.server_error.InternalError(
                        f"Failed setting report_stats to {enabled}"
                    )
        data = stats_option.to_mongo()
        data["current_version"] = get_version()
        result = ReportStatsOptionResponse(**data)

    call.result.data_model = result
