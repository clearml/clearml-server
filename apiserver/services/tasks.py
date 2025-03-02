import itertools
from copy import deepcopy
from datetime import datetime
from functools import partial
from typing import Sequence, Union, Tuple, Mapping

from mongoengine import EmbeddedDocument, Q
from mongoengine.queryset.transform import COMPARISON_OPERATORS
from pymongo import UpdateOne

from apiserver.apierrors import errors
from apiserver.apierrors.errors.bad_request import InvalidTaskId
from apiserver.apimodels.base import (
    UpdateResponse,
    IdResponse,
    MakePublicRequest,
    MoveRequest,
)
from apiserver.apimodels.batch import (
    BatchResponse,
    UpdateBatchResponse,
    UpdateBatchItem,
)
from apiserver.apimodels.tasks import (
    StartedResponse,
    ResetResponse,
    PublishRequest,
    CreateRequest,
    UpdateRequest,
    SetRequirementsRequest,
    TaskRequest,
    DeleteRequest,
    PingRequest,
    EnqueueRequest,
    EnqueueResponse,
    DequeueResponse,
    CloneRequest,
    AddOrUpdateArtifactsRequest,
    GetTypesRequest,
    ResetRequest,
    GetHyperParamsRequest,
    EditHyperParamsRequest,
    DeleteHyperParamsRequest,
    GetConfigurationsRequest,
    EditConfigurationRequest,
    DeleteConfigurationRequest,
    GetConfigurationNamesRequest,
    DeleteArtifactsRequest,
    ArchiveResponse,
    ArchiveRequest,
    AddUpdateModelRequest,
    DeleteModelsRequest,
    StopManyRequest,
    EnqueueManyRequest,
    ResetManyRequest,
    DeleteManyRequest,
    PublishManyRequest,
    EnqueueManyResponse,
    EnqueueBatchItem,
    DequeueBatchItem,
    DequeueManyResponse,
    ResetManyResponse,
    ResetBatchItem,
    CompletedRequest,
    CompletedResponse,
    GetAllReq,
    DequeueRequest,
    DequeueManyRequest,
    UpdateTagsRequest,
    StopRequest,
    UnarchiveManyRequest,
    ArchiveManyRequest,
)
from apiserver.bll.event import EventBLL
from apiserver.bll.model import ModelBLL
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project import ProjectBLL
from apiserver.bll.queue import QueueBLL
from apiserver.bll.task import (
    TaskBLL,
    ChangeStatusRequest,
)
from apiserver.bll.task.task_cleanup import (
    delete_task_events_and_collect_urls,
    schedule_for_delete,
    CleanupResult,
)
from apiserver.bll.task.artifacts import (
    artifacts_prepare_for_save,
    artifacts_unprepare_from_saved,
    Artifacts,
)
from apiserver.bll.task.hyperparams import HyperParams
from apiserver.bll.task.param_utils import (
    params_prepare_for_save,
    params_unprepare_from_saved,
    escape_paths,
)
from apiserver.bll.task.task_operations import (
    stop_task,
    enqueue_task,
    dequeue_task,
    reset_task,
    archive_task,
    delete_task,
    publish_task,
    unarchive_task,
)
from apiserver.bll.task.utils import (
    update_task,
    get_task_for_update,
    deleted_prefix,
    get_many_tasks_for_writing,
    get_task_with_write_access,
)
from apiserver.bll.util import run_batch_operation, update_project_time
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model import EntityVisibility
from apiserver.database.model.project import Project
from apiserver.database.model.task.output import Output
from apiserver.database.model.task.task import (
    Task,
    TaskStatus,
    Script,
    ModelItem,
    TaskModelTypes,
)
from apiserver.database.utils import (
    get_fields_attr,
    parse_from_call,
    get_options,
)
from apiserver.service_repo import APICall, endpoint
from apiserver.service_repo.auth import Identity
from apiserver.services.utils import (
    conform_tag_fields,
    conform_output_tags,
    ModelsBackwardsCompatibility,
    DockerCmdBackwardsCompatibility,
    escape_dict_field,
    unescape_dict_field,
    process_include_subprojects,
)
from apiserver.utilities.dicts import nested_get
from apiserver.utilities.partial_version import PartialVersion

task_fields = set(Task.get_fields())
task_script_stripped_fields = set(
    [f for f, v in get_fields_attr(Script, "strip").items() if v]
)

task_bll = TaskBLL()
event_bll = EventBLL()
queue_bll = QueueBLL()
org_bll = OrgBLL()
project_bll = ProjectBLL()


def _assert_writable_tasks(
    company_id: str, identity: Identity, ids: Sequence[str], only=("id",)
) -> Sequence[Task]:
    tasks = get_many_tasks_for_writing(
        company_id=company_id,
        identity=identity,
        query=Q(id__in=ids),
        only=only,
    )
    missing_ids = set(ids) - {t.id for t in tasks}
    if missing_ids:
        raise errors.bad_request.InvalidTaskId(ids=list(missing_ids))

    return tasks


def set_task_status_from_call(
    request: UpdateRequest,
    company_id: str,
    identity: Identity,
    new_status=None,
    **set_fields,
) -> dict:
    task = get_task_with_write_access(
        request.task,
        company_id=company_id,
        identity=identity,
        only=("id", "status", "project"),
    )

    status_reason = request.status_reason
    status_message = request.status_message
    force = request.force
    return ChangeStatusRequest(
        task=task,
        new_status=new_status or task.status,
        status_reason=status_reason,
        status_message=status_message,
        force=force,
        user_id=identity.user,
    ).execute(**set_fields)


