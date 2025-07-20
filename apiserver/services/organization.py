import csv
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from operator import itemgetter
from typing import Mapping, Type, Sequence, Optional, Callable, Hashable

from flask import stream_with_context
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apimodels.organization import (
    TagsRequest,
    EntitiesCountRequest,
    DownloadForGetAllRequest,
    EntityType,
    PrepareDownloadForGetAllRequest,
)
from apiserver.bll.model import Metadata
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project import ProjectBLL
from apiserver.config_repo import config
from apiserver.database.model import User, AttributedDocument, EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskType
from apiserver.redis_manager import redman
from apiserver.service_repo import endpoint, APICall
from apiserver.services.models import conform_model_data
from apiserver.services.tasks import (
    escape_execution_parameters,
    _hidden_query,
    conform_task_data,
)
from apiserver.services.utils import get_tags_filter_dictionary, sort_tags_response
from apiserver.utilities import json
from apiserver.utilities.dicts import nested_get

org_bll = OrgBLL()
project_bll = ProjectBLL()
redis = redman.connection("apiserver")
conf = config.get("services.organization")


@endpoint("organization.get_tags", request_data_model=TagsRequest)
def get_tags(call: APICall, company, request: TagsRequest):
    filter_dict = get_tags_filter_dictionary(request.filter)
    ret = defaultdict(set)
    for entity in Tags.Model, Tags.Task:
        tags = org_bll.get_tags(
            company,
            entity,
            include_system=request.include_system,
            filter_=filter_dict,
        )
        for field, vals in tags.items():
            ret[field] |= vals

    call.result.data = sort_tags_response(ret)


@endpoint("organization.get_user_companies")
def get_user_companies(call: APICall, company_id: str, _):
    users = [
        {"id": u.id, "name": u.name, "avatar": u.avatar}
        for u in User.objects(company=company_id).only("avatar", "name", "company")
    ]

    call.result.data = {
        "companies": [
            {
                "id": company_id,
                "name": call.identity.company_name,
                "allocated": len(users),
                "owners": sorted(users, key=itemgetter("name")),
            }
        ]
    }


@endpoint("organization.get_entities_count")
def get_entities_count(call: APICall, company, request: EntitiesCountRequest):
    entity_classes: Mapping[str, Type[AttributedDocument]] = {
        "projects": Project,
        "tasks": Task,
        "models": Model,
        "pipelines": Project,
        "datasets": Project,
        "reports": Task,
    }
    ret = {}
    for field, entity_cls in entity_classes.items():
        data = call.data.get(field)
        if data is None:
            continue

        if field == "reports":
            data["type"] = TaskType.report
            data["include_subprojects"] = True

        if request.active_users:
            if entity_cls is Project:
                requested_ids = data.get("id")
                if isinstance(requested_ids, str):
                    requested_ids = [requested_ids]
                ids, _ = project_bll.get_projects_with_selected_children(
                    company=company,
                    users=request.active_users,
                    project_ids=requested_ids,
                    allow_public=request.allow_public,
                )
                if not ids:
                    ret[field] = 0
                    continue
                data["id"] = ids
            elif not data.get("user"):
                data["user"] = request.active_users

        query = Q()
        if (
            entity_cls in (Project, Task)
            and field not in ("reports", "pipelines", "datasets")
            and not request.search_hidden
        ):
            query &= Q(system_tags__ne=EntityVisibility.hidden.value)

        if not request.limit:
            ret[field] = entity_cls.get_count(
                company=company,
                query_dict=data,
                query=query,
                allow_public=request.allow_public,
            )
        else:
            query = entity_cls.get_combined_query(
                company=company,
                query_dict=data,
                query=query,
                allow_public=request.allow_public,
            )
            ids = entity_cls.objects(query).limit(request.limit).scalar("id")
            ret[field] = len(ids)

    call.result.data = ret


def _get_download_getter_fn(
    company: str,
    call: APICall,
    call_data: dict,
    allow_public: bool,
    entity_type: EntityType,
) -> Optional[Callable[[int, int], Sequence[dict]]]:
    def get_task_data() -> Sequence[dict]:
        tasks = Task.get_many_with_join(
            company=company,
            query_dict=call_data,
            query=_hidden_query(call_data),
            allow_public=allow_public,
        )
        conform_task_data(call, tasks)
        return tasks

    def get_model_data() -> Sequence[dict]:
        models = Model.get_many_with_join(
            company=company,
            query_dict=call_data,
            allow_public=allow_public,
        )
        conform_model_data(call, models)
        return models

    if entity_type == EntityType.task:
        call_data = escape_execution_parameters(call_data)
        get_fn = get_task_data
    elif entity_type == EntityType.model:
        call_data = Metadata.escape_query_parameters(call_data)
        get_fn = get_model_data
    else:
        raise errors.bad_request.ValidationError(
            f"Unsupported entity type: {str(entity_type)}"
        )

    def getter(page: int, page_size: int) -> Sequence[dict]:
        call_data.pop("scroll_id", None)
        call_data.pop("start", None)
        call_data.pop("size", None)
        call_data.pop("refresh_scroll", None)
        call_data["page"] = page
        call_data["page_size"] = page_size
        return get_fn()

    return getter


