from datetime import datetime
from functools import partial
from typing import Sequence, Union

from mongoengine import Q, EmbeddedDocument

from apiserver import database
from apiserver.apierrors import errors
from apiserver.apierrors.errors.bad_request import InvalidModelId
from apiserver.apimodels.base import UpdateResponse, MakePublicRequest, MoveRequest
from apiserver.apimodels.batch import BatchResponse, BatchRequest
from apiserver.apimodels.models import (
    CreateModelRequest,
    CreateModelResponse,
    PublishModelRequest,
    PublishModelResponse,
    GetFrameworksRequest,
    DeleteModelRequest,
    DeleteMetadataRequest,
    AddOrUpdateMetadataRequest,
    ModelsPublishManyRequest,
    ModelsDeleteManyRequest,
    ModelsGetRequest,
    ModelRequest,
    TaskRequest,
    UpdateForTaskRequest,
    UpdateModelRequest,
)
from apiserver.apimodels.tasks import UpdateTagsRequest
from apiserver.bll.event import EventBLL
from apiserver.bll.model import ModelBLL, Metadata
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project import ProjectBLL
from apiserver.bll.task import TaskBLL
from apiserver.bll.task.task_cleanup import (
    schedule_for_delete,
    delete_task_events_and_collect_urls,
)
from apiserver.bll.task.task_operations import publish_task
from apiserver.bll.task.utils import get_task_with_write_access, deleted_prefix
from apiserver.bll.util import run_batch_operation
from apiserver.config_repo import config
from apiserver.database.model import validate_id
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import (
    Task,
    TaskStatus,
    ModelItem,
    TaskModelNames,
    TaskModelTypes,
)
from apiserver.database.utils import (
    parse_from_call,
    get_company_or_none_constraint,
    filter_fields,
)
from apiserver.service_repo import APICall, endpoint
from apiserver.service_repo.auth import Identity
from apiserver.services.utils import (
    conform_tag_fields,
    conform_output_tags,
    ModelsBackwardsCompatibility,
    unescape_metadata,
    escape_metadata,
    process_include_subprojects,
)

log = config.logger(__file__)
org_bll = OrgBLL()
project_bll = ProjectBLL()
event_bll = EventBLL()


def conform_model_data(call: APICall, model_data: Union[Sequence[dict], dict]):
    conform_output_tags(call, model_data)
    unescape_metadata(call, model_data)


@endpoint("models.get_by_id")
def get_by_id(call: APICall, company_id, request: ModelRequest):
    model_id = request.model
    call_data = Metadata.escape_query_parameters(call.data)
    models = Model.get_many(
        company=company_id,
        query_dict=call_data,
        query=Q(id=model_id),
        allow_public=True,
    )
    if not models:
        raise errors.bad_request.InvalidModelId(
            "no such public or company model",
            id=model_id,
            company=company_id,
        )
    conform_model_data(call, models[0])
    call.result.data = {"model": models[0]}


@endpoint("models.get_by_task_id")
def get_by_task_id(call: APICall, company_id, request: TaskRequest):
    if call.requested_endpoint_version > ModelsBackwardsCompatibility.max_version:
        raise errors.moved_permanently.NotSupported("use models.get_by_id/get_all apis")

    task_id = request.task

    query = dict(id=task_id, company=company_id)
    task = Task.get(_only=["models"], **query)
    if not task:
        raise errors.bad_request.InvalidTaskId(**query)
    if not task.models or not task.models.output:
        raise errors.bad_request.MissingTaskFields(field="models.output")

    model_id = task.models.output[-1].model
    model = Model.objects(
        Q(id=model_id) & get_company_or_none_constraint(company_id)
    ).first()
    if not model:
        raise errors.bad_request.InvalidModelId(
            "no such public or company model",
            id=model_id,
            company=company_id,
        )
    model_dict = model.to_proper_dict()
    conform_model_data(call, model_dict)
    call.result.data = {"model": model_dict}


@endpoint("models.get_all_ex", request_data_model=ModelsGetRequest)
def get_all_ex(call: APICall, company_id, request: ModelsGetRequest):
    conform_tag_fields(call, call.data)
    call_data = Metadata.escape_query_parameters(call.data)
    process_include_subprojects(call_data)
    ret_params = {}
    models = Model.get_many_with_join(
        company=company_id,
        query_dict=call_data,
        allow_public=request.allow_public,
        ret_params=ret_params,
    )
    conform_model_data(call, models)

    if not request.include_stats:
        call.result.data = {"models": models, **ret_params}
        return

    model_ids = {model["id"] for model in models}
    stats = ModelBLL.get_model_stats(
        company=company_id,
        model_ids=list(model_ids),
    )

    for model in models:
        model["stats"] = stats.get(model["id"])

    call.result.data = {"models": models, **ret_params}


