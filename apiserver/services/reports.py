import textwrap
from datetime import datetime
from itertools import chain
from typing import Sequence

from mongoengine import Q

from apiserver.apimodels.reports import (
    CreateReportRequest,
    UpdateReportRequest,
    PublishReportRequest,
    ArchiveReportRequest,
    DeleteReportRequest,
    MoveReportRequest,
    GetTasksDataRequest,
    EventsRequest,
    GetAllRequest,
)
from apiserver.apierrors import errors
from apiserver.apimodels.base import UpdateResponse
from apiserver.bll.project.project_bll import reports_project_name, reports_tag
from apiserver.bll.task.utils import get_task_with_write_access
from apiserver.database.model.model import Model
from apiserver.service_repo.auth import Identity
from apiserver.services.models import conform_model_data
from apiserver.services.utils import process_include_subprojects, sort_tags_response
from apiserver.bll.organization import OrgBLL
from apiserver.bll.project import ProjectBLL
from apiserver.bll.task import TaskBLL, ChangeStatusRequest
from apiserver.database.model import EntityVisibility
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskType, TaskStatus
from apiserver.service_repo import APICall, endpoint
from apiserver.services.events import (
    _get_task_or_model_index_companies,
    event_bll,
    _get_metrics_response,
    _get_metric_variants_from_request,
    _get_multitask_plots,
    _get_single_value_metrics_response,
)
from apiserver.services.tasks import (
    escape_execution_parameters,
    _hidden_query,
    conform_task_data,
)

org_bll = OrgBLL()
project_bll = ProjectBLL()
task_bll = TaskBLL()


update_fields = {
    "name",
    "tags",
    "comment",
    "report",
    "report_assets",
}


def _assert_report(company_id: str, task_id: str, identity: Identity, only_fields=None):
    if only_fields and "type" not in only_fields:
        only_fields += ("type",)

    task = get_task_with_write_access(
        task_id=task_id,
        company_id=company_id,
        identity=identity,
        only=only_fields,
    )
    if task.type != TaskType.report:
        raise errors.bad_request.OperationSupportedOnReportsOnly(id=task_id)

    return task


@endpoint("reports.update", response_data_model=UpdateResponse)
def update_report(call: APICall, company_id: str, request: UpdateReportRequest):
    task = _assert_report(
        task_id=request.task,
        company_id=company_id,
        identity=call.identity,
        only_fields=("status",),
    )

    partial_update_dict = {
        field: value for field, value in call.data.items() if field in update_fields
    }
    if not partial_update_dict:
        return UpdateResponse(updated=0)

    allowed_for_published = set(partial_update_dict.keys()).issubset(
        {"tags", "name", "comment"}
    )
    if task.status != TaskStatus.created and not allowed_for_published:
        raise errors.bad_request.InvalidTaskStatus(
            expected=TaskStatus.created, status=task.status
        )

    now = datetime.utcnow()
    more_updates = {"last_change": now, "last_changed_by": call.identity.user}
    if not allowed_for_published:
        more_updates["last_update"] = now

    updated = task.update(upsert=False, **partial_update_dict, **more_updates)
    if not updated:
        return UpdateResponse(updated=0)

    updated_tags = partial_update_dict.get("tags")
    if updated_tags:
        partial_update_dict["tags"] = sorted(updated_tags)
    updated_report = partial_update_dict.get("report")
    if updated_report:
        partial_update_dict["report"] = textwrap.shorten(updated_report, width=100)

    return UpdateResponse(updated=updated, fields=partial_update_dict)


def _ensure_reports_project(company: str, user: str, name: str):
    name = name.strip("/")
    _, _, basename = name.rpartition("/")
    if basename != reports_project_name:
        name = f"{name}/{reports_project_name}"

    return project_bll.find_or_create(
        user=user,
        company=company,
        project_name=name,
        description="Reports project",
        system_tags=[reports_tag, EntityVisibility.hidden.value],
    )