@endpoint("tasks.get_by_id", request_data_model=TaskRequest)
def get_by_id(call: APICall, company_id, request: TaskRequest):
    task = TaskBLL.assert_exists(
        company_id,
        task_ids=request.task,
        allow_public=True,
    )[0]
    task_dict = task.to_proper_dict()
    conform_task_data(call, task_dict)
    call.result.data = {"task": task_dict}


def escape_execution_parameters(call_data: dict) -> dict:
    if not call_data:
        return call_data

    keys = list(call_data)
    call_data = {
        safe_key: call_data[key] for key, safe_key in zip(keys, escape_paths(keys))
    }

    projection = Task.get_projection(call_data)
    if projection:
        Task.set_projection(call_data, escape_paths(projection))

    ordering = Task.get_ordering(call_data)
    if ordering:
        Task.set_ordering(call_data, escape_paths(ordering))

    return call_data


def _hidden_query(data: dict) -> Q:
    """
    1. Add only non-hidden tasks search condition (unless specifically specified differently)
    """
    if data.get("search_hidden") or data.get("id"):
        return Q()

    return Q(system_tags__ne=EntityVisibility.hidden.value)


@endpoint("tasks.get_all_ex")
def get_all_ex(call: APICall, company_id, request: GetAllReq):
    conform_tag_fields(call, call.data)
    call_data = escape_execution_parameters(call.data)
    process_include_subprojects(call_data)
    ret_params = {}
    tasks = Task.get_many_with_join(
        company=company_id,
        query_dict=call_data,
        query=_hidden_query(call_data),
        allow_public=request.allow_public,
        ret_params=ret_params,
    )
    conform_task_data(call, tasks)
    call.result.data = {"tasks": tasks, **ret_params}


@endpoint("tasks.get_by_id_ex", required_fields=["id"])
def get_by_id_ex(call: APICall, company_id, _):
    conform_tag_fields(call, call.data)
    call_data = escape_execution_parameters(call.data)
    tasks = Task.get_many_with_join(
        company=company_id,
        query_dict=call_data,
        allow_public=True,
    )

    conform_task_data(call, tasks)
    call.result.data = {"tasks": tasks}


@endpoint("tasks.get_all")
def get_all(call: APICall, company_id, _):
    conform_tag_fields(call, call.data)
    call_data = escape_execution_parameters(call.data)
    process_include_subprojects(call_data)

    ret_params = {}
    tasks = Task.get_many(
        company=company_id,
        parameters=call_data,
        query_dict=call_data,
        query=_hidden_query(call_data),
        allow_public=True,
        ret_params=ret_params,
    )
    conform_task_data(call, tasks)
    call.result.data = {"tasks": tasks, **ret_params}


@endpoint("tasks.get_types", request_data_model=GetTypesRequest)
def get_types(call: APICall, company_id, request: GetTypesRequest):
    call.result.data = {
        "types": list(
            project_bll.get_task_types(company_id, project_ids=request.projects)
        )
    }


@endpoint("tasks.stop", response_data_model=UpdateResponse)
def stop(call: APICall, company_id, request: StopRequest):
    """
    stop
    :summary: Stop a running task. Requires task status 'in_progress' and
              execution_progress 'running', or force=True.
              Development task is stopped immediately. For a non-development task
              only its status message is set to 'stopping'

    """
    call.result.data_model = UpdateResponse(
        **stop_task(
            task_id=request.task,
            company_id=company_id,
            identity=call.identity,
            user_name=call.identity.user_name,
            status_reason=request.status_reason,
            force=request.force,
            include_pipeline_steps=request.include_pipeline_steps,
        )
    )


@endpoint(
    "tasks.stop_many",
    request_data_model=StopManyRequest,
    response_data_model=UpdateBatchResponse,
)
def stop_many(call: APICall, company_id, request: StopManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            stop_task,
            company_id=company_id,
            identity=call.identity,
            user_name=call.identity.user_name,
            status_reason=request.status_reason,
            force=request.force,
            include_pipeline_steps=request.include_pipeline_steps,
        ),
        ids=request.ids,
    )
    call.result.data_model = UpdateBatchResponse(
        succeeded=[UpdateBatchItem(id=_id, **res) for _id, res in results],
        failed=failures,
    )


@endpoint(
    "tasks.stopped",
    request_data_model=UpdateRequest,
    response_data_model=UpdateResponse,
)
def stopped(call: APICall, company_id, req_model: UpdateRequest):
    call.result.data_model = UpdateResponse(
        **set_task_status_from_call(
            req_model,
            company_id=company_id,
            identity=call.identity,
            new_status=TaskStatus.stopped,
            completed=datetime.utcnow(),
        )
    )


@endpoint(
    "tasks.started",
    request_data_model=UpdateRequest,
    response_data_model=StartedResponse,
)
def started(call: APICall, company_id, req_model: UpdateRequest):
    started_update = {}
    if Task.objects(id=req_model.task, started=None).only("id"):
        # this is the fix for older versions putting started to None on reset
        started_update["started"] = datetime.utcnow()
    else:
        # don't override a previous, smaller "started" field value
        started_update["min__started"] = datetime.utcnow()

    res = StartedResponse(
        **set_task_status_from_call(
            req_model,
            company_id=company_id,
            identity=call.identity,
            new_status=TaskStatus.in_progress,
            **started_update,
        )
    )
    res.started = res.updated
    call.result.data_model = res