@endpoint("models.get_by_id_ex", required_fields=["id"])
def get_by_id_ex(call: APICall, company_id, _):
    conform_tag_fields(call, call.data)
    call_data = Metadata.escape_query_parameters(call.data)
    models = Model.get_many_with_join(
        company=company_id, query_dict=call_data, allow_public=True
    )
    conform_model_data(call, models)
    call.result.data = {"models": models}


@endpoint("models.get_all")
def get_all(call: APICall, company_id, _):
    conform_tag_fields(call, call.data)
    call_data = Metadata.escape_query_parameters(call.data)
    process_include_subprojects(call_data)
    ret_params = {}
    models = Model.get_many(
        company=company_id,
        parameters=call_data,
        query_dict=call_data,
        allow_public=True,
        ret_params=ret_params,
    )
    conform_model_data(call, models)
    call.result.data = {"models": models, **ret_params}


@endpoint("models.get_frameworks", request_data_model=GetFrameworksRequest)
def get_frameworks(call: APICall, company_id, request: GetFrameworksRequest):
    call.result.data = {
        "frameworks": sorted(
            filter(
                None,
                project_bll.get_model_frameworks(
                    company_id, project_ids=request.projects
                ),
            )
        )
    }


create_fields = {
    "name": None,
    "tags": list,
    "system_tags": list,
    "task": Task,
    "comment": None,
    "uri": None,
    "project": Project,
    "parent": Model,
    "framework": None,
    "design": dict,
    "labels": dict,
    "ready": None,
    "metadata": list,
}

last_update_fields = (
    "uri",
    "framework",
    "design",
    "labels",
    "ready",
    "metadata",
    "system_tags",
    "tags",
)


def parse_model_fields(call, valid_fields):
    task_id = call.data.get("task")
    if isinstance(task_id, str) and task_id.startswith(deleted_prefix):
        call.data.pop("task")
    fields = parse_from_call(call.data, valid_fields, Model.get_fields())
    conform_tag_fields(call, fields, validate=True)
    escape_metadata(fields)
    return fields


def _update_cached_tags(company: str, project: str, fields: dict):
    org_bll.update_tags(
        company,
        Tags.Model,
        projects=[project],
        tags=fields.get("tags"),
        system_tags=fields.get("system_tags"),
    )


def _reset_cached_tags(company: str, projects: Sequence[str]):
    org_bll.reset_tags(
        company,
        Tags.Model,
        projects=projects,
    )


@endpoint("models.update_for_task")
def update_for_task(call: APICall, company_id, request: UpdateForTaskRequest):
    if call.requested_endpoint_version > ModelsBackwardsCompatibility.max_version:
        raise errors.moved_permanently.NotSupported("use tasks.add_or_update_model")

    task_id = request.task
    uri = request.uri
    iteration = request.iteration
    override_model_id = request.override_model_id
    if not (uri or override_model_id) or (uri and override_model_id):
        raise errors.bad_request.MissingRequiredFields(
            "exactly one field is required", fields=("uri", "override_model_id")
        )

    query = dict(id=task_id, company=company_id)
    task = get_task_with_write_access(
        task_id=task_id,
        company_id=company_id,
        identity=call.identity,
        only=("models", "execution", "name", "status", "project"),
    )

    allowed_states = [TaskStatus.created, TaskStatus.in_progress]
    if task.status not in allowed_states:
        raise errors.bad_request.InvalidTaskStatus(
            f"model can only be updated for tasks in the {allowed_states} states",
            **query,
        )

    if override_model_id:
        model = ModelBLL.get_company_model_by_id(
            company_id=company_id, model_id=override_model_id
        )
    else:
        if "name" not in call.data:
            # use task name if name not provided
            call.data["name"] = task.name

        if "comment" not in call.data:
            call.data["comment"] = f"Created by task `{task.name}` ({task.id})"

        if task.models and task.models.output:
            # model exists, update
            model_id = task.models.output[-1].model
            res = _update_model(call, company_id, model_id=model_id).to_struct()
            res.update({"id": model_id, "created": False})
            call.result.data = res
            return

        # new model, create
        fields = parse_model_fields(call, create_fields)

        # create and save model
        now = datetime.utcnow()
        model = Model(
            id=database.utils.id(),
            created=now,
            last_update=now,
            last_change=now,
            last_changed_by=call.identity.user,
            user=call.identity.user,
            company=company_id,
            project=task.project,
            framework=task.execution.framework,
            parent=task.models.input[0].model
            if task.models and task.models.input
            else None,
            design=task.execution.model_desc,
            labels=task.execution.model_labels,
            ready=(task.status == TaskStatus.published),
            **fields,
        )
        model.save()
        _update_cached_tags(company_id, project=model.project, fields=fields)

    TaskBLL.update_statistics(
        task_id=task_id,
        company_id=company_id,
        user_id=call.identity.user,
        last_iteration_max=iteration,
        models__output=[
            ModelItem(
                model=model.id,
                name=TaskModelNames[TaskModelTypes.output],
                updated=datetime.utcnow(),
            )
        ],
    )

    call.result.data = {"id": model.id, "created": True}


