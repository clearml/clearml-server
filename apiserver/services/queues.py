from typing import Union, Sequence

from mongoengine import Q

from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.queues import (
    GetDefaultResp,
    CreateRequest,
    DeleteRequest,
    UpdateRequest,
    MoveTaskRequest,
    MoveTaskResponse,
    TaskRequest,
    QueueRequest,
    GetMetricsRequest,
    GetMetricsResponse,
    QueueMetrics,
    AddOrUpdateMetadataRequest,
    DeleteMetadataRequest,
    GetNextTaskRequest,
    GetByIdRequest,
    GetAllRequest,
    AddTaskRequest,
    RemoveTaskRequest,
)
from apiserver.bll.model import Metadata
from apiserver.bll.queue import QueueBLL
from apiserver.bll.queue.queue_bll import MOVE_FIRST, MOVE_LAST
from apiserver.bll.workers import WorkerBLL
from apiserver.config_repo import config
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.service_repo import APICall, endpoint
from apiserver.services.utils import (
    conform_tag_fields,
    conform_output_tags,
    conform_tags,
    escape_metadata,
    unescape_metadata,
)
from apiserver.utilities import extract_properties_to_lists

worker_bll = WorkerBLL()
queue_bll = QueueBLL(worker_bll)


def conform_queue_data(call: APICall, queue_data: Union[Sequence[dict], dict]):
    conform_output_tags(call, queue_data)
    unescape_metadata(call, queue_data)


@endpoint("queues.get_by_id", min_version="2.4")
def get_by_id(call: APICall, company_id, request: GetByIdRequest):
    queue = queue_bll.get_by_id(
        company_id, request.queue, max_task_entries=request.max_task_entries
    )
    queue_dict = queue.to_proper_dict()
    conform_queue_data(call, queue_dict)
    call.result.data = {"queue": queue_dict}


@endpoint("queues.get_default", min_version="2.4", response_data_model=GetDefaultResp)
def get_by_id(call: APICall):
    queue = queue_bll.get_default(call.identity.company)
    call.result.data_model = GetDefaultResp(id=queue.id, name=queue.name)


def _hidden_query(data: dict) -> Q:
    """
    1. Add only non-hidden queues search condition (unless specifically specified differently)
    """
    hidden_tags = config.get("services.queues.hidden_tags", [])
    if (
        not hidden_tags
        or data.get("search_hidden")
        or data.get("id")
        or data.get("name")
    ):
        return Q()

    return Q(system_tags__nin=hidden_tags)


@endpoint("queues.get_all_ex", min_version="2.4")
def get_all_ex(call: APICall, company: str, request: GetAllRequest):
    conform_tag_fields(call, call.data)
    ret_params = {}

    call_data = Metadata.escape_query_parameters(call.data)
    queues = queue_bll.get_queue_infos(
        company_id=company,
        query_dict=call_data,
        query=_hidden_query(call_data),
        max_task_entries=request.max_task_entries,
        ret_params=ret_params,
    )
    conform_queue_data(call, queues)
    call.result.data = {"queues": queues, **ret_params}


@endpoint("queues.get_all", min_version="2.4")
def get_all(call: APICall, company: str, request: GetAllRequest):
    conform_tag_fields(call, call.data)
    ret_params = {}
    call_data = Metadata.escape_query_parameters(call.data)
    queues = queue_bll.get_all(
        company_id=company,
        query_dict=call_data,
        query=_hidden_query(call_data),
        max_task_entries=request.max_task_entries,
        ret_params=ret_params,
    )
    conform_queue_data(call, queues)
    call.result.data = {"queues": queues, **ret_params}


@endpoint("queues.create", min_version="2.4")
def create(call: APICall, company_id, request: CreateRequest):
    tags, system_tags = conform_tags(
        call, request.tags, request.system_tags, validate=True
    )
    queue = queue_bll.create(
        company_id=company_id,
        name=request.name,
        display_name=request.display_name,
        tags=tags,
        system_tags=system_tags,
        metadata=Metadata.metadata_from_api(request.metadata),
    )
    call.result.data = {"id": queue.id}


@endpoint(
    "queues.update",
    min_version="2.4",
    response_data_model=UpdateResponse,
)
def update(call: APICall, company_id, request: UpdateRequest):
    data = call.data_model_for_partial_update
    conform_tag_fields(call, data, validate=True)
    escape_metadata(data)
    updated, fields = queue_bll.update(
        company_id=company_id, queue_id=request.queue, **data
    )
    conform_queue_data(call, fields)
    call.result.data_model = UpdateResponse(updated=updated, fields=fields)


@endpoint("queues.delete", min_version="2.4")
def delete(call: APICall, company_id, request: DeleteRequest):
    queue_bll.delete(
        company_id=company_id,
        user_id=call.identity.user,
        queue_id=request.queue,
        force=request.force,
    )
    call.result.data = {"deleted": 1}


@endpoint("queues.add_task", min_version="2.4")
def add_task(call: APICall, company_id, request: AddTaskRequest):
    added = queue_bll.add_task(
        company_id=company_id, queue_id=request.queue, task_id=request.task
    )
    if added and request.update_execution_queue:
        Task.objects(id=request.task).update(
            execution__queue=request.queue, multi=False
        )
    call.result.data = {"added": added}