@endpoint("organization.prepare_download_for_get_all")
def prepare_download_for_get_all(
    call: APICall, company: str, request: PrepareDownloadForGetAllRequest
):
    # validate input params
    field_names = set()
    for fm in request.field_mappings:
        name = fm.name or fm.field
        if name in field_names:
            raise errors.bad_request.ValidationError(
                f"Field_name appears more than once in field_mappings: {str(name)}"
            )
        field_names.add(name)
        if fm.values:
            value_keys = set()
            for v in fm.values:
                if v.key in value_keys:
                    raise errors.bad_request.ValidationError(
                        f"Value key appears more than once in field_mappings: {str(v.key)}"
                    )
                value_keys.add(v.key)

    getter = _get_download_getter_fn(
        company,
        call,
        call_data=call.data.copy(),
        allow_public=request.allow_public,
        entity_type=request.entity_type,
    )
    # retrieve one element just to make sure that there are no issues with the call parameters
    if getter:
        getter(0, 1)

    redis.setex(
        f"get_all_download_{call.id}",
        int(conf.get("download.redis_timeout_sec", 300)),
        json.dumps(call.data),
    )

    call.result.data = dict(prepare_id=call.id)


_dangerous_chars = ("=", "+", "-", "@", "\t", "\r")

def _sanitize_csv(value: str) -> str:
    """
    Prevent csv injection:
    If the string starts with any of the chars that Excel
    interpret as a special char then prepend it with a single quote
    """
    if value and value.startswith(_dangerous_chars):
        return f"'{value}"
    return value

@endpoint("organization.download_for_get_all")
def download_for_get_all(call: APICall, company, request: DownloadForGetAllRequest):
    request_data = redis.get(f"get_all_download_{request.prepare_id}")
    if not request_data:
        raise errors.bad_request.InvalidId(
            f"prepare ID not found", prepare_id=request.prepare_id
        )

    try:
        call_data = json.loads(request_data)
        request = PrepareDownloadForGetAllRequest(**call_data)
    except Exception as ex:
        raise errors.server_error.DataError("failed parsing prepared data", ex=ex)

    class SingleLine:
        @staticmethod
        def write(line: str) -> str:
            return line

    def generate():
        field_mappings = {
            mapping.get("name", mapping["field"]): {
                "field_path": mapping["field"].split("."),
                "values": {
                    v.get("key"): v.get("value")
                    for v in (mapping.get("values") or [])
                },
            }
            for mapping in call_data.get("field_mappings", [])
        }
        get_fn = _get_download_getter_fn(
            company,
            call,
            call_data=call_data,
            allow_public=request.allow_public,
            entity_type=request.entity_type,
        )
        if not get_fn:
            yield csv.writer(SingleLine()).writerow(field_mappings)
            return

        def get_entity_field_as_str(
            data: dict, field_path: Sequence[str], values: Mapping
        ) -> str:
            val = nested_get(data, field_path, "")
            if isinstance(val, dict):
                val = val.get("id", "")
            if values and isinstance(val, Hashable):
                val = values.get(val, val)

            return _sanitize_csv(str(val))

        def get_projected_fields(data: dict) -> Sequence[str]:
            return [
                get_entity_field_as_str(
                    data, field_path=m["field_path"], values=m["values"]
                )
                for m in field_mappings.values()
            ]

        with ThreadPoolExecutor(1) as pool:
            page = 0
            page_size = int(conf.get("download.batch_size", 500))
            items_left = int(conf.get("download.max_download_items", 1000))
            future = pool.submit(get_fn, page, min(page_size, items_left))
            while items_left > 0:
                result = future.result()
                if not result:
                    break

                items_left -= len(result)
                page += 1
                if items_left > 0:
                    future = pool.submit(get_fn, page, min(page_size, items_left))

                with StringIO() as fp:
                    writer = csv.writer(fp, quoting=csv.QUOTE_NONNUMERIC)
                    if page == 1:
                        fp.write("\ufeff")  # utf-8 signature
                        writer.writerow(field_mappings)
                    writer.writerows(get_projected_fields(r) for r in result)
                    yield fp.getvalue()

        if page == 0:
            yield csv.writer(SingleLine()).writerow(field_mappings)

    def get_project_name() -> Optional[str]:
        projects = call_data.get("project")
        if not projects or not isinstance(projects, (list, str)):
            return
        if isinstance(projects, list):
            if len(projects) > 1:
                return
            projects = projects[0]
            if projects is None:
                return "root"
        project: Project = Project.objects(id=projects).only("basename").first()
        if not project:
            return

        return project.basename[: conf.get("download.max_project_name_length", 60)]

    call.result.filename = "-".join(
        filter(None, ("clearml", get_project_name(), f"{request.entity_type}s.csv"))
    )
    call.result.content_type = "text/csv"
    call.result.raw_data = stream_with_context(generate())