@endpoint(
    "models.create",
    request_data_model=CreateModelRequest,
    response_data_model=CreateModelResponse,
)
def create(call: APICall, company_id, req_model: CreateModelRequest):
    if req_model.public:
        company_id = ""

    project = req_model.project
    if project:
        validate_id(Project, company=company_id, project=project)

    task = req_model.task
    req_data = req_model.to_struct()
    if task:
        validate_task(company_id, call.identity, req_data)

    fields = filter_fields(Model, req_data)
    conform_tag_fields(call, fields, validate=True)
    escape_metadata(fields)

    # create and save model
    now = datetime.utcnow()
    model = Model(
        id=database.utils.id(),
        user=call.identity.user,
        company=company_id,
        created=now,
        last_update=now,
        last_change=now,
        last_changed_by=call.identity.user,
        **fields,
    )
    model.save()
    _update_cached_tags(company_id, project=model.project, fields=fields)

    call.result.data_model = CreateModelResponse(id=model.id, created=True)


def prepare_update_fields(call, company_id, fields: dict):
    fields = fields.copy()
    if "uri" in fields:
        # clear UI cache if URI is provided (model updated)
        fields["ui_cache"] = fields.pop("ui_cache", {})
    if "task" in fields:
        validate_task(company_id, call.identity, fields)

    if "labels" in fields:
        labels = fields["labels"]

        def find_other_types(iterable, type_):
            res = [x for x in iterable if not isinstance(x, type_)]
            try:
                return set(res)
            except TypeError:
                # Un-hashable, probably
                return res

        invalid_keys = find_other_types(labels.keys(), str)
        if invalid_keys:
            raise errors.bad_request.ValidationError(
                "labels keys must be strings", keys=invalid_keys
            )

        invalid_values = find_other_types(labels.values(), int)
        if invalid_values:
            raise errors.bad_request.ValidationError(
                "labels values must be integers", values=invalid_values
            )

    conform_tag_fields(call, fields, validate=True)
    escape_metadata(fields)
    return fields


def validate_task(company_id: str, identity: Identity, fields: dict):
    task_id = fields["task"]
    get_task_with_write_access(
        task_id=task_id, company_id=company_id, identity=identity, only=("id",)
    )


@endpoint("models.edit", response_data_model=UpdateResponse)
def edit(call: APICall, company_id, request: UpdateModelRequest):
    model_id = request.model

    model = ModelBLL.get_company_model_by_id(company_id=company_id, model_id=model_id)

    fields = parse_model_fields(call, create_fields)
    fields = prepare_update_fields(call, company_id, fields)

    for key in fields:
        field = getattr(model, key, None)
        value = fields[key]
        if field and isinstance(value, dict) and isinstance(field, EmbeddedDocument):
            d = field.to_mongo(use_db_field=False).to_dict()
            d.update(value)
            fields[key] = d

    iteration = request.iteration
    task_id = model.task or fields.get("task")
    if task_id and iteration is not None:
        TaskBLL.update_statistics(
            task_id=task_id,
            company_id=company_id,
            user_id=call.identity.user,
            last_iteration_max=iteration,
        )

    if fields:
        now = datetime.utcnow()
        fields.update(
            last_change=now,
            last_changed_by=call.identity.user,
        )
        if any(uf in fields for uf in last_update_fields):
            fields.update(last_update=now)

        updated = model.update(upsert=False, **fields)
        if updated:
            new_project = fields.get("project", model.project)
            if new_project != model.project:
                _reset_cached_tags(company_id, projects=[new_project, model.project])
            else:
                _update_cached_tags(company_id, project=model.project, fields=fields)
        conform_model_data(call, fields)
        call.result.data_model = UpdateResponse(updated=updated, fields=fields)
    else:
        call.result.data_model = UpdateResponse(updated=0)


