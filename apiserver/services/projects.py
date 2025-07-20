from typing import Sequence, Optional, Tuple

import attr
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apierrors.errors.bad_request import InvalidProjectId
from apiserver.apimodels.base import UpdateResponse, MakePublicRequest, IdResponse
from apiserver.apimodels.projects import (
    DeleteRequest,
    GetParamsRequest,
    ProjectTagsRequest,
    ProjectTaskParentsRequest,
    ProjectHyperparamValuesRequest,
    ProjectsGetRequest,
    MoveRequest,
    MergeRequest,
    ProjectRequest,
    ProjectModelMetadataValuesRequest,
    ProjectChildrenType,
    GetUniqueMetricsRequest,
    ProjectUserNamesRequest,
    EntityTypeEnum,
)
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project import ProjectBLL, ProjectQueries
from apiserver.bll.project.project_bll import pipeline_tag, reports_tag
from apiserver.bll.project.project_cleanup import (
    delete_project,
    validate_project_delete,
)
from apiserver.database.errors import translate_errors_context
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import TaskType, Task
from apiserver.database.utils import (
    parse_from_call,
    get_company_or_none_constraint,
)
from apiserver.service_repo import APICall, endpoint
from apiserver.services.utils import (
    conform_tag_fields,
    conform_output_tags,
    get_tags_filter_dictionary,
    sort_tags_response,
)

org_bll = OrgBLL()
project_bll = ProjectBLL()
project_queries = ProjectQueries()

create_fields = {
    "name": None,
    "description": None,
    "tags": list,
    "system_tags": list,
    "default_output_destination": None,
}


@endpoint("projects.get_by_id")
def get_by_id(call: APICall, company: str, request: ProjectRequest):
    project_id = request.project

    with translate_errors_context():
        query = Q(id=project_id) & get_company_or_none_constraint(company)
        project = Project.objects(query).first()
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        project_dict = project.to_proper_dict()
        conform_output_tags(call, project_dict)

        call.result.data = {"project": project_dict}


def _hidden_query(search_hidden: bool, ids: Sequence) -> Q:
    """
    1. Add only non-hidden tasks search condition (unless specifically specified differently)
    """
    if search_hidden or ids:
        return Q()

    return Q(system_tags__ne=EntityVisibility.hidden.value)


def _adjust_search_parameters(data: dict, shallow_search: bool):
    """
    1. Make sure that there is no external query on path
    2. If not shallow_search and parent is provided then parent can be at any place in path
    3. If shallow_search and no parent provided then use a top level parent
    """
    data.pop("path", None)
    if not shallow_search:
        if "parent" in data:
            data["path"] = data.pop("parent")
        return

    if "parent" not in data:
        data["parent"] = [None]


def _get_project_stats_filter(
    request: ProjectsGetRequest,
) -> Tuple[Optional[dict], bool]:
    if request.include_stats_filter or not request.children_type:
        return request.include_stats_filter, request.search_hidden

    if request.children_tags_filter:
        stats_filter = {"tags": request.children_tags_filter}
    elif request.children_tags:
        stats_filter = {"tags": request.children_tags}
    else:
        stats_filter = {}

    if request.children_type == ProjectChildrenType.pipeline:
        return (
            {
                **stats_filter,
                "system_tags": [pipeline_tag],
                "type": [TaskType.controller],
            },
            True,
        )
    if request.children_type == ProjectChildrenType.report:
        return (
            {**stats_filter, "system_tags": [reports_tag], "type": [TaskType.report]},
            True,
        )
    return stats_filter, request.search_hidden