@endpoint("queues.get_next_task")
def get_next_task(call: APICall, company_id, request: GetNextTaskRequest):
    entry = queue_bll.get_next_task(
        company_id=company_id, queue_id=request.queue, task_id=request.task
    )
    if entry:
        data = {"entry": entry.to_proper_dict()}
        task = Task.objects(id=entry.task).only("company", "user", "status").first()
        if task:
            # fix racing condition that can result in the task being aborted
            # by an agent after it was already placed in a queue
            if task.status == TaskStatus.stopped:
                task.update(status=TaskStatus.queued)

            if request.get_task_info:
                data["task_info"] = {"company": task.company, "user": task.user}

        call.result.data = data


@endpoint("queues.remove_task", min_version="2.4")
def remove_task(call: APICall, company_id, request: RemoveTaskRequest):
    call.result.data = {
        "removed": queue_bll.remove_task(
            company_id=company_id,
            user_id=call.identity.user,
            queue_id=request.queue,
            task_id=request.task,
            update_task_status=request.update_task_status,
        )
    }


@endpoint("queues.clear_queue")
def clear_queue(call: APICall, company_id, request: QueueRequest):
    call.result.data = {
        "removed_tasks": queue_bll.clear_queue(
            company_id=company_id,
            user_id=call.identity.user,
            queue_id=request.queue,
        )
    }


@endpoint(
    "queues.move_task_forward",
    min_version="2.4",
    response_data_model=MoveTaskResponse,
)
def move_task_forward(call: APICall, company_id, request: MoveTaskRequest):
    call.result.data_model = MoveTaskResponse(
        position=queue_bll.reposition_task(
            company_id=company_id,
            queue_id=request.queue,
            task_id=request.task,
            move_count=-request.count,
        )
    )


@endpoint(
    "queues.move_task_backward",
    min_version="2.4",
    response_data_model=MoveTaskResponse,
)
def move_task_backward(call: APICall, company_id, request: MoveTaskRequest):
    call.result.data_model = MoveTaskResponse(
        position=queue_bll.reposition_task(
            company_id=company_id,
            queue_id=request.queue,
            task_id=request.task,
            move_count=request.count,
        )
    )


@endpoint(
    "queues.move_task_to_front",
    min_version="2.4",
    response_data_model=MoveTaskResponse,
)
def move_task_to_front(call: APICall, company_id, request: TaskRequest):
    call.result.data_model = MoveTaskResponse(
        position=queue_bll.reposition_task(
            company_id=company_id,
            queue_id=request.queue,
            task_id=request.task,
            move_count=MOVE_FIRST,
        )
    )


@endpoint(
    "queues.move_task_to_back",
    min_version="2.4",
    response_data_model=MoveTaskResponse,
)
def move_task_to_back(call: APICall, company_id, request: TaskRequest):
    call.result.data_model = MoveTaskResponse(
        position=queue_bll.reposition_task(
            company_id=company_id,
            queue_id=request.queue,
            task_id=request.task,
            move_count=MOVE_LAST,
        )
    )


@endpoint(
    "queues.get_queue_metrics",
    min_version="2.4",
    response_data_model=GetMetricsResponse,
)
def get_queue_metrics(
    call: APICall, company_id, request: GetMetricsRequest
) -> GetMetricsResponse:
    ret = queue_bll.metrics.get_queue_metrics(
        company_id=company_id,
        from_date=request.from_date,
        to_date=request.to_date,
        interval=request.interval,
        queue_ids=request.queue_ids,
        refresh=request.refresh,
    )

    queue_dicts = {
        queue: extract_properties_to_lists(
            ["date", "avg_waiting_time", "queue_length"], data
        )
        for queue, data in ret.items()
    }
    return GetMetricsResponse(
        queues=[
            QueueMetrics(
                queue=queue,
                dates=data["date"],
                avg_waiting_times=data["avg_waiting_time"],
                queue_lengths=data["queue_length"],
            )
            if data
            else QueueMetrics(queue=queue)
            for queue, data in queue_dicts.items()
        ]
    )


@endpoint("queues.add_or_update_metadata", min_version="2.13")
def add_or_update_metadata(
    call: APICall, company_id: str, request: AddOrUpdateMetadataRequest
):
    queue_id = request.queue
    queue = queue_bll.get_by_id(company_id=company_id, queue_id=queue_id, only=("id",))
    return {
        "updated": Metadata.edit_metadata(
            queue, items=request.metadata, replace_metadata=request.replace_metadata
        )
    }


@endpoint("queues.delete_metadata", min_version="2.13")
def delete_metadata(call: APICall, company_id: str, request: DeleteMetadataRequest):
    queue_id = request.queue
    queue = queue_bll.get_by_id(company_id=company_id, queue_id=queue_id, only=("id",))
    return {"updated": Metadata.delete_metadata(queue, keys=request.keys)}


@endpoint("queues.peek_task", min_version="2.15")
def peek_task(call: APICall, company_id: str, request: QueueRequest):
    queue_id = request.queue
    queue = queue_bll.get_by_id(
        company_id=company_id, queue_id=queue_id, max_task_entries=1
    )
    return {"task": queue.entries[0].task if queue.entries else None}


@endpoint("queues.get_num_entries", min_version="2.15")
def get_num_entries(call: APICall, company_id: str, request: QueueRequest):
    return {"num": queue_bll.count_entries(company=company_id, queue_id=request.queue)}