def _update_model(call: APICall, company_id, model_id):
    model = ModelBLL.get_company_model_by_id(company_id=company_id, model_id=model_id)
    data = prepare_update_fields(call, company_id, call.data)
    task_id = data.get("task")
    iteration = data.get("iteration")
    if task_id and iteration is not None:
        TaskBLL.update_statistics(
            task_id=task_id,
            company_id=company_id,
            user_id=call.identity.user,
            last_iteration_max=iteration,
        )

    now = datetime.utcnow()
    updated_count, updated_fields = Model.safe_update(
        company_id,
        model.id,
        data,
        injected_update=dict(
            last_change=now,
            last_changed_by=call.identity.user,
        ),
    )
    if updated_count:
        if any(uf in updated_fields for uf in last_update_fields):
            model.update(upsert=False, last_update=now)

        new_project = updated_fields.get("project", model.project)
        if new_project != model.project:
            _reset_cached_tags(company_id, projects=[new_project, model.project])
        else:
            _update_cached_tags(
                company_id, project=model.project, fields=updated_fields
            )
    conform_model_data(call, updated_fields)
    return UpdateResponse(updated=updated_count, fields=updated_fields)


@endpoint("models.update", response_data_model=UpdateResponse)
def update(call, company_id, request: UpdateModelRequest):
    call.result.data_model = _update_model(call, company_id, model_id=request.model)


@endpoint(
    "models.set_ready",
    request_data_model=PublishModelRequest,
    response_data_model=PublishModelResponse,
)
def set_ready(call: APICall, company_id: str, request: PublishModelRequest):
    updated, published_task = ModelBLL.publish_model(
        model_id=request.model,
        company_id=company_id,
        identity=call.identity,
        force_publish_task=request.force_publish_task,
        publish_task_func=publish_task if request.publish_task else None,
    )
    call.result.data_model = PublishModelResponse(
        updated=updated, published_task=published_task
    )


@endpoint(
    "models.publish_many",
    request_data_model=ModelsPublishManyRequest,
    response_data_model=BatchResponse,
)
def publish_many(call: APICall, company_id, request: ModelsPublishManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            ModelBLL.publish_model,
            company_id=company_id,
            identity=call.identity,
            force_publish_task=request.force_publish_task,
            publish_task_func=publish_task if request.publish_task else None,
        ),
        ids=request.ids,
    )

    call.result.data_model = BatchResponse(
        succeeded=[
            dict(
                id=_id,
                updated=bool(updated),
                published_task=published_task.to_struct() if published_task else None,
            )
            for _id, (updated, published_task) in results
        ],
        failed=failures,
    )


def _delete_model_events(
    company_id: str,
    user_id: str,
    models: Sequence[Model],
    delete_external_artifacts: bool,
    sync_delete: bool,
):
    if not models:
        return
    model_ids = [m.id for m in models]
    delete_external_artifacts = delete_external_artifacts and config.get(
        "services.async_urls_delete.enabled", True
    )
    if delete_external_artifacts:
        model_urls = {m.uri for m in models if m.uri}
        if model_urls:
            schedule_for_delete(
                task_id=model_ids[0],
                company=company_id,
                user=user_id,
                urls=model_urls,
                can_delete_folders=False,
            )

        event_urls = delete_task_events_and_collect_urls(
            company=company_id,
            task_ids=model_ids,
            model=True,
            wait_for_delete=sync_delete,
        )
        if event_urls:
            schedule_for_delete(
                task_id=model_ids[0],
                company=company_id,
                user=user_id,
                urls=event_urls,
                can_delete_folders=False,
            )

    event_bll.delete_task_events(
        company_id, model_ids, model=True, wait_for_delete=sync_delete
    )


@endpoint("models.delete", request_data_model=DeleteModelRequest)
def delete(call: APICall, company_id, request: DeleteModelRequest):
    user_id = call.identity.user
    del_count, model = ModelBLL.delete_model(
        model_id=request.model,
        company_id=company_id,
        user_id=user_id,
        force=request.force,
    )
    if del_count:
        _delete_model_events(
            company_id=company_id,
            user_id=user_id,
            models=[model],
            delete_external_artifacts=request.delete_external_artifacts,
            sync_delete=True,
        )
        _reset_cached_tags(
            company_id, projects=[model.project] if model.project else []
        )

    call.result.data = dict(deleted=bool(del_count), url=model.uri)