@endpoint("reports.create")
def create_report(call: APICall, company_id: str, request: CreateReportRequest):
    user_id = call.identity.user
    project_id = request.project
    if request.project:
        project = Project.get_for_writing(
            company=company_id, id=project_id, _only=("name",)
        )
        project_name = project.name
    else:
        project_name = ""

    project_id = _ensure_reports_project(
        company=company_id, user=user_id, name=project_name
    )
    task = task_bll.create(
        company=company_id,
        user=user_id,
        fields=dict(
            project=project_id,
            name=request.name,
            tags=request.tags,
            comment=request.comment,
            type=TaskType.report,
            system_tags=[reports_tag, EntityVisibility.hidden.value],
        ),
    )
    task.save()

    call.result.data = {"id": task.id, "project_id": project_id}


def _delete_reports_project_if_empty(project_id):
    project = Project.objects(id=project_id).only("basename").first()
    if (
        project
        and project.basename == reports_project_name
        and Task.objects(project=project_id).count() == 0
    ):
        project.delete()


@endpoint("reports.get_all_ex")
def get_all_ex(call: APICall, company_id, request: GetAllRequest):
    call_data = call.data
    call_data["type"] = TaskType.report
    process_include_subprojects(call_data)
    # bring projects one level down in case not the .reports project was passed
    if "project" in call_data:
        project_ids = call_data["project"]
        if not isinstance(project_ids, list):
            project_ids = [project_ids]

        query = Q(parent__in=project_ids) | Q(id__in=project_ids)
        project_ids = Project.objects(query & Q(basename=reports_project_name)).scalar(
            "id"
        )
        if not project_ids:
            return {"tasks": []}
        call_data["project"] = list(project_ids)

    ret_params = {}
    tasks = Task.get_many_with_join(
        company=company_id,
        query_dict=call_data,
        allow_public=request.allow_public,
        ret_params=ret_params,
    )
    conform_task_data(call, tasks)

    call.result.data = {"tasks": tasks, **ret_params}


def _get_task_metrics_from_request(
    task_ids: Sequence[str], request: EventsRequest
) -> dict:
    task_metrics = {}
    for task in task_ids:
        task_dict = {}
        for mv in request.metrics:
            task_dict[mv.metric] = mv.variants
        task_metrics[task] = task_dict

    return task_metrics


@endpoint("reports.get_task_data")
def get_task_data(call: APICall, company_id, request: GetTasksDataRequest):
    if request.model_events:
        entity_cls = Model
        conform_data = conform_model_data
    else:
        entity_cls = Task
        conform_data = conform_task_data

    call_data = escape_execution_parameters(call.data)
    process_include_subprojects(call_data)

    ret_params = {}
    tasks = entity_cls.get_many_with_join(
        company=company_id,
        query_dict=call_data,
        query=_hidden_query(call_data),
        allow_public=request.allow_public,
        ret_params=ret_params,
    )
    conform_data(call, tasks)
    res = {"tasks": tasks, **ret_params}
    if not (
        request.debug_images
        or request.plots
        or request.scalar_metrics_iter_histogram
        or request.single_value_metrics
    ):
        return res

    task_ids = [task["id"] for task in tasks]
    companies = _get_task_or_model_index_companies(
        company_id, task_ids=task_ids, model_events=request.model_events
    )
    if request.debug_images:
        result = event_bll.debug_images_iterator.get_task_events(
            companies={
                t.id: t.company for t in chain.from_iterable(companies.values())
            },
            task_metrics=_get_task_metrics_from_request(task_ids, request.debug_images),
            iter_count=request.debug_images.iters,
        )
        res["debug_images"] = [
            r.to_struct() for r in _get_metrics_response(result.metric_events)
        ]

    if request.plots:
        res["plots"] = _get_multitask_plots(
            companies=companies,
            last_iters=request.plots.iters,
            request_metrics=request.plots.metrics,
            last_iters_per_task_metric=request.plots.last_iters_per_task_metric,
        )[0]

    if request.scalar_metrics_iter_histogram:
        res[
            "scalar_metrics_iter_histogram"
        ] = event_bll.metrics.compare_scalar_metrics_average_per_iter(
            companies=companies,
            samples=request.scalar_metrics_iter_histogram.samples,
            key=request.scalar_metrics_iter_histogram.key,
            metric_variants=_get_metric_variants_from_request(
                request.scalar_metrics_iter_histogram.metrics
            ),
            model_events=request.model_events,
        )

    if request.single_value_metrics:
        res["single_value_metrics"] = _get_single_value_metrics_response(
            companies=companies,
            value_metrics=event_bll.metrics.get_task_single_value_metrics(
                companies=companies
            ),
        )

    call.result.data = res