@endpoint(
    "tasks.failed", request_data_model=UpdateRequest, response_data_model=UpdateResponse
)
def failed(call: APICall, company_id, req_model: UpdateRequest):
    call.result.data_model = UpdateResponse(
        **set_task_status_from_call(
            req_model,
            company_id=company_id,
            identity=call.identity,
            new_status=TaskStatus.failed,
        )
    )


@endpoint(
    "tasks.close", request_data_model=UpdateRequest, response_data_model=UpdateResponse
)
def close(call: APICall, company_id, req_model: UpdateRequest):
    call.result.data_model = UpdateResponse(
        **set_task_status_from_call(
            req_model,
            company_id=company_id,
            identity=call.identity,
            new_status=TaskStatus.closed,
        )
    )


create_fields = {
    "name": None,
    "tags": list,
    "system_tags": list,
    "type": None,
    "error": None,
    "comment": None,
    "parent": Task,
    "project": Project,
    "input": None,
    "models": None,
    "container": dict,
    "output_dest": None,
    "execution": None,
    "hyperparams": dict,
    "configuration": dict,
    "script": None,
    "runtime": dict,
}


dict_fields_paths = [("execution", "model_labels"), "container"]


def prepare_for_save(call: APICall, fields: dict, previous_task: Task = None):
    conform_tag_fields(call, fields, validate=True)
    params_prepare_for_save(fields, previous_task=previous_task)
    artifacts_prepare_for_save(fields)
    ModelsBackwardsCompatibility.prepare_for_save(call, fields)
    DockerCmdBackwardsCompatibility.prepare_for_save(call, fields)
    for path in dict_fields_paths:
        escape_dict_field(fields, path)

    # Strip all script fields (remove leading and trailing whitespace chars) to avoid unusable names and paths
    script = fields.get("script")
    if script:
        for field in task_script_stripped_fields:
            value = script.get(field)
            if isinstance(value, str):
                script[field] = value.strip()

    return fields


def conform_task_data(call: APICall, tasks_data: Union[Sequence[dict], dict]):
    if isinstance(tasks_data, dict):
        tasks_data = [tasks_data]

    conform_output_tags(call, tasks_data)

    for data in tasks_data:
        for path in dict_fields_paths:
            unescape_dict_field(data, path)

    ModelsBackwardsCompatibility.unprepare_from_saved(call, tasks_data)
    DockerCmdBackwardsCompatibility.unprepare_from_saved(call, tasks_data)

    need_legacy_params = call.requested_endpoint_version < PartialVersion("2.9")

    for data in tasks_data:
        params_unprepare_from_saved(
            fields=data,
            copy_to_legacy=need_legacy_params,
        )
        artifacts_unprepare_from_saved(fields=data)


def prepare_create_fields(
    call: APICall,
    valid_fields=None,
    output=None,
    previous_task: Task = None,
):
    valid_fields = valid_fields if valid_fields is not None else create_fields
    t_fields = task_fields
    t_fields.add("output_dest")

    fields = parse_from_call(call.data, valid_fields, t_fields)

    # Move output_dest to output.destination
    output_dest = fields.get("output_dest")
    if output_dest is not None:
        fields.pop("output_dest")
        if output:
            output.destination = output_dest
        else:
            output = Output(destination=output_dest)
        fields["output"] = output

    # Add models updated time
    models = fields.get("models")
    if models:
        now = datetime.utcnow()
        for field in (TaskModelTypes.input, TaskModelTypes.output):
            field_models = models.get(field)
            if not field_models:
                continue
            for model in field_models:
                model["updated"] = now

    return prepare_for_save(call, fields, previous_task=previous_task)


def _validate_and_get_task_from_call(call: APICall, **kwargs) -> Tuple[Task, dict]:
    with translate_errors_context(
        field_does_not_exist_cls=errors.bad_request.ValidationError
    ):
        fields = prepare_create_fields(call, **kwargs)
        task = task_bll.create(
            company=call.identity.company, user=call.identity.user, fields=fields
        )

    task_bll.validate(task)

    return task, fields


@endpoint("tasks.validate", request_data_model=CreateRequest)
def validate(call: APICall, _, __: CreateRequest):
    parent = call.data.get("parent")
    if parent and parent.startswith(deleted_prefix):
        call.data.pop("parent")
    _validate_and_get_task_from_call(call)


def _update_cached_tags(company: str, project: str, fields: dict):
    org_bll.update_tags(
        company,
        Tags.Task,
        projects=[project],
        tags=fields.get("tags"),
        system_tags=fields.get("system_tags"),
    )


def _reset_cached_tags(company: str, projects: Sequence[str]):
    org_bll.reset_tags(company, Tags.Task, projects=projects)


@endpoint(
    "tasks.create", request_data_model=CreateRequest, response_data_model=IdResponse
)
def create(call: APICall, company_id, _: CreateRequest):
    task, fields = _validate_and_get_task_from_call(call)

    with translate_errors_context():
        task.save()
        _update_cached_tags(company_id, project=task.project, fields=fields)
        update_project_time(task.project)

    call.result.data_model = IdResponse(id=task.id)