@endpoint("projects.get_all_ex")
def get_all_ex(call: APICall, company_id: str, request: ProjectsGetRequest):
    data = call.data
    conform_tag_fields(call, data)
    allow_public = (
        data["allow_public"]
        if "allow_public" in data
        else not data["non_public"]
        if "non_public" in data
        else request.allow_public
    )

    requested_ids = data.get("id")
    if isinstance(requested_ids, str):
        requested_ids = [requested_ids]

    _adjust_search_parameters(
        data,
        shallow_search=request.shallow_search,
    )
    selected_project_ids = None
    if request.active_users or request.children_type:
        ids, selected_project_ids = project_bll.get_projects_with_selected_children(
            company=company_id,
            users=request.active_users,
            project_ids=requested_ids,
            allow_public=allow_public,
            children_type=request.children_type,
            children_tags=request.children_tags,
            children_tags_filter=request.children_tags_filter,
        )
        if not ids:
            return {"projects": []}
        data["id"] = ids

    ret_params = {}

    remove_system_tags = False
    if request.search_hidden:
        only_fields = data.get("only_fields")
        if isinstance(only_fields, list) and "system_tags" not in only_fields:
            only_fields.append("system_tags")
            remove_system_tags = True

    projects: Sequence[dict] = Project.get_many_with_join(
        company=company_id,
        query_dict=data,
        query=_hidden_query(search_hidden=request.search_hidden, ids=requested_ids),
        allow_public=allow_public,
        ret_params=ret_params,
    )
    if not projects:
        return {"projects": projects, **ret_params}

    if request.search_hidden:
        for p in projects:
            system_tags = (
                p.pop("system_tags", [])
                if remove_system_tags
                else p.get("system_tags", [])
            )
            if EntityVisibility.hidden.value in system_tags:
                p["hidden"] = True

    conform_output_tags(call, projects)
    project_ids = list({project["id"] for project in projects})

    stats_filter, stats_search_hidden = _get_project_stats_filter(request)
    if request.check_own_contents:
        if request.children_type == ProjectChildrenType.dataset:
            contents = project_bll.calc_own_datasets(
                company=company_id,
                project_ids=project_ids,
                filter_=stats_filter,
                users=request.active_users,
            )
        else:
            contents = project_bll.calc_own_contents(
                company=company_id,
                project_ids=project_ids,
                filter_=stats_filter,
                users=request.active_users,
            )

        for project in projects:
            project.update(**contents.get(project["id"], {}))

    if request.include_stats:
        if request.children_type == ProjectChildrenType.dataset:
            stats, children = project_bll.get_project_dataset_stats(
                company=company_id,
                project_ids=project_ids,
                include_children=request.stats_with_children,
                filter_=stats_filter,
                users=request.active_users,
                selected_project_ids=selected_project_ids,
            )
        else:
            stats, children = project_bll.get_project_stats(
                company=company_id,
                project_ids=project_ids,
                include_children=request.stats_with_children,
                search_hidden=stats_search_hidden,
                filter_=stats_filter,
                users=request.active_users,
                selected_project_ids=selected_project_ids,
            )

        for project in projects:
            project["stats"] = stats[project["id"]]
            project["sub_projects"] = children[project["id"]]

    if request.include_dataset_stats:
        dataset_stats = project_bll.get_dataset_stats(
            company=company_id,
            project_ids=project_ids,
            users=request.active_users,
        )
        for project in projects:
            project["dataset_stats"] = dataset_stats.get(project["id"])

    call.result.data = {"projects": projects, **ret_params}


@endpoint("projects.get_all")
def get_all(call: APICall, company: str, _):
    data = call.data
    conform_tag_fields(call, data)
    _adjust_search_parameters(
        data,
        shallow_search=data.get("shallow_search", False),
    )
    ret_params = {}
    projects = Project.get_many(
        company=company,
        query_dict=data,
        query=_hidden_query(
            search_hidden=data.get("search_hidden"), ids=data.get("id")
        ),
        parameters=data,
        allow_public=True,
        ret_params=ret_params,
    )
    conform_output_tags(call, projects)
    call.result.data = {"projects": projects, **ret_params}


@endpoint(
    "projects.create",
    required_fields=["name"],
    response_data_model=IdResponse,
)
def create(call: APICall, company: str, _):
    identity = call.identity

    with translate_errors_context():
        fields = parse_from_call(call.data, create_fields, Project.get_fields())
        conform_tag_fields(call, fields, validate=True)

        return IdResponse(
            id=ProjectBLL.create(
                user=identity.user,
                company=company,
                **fields,
            )
        )