@endpoint("reports.move")
def move(call: APICall, company_id: str, request: MoveReportRequest):
    if not ("project" in call.data or request.project_name):
        raise errors.bad_request.MissingRequiredFields(
            "project or project_name is required"
        )

    task = _assert_report(
        company_id=company_id,
        task_id=request.task,
        identity=call.identity,
        only_fields=("project",),
    )
    user_id = call.identity.user
    project_name = request.project_name
    if not project_name:
        if request.project:
            project = Project.get_for_writing(
                company=company_id, id=request.project, _only=("name",)
            )
            project_name = project.name
        else:
            project_name = ""

    project_id = _ensure_reports_project(
        company=company_id, user=user_id, name=project_name
    )

    project_bll.move_under_project(
        entity_cls=Task,
        user=call.identity.user,
        company=company_id,
        ids=[request.task],
        project=project_id,
    )

    _delete_reports_project_if_empty(task.project)

    return {"project_id": project_id}


@endpoint(
    "reports.publish",
    response_data_model=UpdateResponse,
)
def publish(call: APICall, company_id, request: PublishReportRequest):
    task = _assert_report(
        company_id=company_id, task_id=request.task, identity=call.identity
    )
    updates = ChangeStatusRequest(
        task=task,
        new_status=TaskStatus.published,
        force=True,
        status_reason="",
        status_message=request.message,
        user_id=call.identity.user,
    ).execute(published=datetime.utcnow())

    call.result.data_model = UpdateResponse(**updates)


@endpoint("reports.archive")
def archive(call: APICall, company_id, request: ArchiveReportRequest):
    task = _assert_report(
        company_id=company_id, task_id=request.task, identity=call.identity
    )
    archived = task.update(
        status_message=request.message,
        status_reason="",
        add_to_set__system_tags=EntityVisibility.archived.value,
        last_change=datetime.utcnow(),
        last_changed_by=call.identity.user,
    )

    return {"archived": archived}


@endpoint("reports.unarchive")
def unarchive(call: APICall, company_id, request: ArchiveReportRequest):
    task = _assert_report(
        company_id=company_id, task_id=request.task, identity=call.identity
    )
    unarchived = task.update(
        status_message=request.message,
        status_reason="",
        pull__system_tags=EntityVisibility.archived.value,
        last_change=datetime.utcnow(),
        last_changed_by=call.identity.user,
    )
    return {"unarchived": unarchived}


# @endpoint("reports.share")
# def share(call: APICall, company_id, request: ShareReportRequest):
#     _assert_report(
#         company_id=company_id, user_id=call.identity.user, task_id=request.task
#     )
#     call.result.data = {
#         "changed": task_bll.share_task(
#             company_id=company_id, task_ids=[request.task], share=request.share
#         )
#     }


@endpoint("reports.delete")
def delete(call: APICall, company_id, request: DeleteReportRequest):
    task = _assert_report(
        company_id=company_id,
        task_id=request.task,
        identity=call.identity,
        only_fields=("project",),
    )
    if (
        task.status != TaskStatus.created
        and EntityVisibility.archived.value not in task.system_tags
        and not request.force
    ):
        raise errors.bad_request.TaskCannotBeDeleted(
            "due to status, use force=True",
            task=task.id,
            expected=TaskStatus.created,
            current=task.status,
        )

    task.delete()
    _delete_reports_project_if_empty(task.project)

    call.result.data = {"deleted": 1}


@endpoint("reports.get_tags")
def get_tags(call: APICall, company_id: str, _):
    tags = Task.objects(company=company_id, type=TaskType.report).distinct(field="tags")
    call.result.data = sort_tags_response({"tags": tags})