@endpoint("tasks.clone", request_data_model=CloneRequest)
def clone_task(call: APICall, company_id, request: CloneRequest):
    task, new_project = task_bll.clone_task(
        company_id=company_id,
        user_id=call.identity.user,
        task_id=request.task,
        name=request.new_task_name,
        comment=request.new_task_comment,
        parent=request.new_task_parent,
        project=request.new_task_project,
        tags=request.new_task_tags,
        system_tags=request.new_task_system_tags,
        hyperparams=request.new_task_hyperparams,
        configuration=request.new_task_configuration,
        container=request.new_task_container,
        execution_overrides=request.execution_overrides,
        input_models=request.new_task_input_models,
        validate_references=request.validate_references,
        new_project_name=request.new_project_name,
    )
    call.result.data = {
        "id": task.id,
        **({"new_project": new_project} if new_project else {}),
    }


def prepare_update_fields(call: APICall, call_data):
    valid_fields = deepcopy(Task.user_set_allowed())
    update_fields = {k: v for k, v in create_fields.items() if k in valid_fields}
    update_fields.update(
        status=None, status_reason=None, status_message=None, output__error=None
    )
    t_fields = task_fields
    t_fields.add("output__error")
    fields = parse_from_call(call_data, update_fields, t_fields)
    return prepare_for_save(call, fields), valid_fields


@endpoint(
    "tasks.update", request_data_model=UpdateRequest, response_data_model=UpdateResponse
)
def update(call: APICall, company_id, req_model: UpdateRequest):
    task_id = req_model.task

    with translate_errors_context():
        task = get_task_with_write_access(
            task_id=task_id,
            company_id=company_id,
            identity=call.identity,
            only=("id", "project"),
        )

        partial_update_dict, valid_fields = prepare_update_fields(call, call.data)

        if not partial_update_dict:
            return UpdateResponse(updated=0)

        updated_count, updated_fields = Task.safe_update(
            company_id=company_id,
            id=task_id,
            partial_update_dict=partial_update_dict,
            injected_update=dict(
                last_change=datetime.utcnow(),
                last_changed_by=call.identity.user,
            ),
        )
        if updated_count:
            new_project = updated_fields.get("project", task.project)
            if new_project != task.project:
                _reset_cached_tags(company_id, projects=[new_project, task.project])
            else:
                _update_cached_tags(
                    company_id, project=task.project, fields=updated_fields
                )
            update_project_time(updated_fields.get("project"))
        conform_task_data(call, updated_fields)
        return UpdateResponse(updated=updated_count, fields=updated_fields)


@endpoint(
    "tasks.set_requirements",
    request_data_model=SetRequirementsRequest,
    response_data_model=UpdateResponse,
)
def set_requirements(call: APICall, company_id, req_model: SetRequirementsRequest):
    requirements = req_model.requirements
    with translate_errors_context():
        task = get_task_with_write_access(
            req_model.task,
            company_id=company_id,
            identity=call.identity,
            only=("status", "script"),
        )
        if not task.script:
            raise errors.bad_request.MissingTaskFields(
                "Task has no script field", task=task.id
            )
        res = update_task(
            task,
            user_id=call.identity.user,
            update_cmds=dict(script__requirements=requirements),
        )
        call.result.data_model = UpdateResponse(updated=res)
        if res:
            call.result.data_model.fields = {"script.requirements": requirements}


@endpoint("tasks.update_batch")
def update_batch(call: APICall, company_id, _):
    items = call.batched_data
    if items is None:
        raise errors.bad_request.BatchContainsNoItems()

    with translate_errors_context():
        items = {i["task"]: i for i in items}
        tasks = {
            t.id: t
            for t in _assert_writable_tasks(
                identity=call.identity,
                company_id=company_id,
                ids=list(items),
                only=("id", "project"),
            )
        }

        if len(tasks) < len(items):
            missing = tuple(set(items).difference(tasks))
            raise errors.bad_request.InvalidTaskId(ids=missing)

        now = datetime.utcnow()

        bulk_ops = []
        updated_projects = set()
        for id, data in items.items():
            task = tasks[id]
            fields, valid_fields = prepare_update_fields(call, data)
            partial_update_dict = Task.get_safe_update_dict(fields)
            if not partial_update_dict:
                continue
            partial_update_dict.update(
                last_change=now,
                last_changed_by=call.identity.user,
            )
            update_op = UpdateOne(
                {"_id": id, "company": company_id}, {"$set": partial_update_dict}
            )
            bulk_ops.append(update_op)

            new_project = partial_update_dict.get("project", task.project)
            if new_project != task.project:
                updated_projects.update({new_project, task.project})
            elif any(f in partial_update_dict for f in ("tags", "system_tags")):
                updated_projects.add(task.project)

        updated = 0
        if bulk_ops:
            res = Task._get_collection().bulk_write(bulk_ops)
            updated = res.modified_count

        if updated and updated_projects:
            projects = list(updated_projects)
            _reset_cached_tags(company_id, projects=projects)
            update_project_time(project_ids=projects)

        call.result.data = {"updated": updated}