@endpoint("projects.update", response_data_model=UpdateResponse)
def update(call: APICall, company: str, request: ProjectRequest):
    """
    update

    :summary: Update project information.
              See `project.create` for parameters.
    :return: updated - `int` - number of projects updated
             fields - `[string]` - updated fields
    """
    fields = parse_from_call(
        call.data, create_fields, Project.get_fields(), discard_none_values=False
    )
    conform_tag_fields(call, fields, validate=True)
    updated = ProjectBLL.update(company=company, project_id=request.project, **fields)
    conform_output_tags(call, fields)
    call.result.data_model = UpdateResponse(updated=updated, fields=fields)


def _reset_cached_tags(company: str, projects: Sequence[str]):
    org_bll.reset_tags(company, Tags.Task, projects=projects)
    org_bll.reset_tags(company, Tags.Model, projects=projects)


@endpoint("projects.move", request_data_model=MoveRequest)
def move(call: APICall, company: str, request: MoveRequest):
    moved, affected_projects = ProjectBLL.move_project(
        company=company,
        user=call.identity.user,
        project_id=request.project,
        new_location=request.new_location,
    )
    _reset_cached_tags(company, projects=list(affected_projects))

    call.result.data = {"moved": moved}


@endpoint("projects.merge", request_data_model=MergeRequest)
def merge(call: APICall, company: str, request: MergeRequest):
    moved_entitites, moved_projects, affected_projects = ProjectBLL.merge_project(
        company, source_id=request.project, destination_id=request.destination_project
    )

    _reset_cached_tags(company, projects=list(affected_projects))

    call.result.data = {
        "moved_entities": moved_entitites,
        "moved_projects": moved_projects,
    }


@endpoint("projects.validate_delete")
def validate_delete(call: APICall, company_id: str, request: ProjectRequest):
    call.result.data = validate_project_delete(
        company=company_id, project_id=request.project
    )


@endpoint("projects.delete", request_data_model=DeleteRequest)
def delete(call: APICall, company_id: str, request: DeleteRequest):
    res, affected_projects = delete_project(
        company=company_id,
        user=call.identity.user,
        project_id=request.project,
        force=request.force,
        delete_contents=request.delete_contents,
        delete_external_artifacts=request.delete_external_artifacts,
    )
    _reset_cached_tags(company_id, projects=list(affected_projects))
    # noinspection PyTypeChecker
    call.result.data = {**attr.asdict(res)}


@endpoint(
    "projects.get_unique_metric_variants", request_data_model=GetUniqueMetricsRequest
)
def get_unique_metric_variants(
    call: APICall, company_id: str, request: GetUniqueMetricsRequest
):
    metrics = project_queries.get_unique_metric_variants(
        company_id,
        [request.project] if request.project else None,
        include_subprojects=request.include_subprojects,
        ids=request.ids,
        model_metrics=request.model_metrics,
    )

    call.result.data = {"metrics": metrics}


@endpoint("projects.get_model_metadata_keys")
def get_model_metadata_keys(call: APICall, company_id: str, request: GetParamsRequest):
    total, remaining, keys = project_queries.get_model_metadata_keys(
        company_id,
        project_ids=[request.project] if request.project else None,
        include_subprojects=request.include_subprojects,
        page=request.page,
        page_size=request.page_size,
    )

    call.result.data = {
        "total": total,
        "remaining": remaining,
        "keys": keys,
    }


@endpoint("projects.get_model_metadata_values")
def get_model_metadata_values(
    call: APICall, company_id: str, request: ProjectModelMetadataValuesRequest
):
    total, values = project_queries.get_model_metadata_distinct_values(
        company_id,
        project_ids=request.projects,
        key=request.key,
        include_subprojects=request.include_subprojects,
        allow_public=request.allow_public,
        page=request.page,
        page_size=request.page_size,
    )
    call.result.data = {
        "total": total,
        "values": values,
    }