@endpoint(
    "models.delete_many",
    request_data_model=ModelsDeleteManyRequest,
    response_data_model=BatchResponse,
)
def delete_many(call: APICall, company_id, request: ModelsDeleteManyRequest):
    user_id = call.identity.user

    results, failures = run_batch_operation(
        func=partial(
            ModelBLL.delete_model,
            company_id=company_id,
            user_id=call.identity.user,
            force=request.force,
        ),
        ids=request.ids,
    )

    succeeded = []
    deleted_models = []
    for _id, (deleted, model) in results:
        succeeded.append(dict(id=_id, deleted=bool(deleted), url=model.uri))
        deleted_models.append(model)

    if deleted_models:
        _delete_model_events(
            company_id=company_id,
            user_id=user_id,
            models=deleted_models,
            delete_external_artifacts=request.delete_external_artifacts,
            sync_delete=False,
        )
        projects = set(model.project for model in deleted_models)
        _reset_cached_tags(company_id, projects=list(projects))

    call.result.data_model = BatchResponse(
        succeeded=succeeded,
        failed=failures,
    )


@endpoint(
    "models.archive_many",
    request_data_model=BatchRequest,
    response_data_model=BatchResponse,
)
def archive_many(call: APICall, company_id, request: BatchRequest):
    results, failures = run_batch_operation(
        func=partial(
            ModelBLL.archive_model, company_id=company_id, user_id=call.identity.user
        ),
        ids=request.ids,
    )
    call.result.data_model = BatchResponse(
        succeeded=[dict(id=_id, archived=bool(archived)) for _id, archived in results],
        failed=failures,
    )


@endpoint(
    "models.unarchive_many",
    request_data_model=BatchRequest,
    response_data_model=BatchResponse,
)
def unarchive_many(call: APICall, company_id, request: BatchRequest):
    results, failures = run_batch_operation(
        func=partial(
            ModelBLL.unarchive_model, company_id=company_id, user_id=call.identity.user
        ),
        ids=request.ids,
    )
    call.result.data_model = BatchResponse(
        succeeded=[
            dict(id=_id, unarchived=bool(unarchived)) for _id, unarchived in results
        ],
        failed=failures,
    )


@endpoint("models.make_public", min_version="2.9", request_data_model=MakePublicRequest)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Model.set_public(
        company_id=company_id,
        user_id=call.identity.user,
        ids=request.ids,
        invalid_cls=InvalidModelId,
        enabled=True,
    )


@endpoint(
    "models.make_private", min_version="2.9", request_data_model=MakePublicRequest
)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Model.set_public(
        company_id=company_id,
        user_id=call.identity.user,
        ids=request.ids,
        invalid_cls=InvalidModelId,
        enabled=False,
    )


@endpoint("models.move", request_data_model=MoveRequest)
def move(call: APICall, company_id: str, request: MoveRequest):
    if not ("project" in call.data or request.project_name):
        raise errors.bad_request.MissingRequiredFields(
            "project or project_name is required"
        )

    return {
        "project_id": project_bll.move_under_project(
            entity_cls=Model,
            user=call.identity.user,
            company=company_id,
            ids=request.ids,
            project=request.project,
            project_name=request.project_name,
        )
    }


@endpoint("models.update_tags")
def update_tags(call: APICall, company_id: str, request: UpdateTagsRequest):
    return {
        "updated": org_bll.edit_entity_tags(
            company_id=company_id,
            user_id=call.identity.user,
            entity_cls=Model,
            entity_ids=request.ids,
            add_tags=request.add_tags,
            remove_tags=request.remove_tags,
        )
    }


@endpoint("models.add_or_update_metadata", min_version="2.13")
def add_or_update_metadata(
    call: APICall, company_id: str, request: AddOrUpdateMetadataRequest
):
    model_id = request.model
    model = ModelBLL.get_company_model_by_id(company_id=company_id, model_id=model_id)
    now = datetime.utcnow()
    return {
        "updated": Metadata.edit_metadata(
            model,
            items=request.metadata,
            replace_metadata=request.replace_metadata,
            last_update=now,
            last_change=now,
            last_changed_by=call.identity.user,
        )
    }


@endpoint("models.delete_metadata", min_version="2.13")
def delete_metadata(call: APICall, company_id: str, request: DeleteMetadataRequest):
    model_id = request.model
    model = ModelBLL.get_company_model_by_id(
        company_id=company_id, model_id=model_id, only_fields=("id",)
    )
    now = datetime.utcnow()
    return {
        "updated": Metadata.delete_metadata(
            model,
            keys=request.keys,
            last_update=now,
            last_change=now,
            last_changed_by=call.identity.user,
        )
    }