@endpoint(
    "tasks.edit", request_data_model=UpdateRequest, response_data_model=UpdateResponse
)
def edit(call: APICall, company_id, req_model: UpdateRequest):
    task_id = req_model.task
    force = req_model.force

    with translate_errors_context():
        task = get_task_with_write_access(
            task_id=task_id,
            company_id=company_id,
            identity=call.identity,
        )

        if not force and task.status != TaskStatus.created:
            raise errors.bad_request.InvalidTaskStatus(
                expected=TaskStatus.created, status=task.status
            )

        edit_fields = create_fields.copy()
        edit_fields.update(dict(status=None))

        with translate_errors_context(
            field_does_not_exist_cls=errors.bad_request.ValidationError
        ):
            fields = prepare_create_fields(
                call, valid_fields=edit_fields, output=task.output, previous_task=task
            )

        for key in fields:
            field = getattr(task, key, None)
            value = fields[key]
            if (
                field
                and isinstance(value, dict)
                and isinstance(field, EmbeddedDocument)
            ):
                d = field.to_mongo(use_db_field=False).to_dict()
                d.update(value)
                fields[key] = d

        task_bll.validate(
            task_bll.create(
                company=call.identity.company, user=call.identity.user, fields=fields
            )
        )

        # make sure field names do not end in mongoengine comparison operators
        fixed_fields = {
            (k if k not in COMPARISON_OPERATORS else "%s__" % k): v
            for k, v in fields.items()
        }
        if fixed_fields:
            now = datetime.utcnow()
            last_change = dict(last_change=now, last_changed_by=call.identity.user)
            if not set(fields).issubset(Task.user_set_allowed()):
                last_change.update(last_update=now)
            fields.update(**last_change)
            fixed_fields.update(**last_change)
            updated = task.update(upsert=False, **fixed_fields)
            if updated:
                new_project = fixed_fields.get("project", task.project)
                if new_project != task.project:
                    _reset_cached_tags(company_id, projects=[new_project, task.project])
                else:
                    _update_cached_tags(
                        company_id, project=task.project, fields=fixed_fields
                    )
                update_project_time(fields.get("project"))
            conform_task_data(call, fields)
            call.result.data_model = UpdateResponse(updated=updated, fields=fields)
        else:
            call.result.data_model = UpdateResponse(updated=0)


@endpoint(
    "tasks.get_hyper_params",
    request_data_model=GetHyperParamsRequest,
)
def get_hyper_params(call: APICall, company_id, request: GetHyperParamsRequest):
    tasks_params = HyperParams.get_params(company_id, task_ids=request.tasks)

    call.result.data = {
        "params": [{"task": task, **data} for task, data in tasks_params.items()]
    }


@endpoint("tasks.edit_hyper_params", request_data_model=EditHyperParamsRequest)
def edit_hyper_params(call: APICall, company_id, request: EditHyperParamsRequest):
    call.result.data = {
        "updated": HyperParams.edit_params(
            company_id,
            identity=call.identity,
            task_id=request.task,
            hyperparams=request.hyperparams,
            replace_hyperparams=request.replace_hyperparams,
            force=request.force,
        )
    }


@endpoint("tasks.delete_hyper_params", request_data_model=DeleteHyperParamsRequest)
def delete_hyper_params(call: APICall, company_id, request: DeleteHyperParamsRequest):
    call.result.data = {
        "deleted": HyperParams.delete_params(
            company_id,
            identity=call.identity,
            task_id=request.task,
            hyperparams=request.hyperparams,
            force=request.force,
        )
    }


@endpoint(
    "tasks.get_configurations",
    request_data_model=GetConfigurationsRequest,
)
def get_configurations(call: APICall, company_id, request: GetConfigurationsRequest):
    tasks_params = HyperParams.get_configurations(
        company_id, task_ids=request.tasks, names=request.names
    )

    call.result.data = {
        "configurations": [
            {"task": task, **data} for task, data in tasks_params.items()
        ]
    }


@endpoint(
    "tasks.get_configuration_names",
    request_data_model=GetConfigurationNamesRequest,
)
def get_configuration_names(
    call: APICall, company_id, request: GetConfigurationNamesRequest
):
    tasks_params = HyperParams.get_configuration_names(
        company_id, task_ids=request.tasks, skip_empty=request.skip_empty
    )

    call.result.data = {
        "configurations": [
            {"task": task, **data} for task, data in tasks_params.items()
        ]
    }


@endpoint("tasks.edit_configuration", request_data_model=EditConfigurationRequest)
def edit_configuration(call: APICall, company_id, request: EditConfigurationRequest):
    call.result.data = {
        "updated": HyperParams.edit_configuration(
            company_id,
            identity=call.identity,
            task_id=request.task,
            configuration=request.configuration,
            replace_configuration=request.replace_configuration,
            force=request.force,
        )
    }


@endpoint("tasks.delete_configuration", request_data_model=DeleteConfigurationRequest)
def delete_configuration(
    call: APICall, company_id, request: DeleteConfigurationRequest
):
    call.result.data = {
        "deleted": HyperParams.delete_configuration(
            company_id,
            identity=call.identity,
            task_id=request.task,
            configuration=request.configuration,
            force=request.force,
        )
    }


@endpoint(
    "tasks.enqueue",
    request_data_model=EnqueueRequest,
    response_data_model=EnqueueResponse,
)
def enqueue(call: APICall, company_id, request: EnqueueRequest):
    if request.verify_watched_queue and not request.update_execution_queue:
        raise errors.bad_request.ValidationError(
            "verify_watched_queue cannot be used with update_execution_queue=False"
        )

    queued, res = enqueue_task(
        task_id=request.task,
        company_id=company_id,
        identity=call.identity,
        queue_id=request.queue,
        status_message=request.status_message,
        status_reason=request.status_reason,
        queue_name=request.queue_name,
        force=request.force,
        update_execution_queue=request.update_execution_queue,
    )
    if request.verify_watched_queue:
        res_queue = nested_get(res, ("fields", "execution.queue"))
        if res_queue:
            res["queue_watched"] = queue_bll.check_for_workers(company_id, res_queue)

    call.result.data_model = EnqueueResponse(queued=queued, **res)