@endpoint(
    "projects.get_hyper_parameters",
    min_version="2.9",
    request_data_model=GetParamsRequest,
)
def get_hyper_parameters(call: APICall, company_id: str, request: GetParamsRequest):
    total, remaining, parameters = project_queries.get_aggregated_project_parameters(
        company_id,
        project_ids=[request.project] if request.project else None,
        include_subprojects=request.include_subprojects,
        page=request.page,
        page_size=request.page_size,
    )

    call.result.data = {
        "total": total,
        "remaining": remaining,
        "parameters": parameters,
    }


@endpoint(
    "projects.get_hyperparam_values",
    min_version="2.13",
    request_data_model=ProjectHyperparamValuesRequest,
)
def get_hyperparam_values(
    call: APICall, company_id: str, request: ProjectHyperparamValuesRequest
):
    total, values = project_queries.get_task_hyperparam_distinct_values(
        company_id,
        project_ids=request.projects,
        section=request.section,
        name=request.name,
        include_subprojects=request.include_subprojects,
        allow_public=request.allow_public,
        pattern=request.pattern,
        page=request.page,
        page_size=request.page_size,
    )
    call.result.data = {
        "total": total,
        "values": values,
    }


@endpoint("projects.get_project_tags")
def get_tags(call: APICall, company, request: ProjectTagsRequest):
    tags, system_tags = project_bll.get_project_tags(
        company,
        include_system=request.include_system,
        filter_=get_tags_filter_dictionary(request.filter),
        projects=request.projects,
    )
    call.result.data = sort_tags_response({"tags": tags, "system_tags": system_tags})


@endpoint(
    "projects.get_task_tags", min_version="2.8", request_data_model=ProjectTagsRequest
)
def get_tags(call: APICall, company, request: ProjectTagsRequest):
    ret = org_bll.get_tags(
        company,
        Tags.Task,
        include_system=request.include_system,
        filter_=get_tags_filter_dictionary(request.filter),
        projects=request.projects,
    )
    call.result.data = sort_tags_response(ret)


@endpoint(
    "projects.get_model_tags", min_version="2.8", request_data_model=ProjectTagsRequest
)
def get_tags(call: APICall, company, request: ProjectTagsRequest):
    ret = org_bll.get_tags(
        company,
        Tags.Model,
        include_system=request.include_system,
        filter_=get_tags_filter_dictionary(request.filter),
        projects=request.projects,
    )
    call.result.data = sort_tags_response(ret)


@endpoint(
    "projects.make_public", min_version="2.9", request_data_model=MakePublicRequest
)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Project.set_public(
        company_id=company_id,
        user_id=call.identity.user,
        ids=request.ids,
        invalid_cls=InvalidProjectId,
        enabled=True,
    )


@endpoint(
    "projects.make_private", min_version="2.9", request_data_model=MakePublicRequest
)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Project.set_public(
        company_id=company_id,
        user_id=call.identity.user,
        ids=request.ids,
        invalid_cls=InvalidProjectId,
        enabled=False,
    )


@endpoint(
    "projects.get_task_parents",
    min_version="2.12",
    request_data_model=ProjectTaskParentsRequest,
)
def get_task_parents(
    call: APICall, company_id: str, request: ProjectTaskParentsRequest
):
    call.result.data = {
        "parents": ProjectBLL.get_task_parents(
            company_id,
            projects=request.projects,
            include_subprojects=request.include_subprojects,
            state=request.tasks_state,
            name=request.task_name,
        )
    }


@endpoint("projects.get_user_names")
def get_user_names(call: APICall, company_id: str, request: ProjectUserNamesRequest):
    call.result.data = {
        "users": ProjectBLL.get_entity_users(
            company_id,
            entity_cls=Model if request.entity == EntityTypeEnum.model else Task,
            projects=request.projects,
            include_subprojects=request.include_subprojects,
        )
    }