@endpoint(
    "tasks.enqueue_many",
    request_data_model=EnqueueManyRequest,
    response_data_model=EnqueueManyResponse,
)
def enqueue_many(call: APICall, company_id, request: EnqueueManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            enqueue_task,
            company_id=company_id,
            identity=call.identity,
            queue_id=request.queue,
            status_message=request.status_message,
            status_reason=request.status_reason,
            queue_name=request.queue_name,
            validate=request.validate_tasks,
        ),
        ids=request.ids,
    )
    extra = {}
    if request.verify_watched_queue and results:
        _id, (queued, res) = results[0]
        res_queue = nested_get(res, ("fields", "execution.queue"))
        if res_queue:
            extra["queue_watched"] = queue_bll.check_for_workers(company_id, res_queue)

    call.result.data_model = EnqueueManyResponse(
        succeeded=[
            EnqueueBatchItem(id=_id, queued=bool(queued), **res)
            for _id, (queued, res) in results
        ],
        failed=failures,
        **extra,
    )


@endpoint(
    "tasks.dequeue",
    response_data_model=DequeueResponse,
)
def dequeue(call: APICall, company_id, request: DequeueRequest):
    dequeued, res = dequeue_task(
        task_id=request.task,
        company_id=company_id,
        identity=call.identity,
        status_message=request.status_message,
        status_reason=request.status_reason,
        remove_from_all_queues=request.remove_from_all_queues,
        new_status=request.new_status,
    )
    call.result.data_model = DequeueResponse(dequeued=dequeued, **res)


@endpoint(
    "tasks.dequeue_many",
    response_data_model=DequeueManyResponse,
)
def dequeue_many(call: APICall, company_id, request: DequeueManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            dequeue_task,
            company_id=company_id,
            identity=call.identity,
            status_message=request.status_message,
            status_reason=request.status_reason,
            remove_from_all_queues=request.remove_from_all_queues,
            new_status=request.new_status,
        ),
        ids=request.ids,
    )
    call.result.data_model = DequeueManyResponse(
        succeeded=[
            DequeueBatchItem(id=_id, dequeued=bool(dequeued), **res)
            for _id, (dequeued, res) in results
        ],
        failed=failures,
    )


def _delete_task_events(
    company_id: str,
    user_id: str,
    tasks: Mapping[str, CleanupResult],
    delete_external_artifacts: bool,
    sync_delete: bool,
):
    if not tasks:
        return
    task_ids = list(tasks)
    deleted_model_ids = set(
        itertools.chain.from_iterable(
            cr.deleted_model_ids for cr in tasks.values() if cr.deleted_model_ids
        )
    )

    delete_external_artifacts = delete_external_artifacts and config.get(
        "services.async_urls_delete.enabled", True
    )
    if delete_external_artifacts:
        for t_id, cleanup_res in tasks.items():
            urls = set(cleanup_res.urls.model_urls) | set(
                cleanup_res.urls.artifact_urls
            )
            if urls:
                schedule_for_delete(
                    task_id=t_id,
                    company=company_id,
                    user=user_id,
                    urls=urls,
                    can_delete_folders=False,
                )

        event_urls = delete_task_events_and_collect_urls(
            company=company_id,
            task_ids=task_ids,
            wait_for_delete=sync_delete,
        )
        if deleted_model_ids:
            event_urls.update(
                delete_task_events_and_collect_urls(
                    company=company_id,
                    task_ids=list(deleted_model_ids),
                    model=True,
                    wait_for_delete=sync_delete,
                )
            )

        if event_urls:
            schedule_for_delete(
                task_id=task_ids[0],
                company=company_id,
                user=user_id,
                urls=event_urls,
                can_delete_folders=False,
            )
    else:
        event_bll.delete_task_events(company_id, task_ids, wait_for_delete=sync_delete)
        if deleted_model_ids:
            event_bll.delete_task_events(
                company_id,
                list(deleted_model_ids),
                model=True,
                wait_for_delete=sync_delete,
            )


@endpoint(
    "tasks.reset", request_data_model=ResetRequest, response_data_model=ResetResponse
)
def reset(call: APICall, company_id, request: ResetRequest):
    task_id = request.task
    dequeued, cleanup_res, updates = reset_task(
        task_id=task_id,
        company_id=company_id,
        identity=call.identity,
        force=request.force,
        delete_output_models=request.delete_output_models,
        clear_all=request.clear_all,
    )
    _delete_task_events(
        company_id=company_id,
        user_id=call.identity.user,
        tasks={task_id: cleanup_res},
        delete_external_artifacts=request.delete_external_artifacts,
        sync_delete=True,
    )
    res = ResetResponse(
        **updates,
        **cleanup_res.to_res_dict(request.return_file_urls),
        dequeued=dequeued,
    )
    call.result.data_model = res


@endpoint(
    "tasks.reset_many",
    request_data_model=ResetManyRequest,
    response_data_model=ResetManyResponse,
)
def reset_many(call: APICall, company_id, request: ResetManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            reset_task,
            company_id=company_id,
            identity=call.identity,
            force=request.force,
            delete_output_models=request.delete_output_models,
            clear_all=request.clear_all,
        ),
        ids=request.ids,
    )

    succeeded = []
    tasks = {}
    for _id, (dequeued, cleanup, res) in results:
        tasks[_id] = cleanup
        succeeded.append(
            ResetBatchItem(
                id=_id,
                dequeued=bool(dequeued.get("removed")) if dequeued else False,
                **cleanup.to_res_dict(request.return_file_urls),
                **res,
            )
        )

    _delete_task_events(
        company_id=company_id,
        user_id=call.identity.user,
        tasks=tasks,
        delete_external_artifacts=request.delete_external_artifacts,
        sync_delete=False,
    )

    call.result.data_model = ResetManyResponse(
        succeeded=succeeded,
        failed=failures,
    )


@endpoint(
    "tasks.archive",
    request_data_model=ArchiveRequest,
    response_data_model=ArchiveResponse,
)
def archive(call: APICall, company_id, request: ArchiveRequest):
    archived = 0
    tasks = _assert_writable_tasks(
        company_id,
        call.identity,
        ids=request.tasks,
        only=(
            "id",
            "company",
            "execution",
            "status",
            "project",
            "system_tags",
            "enqueue_status",
            "type",
        ),
    )
    for task in tasks:
        archived += archive_task(
            company_id=company_id,
            identity=call.identity,
            task=task,
            status_message=request.status_message,
            status_reason=request.status_reason,
            include_pipeline_steps=request.include_pipeline_steps,
        )

    call.result.data_model = ArchiveResponse(archived=archived)


@endpoint(
    "tasks.archive_many",
    response_data_model=BatchResponse,
)
def archive_many(call: APICall, company_id, request: ArchiveManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            archive_task,
            company_id=company_id,
            identity=call.identity,
            status_message=request.status_message,
            status_reason=request.status_reason,
            include_pipeline_steps=request.include_pipeline_steps,
        ),
        ids=request.ids,
    )
    call.result.data_model = BatchResponse(
        succeeded=[dict(id=_id, archived=bool(archived)) for _id, archived in results],
        failed=failures,
    )


@endpoint(
    "tasks.unarchive_many",
    response_data_model=BatchResponse,
)
def unarchive_many(call: APICall, company_id, request: UnarchiveManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            unarchive_task,
            company_id=company_id,
            identity=call.identity,
            status_message=request.status_message,
            status_reason=request.status_reason,
            include_pipeline_steps=request.include_pipeline_steps,
        ),
        ids=request.ids,
    )
    call.result.data_model = BatchResponse(
        succeeded=[
            dict(id=_id, unarchived=bool(unarchived)) for _id, unarchived in results
        ],
        failed=failures,
    )


@endpoint("tasks.delete", request_data_model=DeleteRequest)
def delete(call: APICall, company_id, request: DeleteRequest):
    deleted, task, cleanup_res = delete_task(
        task_id=request.task,
        company_id=company_id,
        identity=call.identity,
        move_to_trash=request.move_to_trash,
        force=request.force,
        delete_output_models=request.delete_output_models,
        status_message=request.status_message,
        status_reason=request.status_reason,
        include_pipeline_steps=request.include_pipeline_steps,
    )
    if deleted:
        _delete_task_events(
            company_id=company_id,
            user_id=call.identity.user,
            tasks={request.task: cleanup_res},
            delete_external_artifacts=request.delete_external_artifacts,
            sync_delete=True,
        )
        _reset_cached_tags(company_id, projects=[task.project] if task.project else [])
    call.result.data = dict(
        deleted=bool(deleted), **cleanup_res.to_res_dict(request.return_file_urls)
    )


@endpoint("tasks.delete_many", request_data_model=DeleteManyRequest)
def delete_many(call: APICall, company_id, request: DeleteManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            delete_task,
            company_id=company_id,
            identity=call.identity,
            move_to_trash=request.move_to_trash,
            force=request.force,
            delete_output_models=request.delete_output_models,
            status_message=request.status_message,
            status_reason=request.status_reason,
            include_pipeline_steps=request.include_pipeline_steps,
        ),
        ids=request.ids,
    )

    succeeded = []
    tasks = {}
    if results:
        projects = set()
        for _id, (deleted, task, cleanup_res) in results:
            if deleted:
                projects.add(task.project)
                tasks[_id] = cleanup_res
            succeeded.append(
                dict(
                    id=_id,
                    deleted=bool(deleted),
                    **cleanup_res.to_res_dict(request.return_file_urls),
                )
            )

        if tasks:
            _delete_task_events(
                company_id=company_id,
                user_id=call.identity.user,
                tasks=tasks,
                delete_external_artifacts=request.delete_external_artifacts,
                sync_delete=False,
            )
        _reset_cached_tags(company_id, projects=list(projects))

    call.result.data = dict(
        succeeded=succeeded,
        failed=failures,
    )


@endpoint(
    "tasks.publish",
    request_data_model=PublishRequest,
    response_data_model=UpdateResponse,
)
def publish(call: APICall, company_id, request: PublishRequest):
    updates = publish_task(
        task_id=request.task,
        company_id=company_id,
        identity=call.identity,
        force=request.force,
        publish_model_func=ModelBLL.publish_model if request.publish_model else None,
        status_reason=request.status_reason,
        status_message=request.status_message,
    )
    call.result.data_model = UpdateResponse(**updates)


@endpoint(
    "tasks.publish_many",
    request_data_model=PublishManyRequest,
    response_data_model=UpdateBatchResponse,
)
def publish_many(call: APICall, company_id, request: PublishManyRequest):
    results, failures = run_batch_operation(
        func=partial(
            publish_task,
            company_id=company_id,
            identity=call.identity,
            force=request.force,
            publish_model_func=ModelBLL.publish_model
            if request.publish_model
            else None,
            status_reason=request.status_reason,
            status_message=request.status_message,
        ),
        ids=request.ids,
    )

    call.result.data_model = UpdateBatchResponse(
        succeeded=[UpdateBatchItem(id=_id, **res) for _id, res in results],
        failed=failures,
    )


@endpoint(
    "tasks.completed",
    min_version="2.2",
    request_data_model=CompletedRequest,
    response_data_model=CompletedResponse,
)
def completed(call: APICall, company_id, request: CompletedRequest):
    res = CompletedResponse(
        **set_task_status_from_call(
            request,
            company_id=company_id,
            identity=call.identity,
            new_status=TaskStatus.completed,
            completed=datetime.utcnow(),
        )
    )

    if res.updated and request.publish:
        publish_res = publish_task(
            task_id=request.task,
            company_id=company_id,
            identity=call.identity,
            force=request.force,
            publish_model_func=ModelBLL.publish_model,
            status_reason=request.status_reason,
            status_message=request.status_message,
        )
        res.published = publish_res.get("updated")
        new_status = nested_get(publish_res, ("fields", "status"))
        if new_status:
            res.fields["status"] = new_status

    call.result.data_model = res


@endpoint("tasks.ping", request_data_model=PingRequest)
def ping(call: APICall, company_id, request: PingRequest):
    TaskBLL.set_last_update(
        task_ids=[request.task],
        company_id=company_id,
        user_id=call.identity.user,
        last_update=datetime.utcnow(),
    )


@endpoint(
    "tasks.add_or_update_artifacts",
    min_version="2.10",
    request_data_model=AddOrUpdateArtifactsRequest,
)
def add_or_update_artifacts(
    call: APICall, company_id, request: AddOrUpdateArtifactsRequest
):
    call.result.data = {
        "updated": Artifacts.add_or_update_artifacts(
            company_id=company_id,
            identity=call.identity,
            task_id=request.task,
            artifacts=request.artifacts,
            force=True,
        )
    }


@endpoint(
    "tasks.delete_artifacts",
    min_version="2.10",
    request_data_model=DeleteArtifactsRequest,
)
def delete_artifacts(call: APICall, company_id, request: DeleteArtifactsRequest):
    call.result.data = {
        "deleted": Artifacts.delete_artifacts(
            company_id=company_id,
            identity=call.identity,
            task_id=request.task,
            artifact_ids=request.artifacts,
            force=True,
        )
    }


@endpoint("tasks.make_public", min_version="2.9", request_data_model=MakePublicRequest)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Task.set_public(
        company_id=company_id,
        user_id=call.identity.user,
        ids=request.ids,
        invalid_cls=InvalidTaskId,
        enabled=True,
    )


@endpoint("tasks.make_private", min_version="2.9", request_data_model=MakePublicRequest)
def make_public(call: APICall, company_id, request: MakePublicRequest):
    call.result.data = Task.set_public(
        company_id=company_id,
        user_id=call.identity.user,
        ids=request.ids,
        invalid_cls=InvalidTaskId,
        enabled=False,
    )


@endpoint("tasks.move", request_data_model=MoveRequest)
def move(call: APICall, company_id: str, request: MoveRequest):
    if not ("project" in call.data or request.project_name):
        raise errors.bad_request.MissingRequiredFields(
            "project or project_name is required"
        )

    tasks = _assert_writable_tasks(
        company_id, call.identity, request.ids, only=("id", "project")
    )
    updated_projects = set(t.project for t in tasks if t.project)

    project_id = project_bll.move_under_project(
        entity_cls=Task,
        user=call.identity.user,
        company=company_id,
        ids=request.ids,
        project=request.project,
        project_name=request.project_name,
    )

    projects = list(updated_projects | {project_id})
    _reset_cached_tags(company_id, projects=projects)
    update_project_time(projects)

    return {"project_id": project_id}


@endpoint("tasks.update_tags")
def update_tags(call: APICall, company_id: str, request: UpdateTagsRequest):
    _assert_writable_tasks(company_id, call.identity, request.ids)
    return {
        "updated": org_bll.edit_entity_tags(
            company_id=company_id,
            user_id=call.identity.user,
            entity_cls=Task,
            entity_ids=request.ids,
            add_tags=request.add_tags,
            remove_tags=request.remove_tags,
        )
    }


@endpoint("tasks.add_or_update_model", min_version="2.13")
def add_or_update_model(call: APICall, company_id: str, request: AddUpdateModelRequest):
    get_task_for_update(
        company_id=company_id, task_id=request.task, force=True, identity=call.identity
    )

    models_field = f"models__{request.type}"
    model = ModelItem(name=request.name, model=request.model, updated=datetime.utcnow())
    query = {"id": request.task, f"{models_field}__name": request.name}
    updated = Task.objects(**query).update_one(**{f"set__{models_field}__S": model})

    updated = TaskBLL.update_statistics(
        task_id=request.task,
        company_id=company_id,
        user_id=call.identity.user,
        last_iteration_max=request.iteration,
        **({f"push__{models_field}": model} if not updated else {}),
    )

    return {"updated": updated}


@endpoint("tasks.delete_models", min_version="2.13")
def delete_models(call: APICall, company_id: str, request: DeleteModelsRequest):
    task = get_task_for_update(
        company_id=company_id, task_id=request.task, force=True, identity=call.identity
    )

    delete_names = {
        type_: [m.name for m in request.models if m.type == type_]
        for type_ in get_options(TaskModelTypes)
    }
    commands = {
        f"pull__models__{field}__name__in": names
        for field, names in delete_names.items()
        if names
    }

    updated = task.update(
        last_change=datetime.utcnow(),
        last_changed_by=call.identity.user,
        **commands,
    )
    return {"updated": updated}
