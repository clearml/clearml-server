from datetime import datetime
from typing import Callable, Any, Tuple, Union, Sequence

from apiserver.apierrors import errors, APIError
from apiserver.bll.queue import QueueBLL
from apiserver.bll.task import (
    TaskBLL,
    validate_status_change,
    ChangeStatusRequest,
)
from apiserver.bll.task.task_cleanup import cleanup_task, CleanupResult
from apiserver.bll.task.utils import get_task_with_write_access
from apiserver.bll.util import update_project_time
from apiserver.config_repo import config
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.task.output import Output
from apiserver.database.model.task.task import (
    TaskStatus,
    Task,
    TaskSystemTags,
    TaskStatusMessage,
    ArtifactModes,
    Execution,
    DEFAULT_LAST_ITERATION,
    TaskType,
)
from apiserver.database.utils import get_options
from apiserver.service_repo.auth import Identity
from apiserver.utilities.dicts import nested_set

log = config.logger(__file__)
queue_bll = QueueBLL()


def _get_pipeline_steps_for_controller_task(
    task: Task, company_id: str, only: Sequence[str] = None
) -> Sequence[Task]:
    if not task or task.type != TaskType.controller:
        return []

    query = Task.objects(company=company_id, parent=task.id)
    if only:
        query = query.only(*only)

    return list(query)


def archive_task(
    task: Union[str, Task],
    company_id: str,
    identity: Identity,
    status_message: str,
    status_reason: str,
    include_pipeline_steps: bool,
) -> int:
    """
    Deque and archive task
    Return 1 if successful
    """
    user_id = identity.user
    fields = (
        "id",
        "company",
        "execution",
        "status",
        "project",
        "system_tags",
        "enqueue_status",
        "type",
    )
    if isinstance(task, str):
        task = get_task_with_write_access(
            task,
            company_id=company_id,
            identity=identity,
            only=fields,
        )

    def archive_task_core(task_: Task) -> int:
        try:
            TaskBLL.dequeue_and_change_status(
                task_,
                company_id=company_id,
                user_id=user_id,
                status_message=status_message,
                status_reason=status_reason,
                remove_from_all_queues=True,
                new_status_for_aborted_task=TaskStatus.stopped,
            )
        except APIError:
            # dequeue may fail if the task was not enqueued
            pass

        return task_.update(
            status_message=status_message,
            status_reason=status_reason,
            add_to_set__system_tags=EntityVisibility.archived.value,
            last_change=datetime.utcnow(),
            last_changed_by=user_id,
        )

    if include_pipeline_steps and (
        step_tasks := _get_pipeline_steps_for_controller_task(
            task, company_id, only=fields
        )
    ):
        for step in step_tasks:
            archive_task_core(step)

    return archive_task_core(task)


def unarchive_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    status_message: str,
    status_reason: str,
    include_pipeline_steps: bool,
) -> int:
    """
    Unarchive task. Return 1 if successful
    """
    fields = ("id", "type")
    task = get_task_with_write_access(
        task_id,
        company_id=company_id,
        identity=identity,
        only=fields,
    )

    def unarchive_task_core(task_: Task) -> int:
        return task_.update(
            status_message=status_message,
            status_reason=status_reason,
            pull__system_tags=EntityVisibility.archived.value,
            last_change=datetime.utcnow(),
            last_changed_by=identity.user,
        )

    if include_pipeline_steps and (
        step_tasks := _get_pipeline_steps_for_controller_task(
            task, company_id, only=fields
        )
    ):
        for step in step_tasks:
            unarchive_task_core(step)

    return unarchive_task_core(task)


def dequeue_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    status_message: str,
    status_reason: str,
    remove_from_all_queues: bool = False,
    new_status=None,
) -> Tuple[int, dict]:
    if new_status and new_status not in get_options(TaskStatus):
        raise errors.bad_request.ValidationError(f"Invalid task status: {new_status}")

    # get the task without write access to make sure that it actually exists
    task = Task.get(
        id=task_id,
        company=company_id,
        _only=("id",),
        include_public=True,
    )
    if not task:
        TaskBLL.remove_task_from_all_queues(company_id, task_id=task_id)
        return 1, {"updated": 0}

    user_id = identity.user
    task = get_task_with_write_access(
        task_id,
        company_id=company_id,
        identity=identity,
        only=(
            "id",
            "company",
            "execution",
            "status",
            "project",
            "enqueue_status",
        ),
    )

    res = TaskBLL.dequeue_and_change_status(
        task,
        company_id=company_id,
        user_id=user_id,
        status_message=status_message,
        status_reason=status_reason,
        remove_from_all_queues=remove_from_all_queues,
        new_status=new_status,
    )
    return 1, res


def enqueue_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    queue_id: str,
    status_message: str,
    status_reason: str,
    queue_name: str = None,
    validate: bool = False,
    force: bool = False,
    update_execution_queue: bool = True,
) -> Tuple[int, dict]:
    if queue_id and queue_name:
        raise errors.bad_request.ValidationError(
            "Either queue id or queue name should be provided"
        )

    task = get_task_with_write_access(
        task_id=task_id, company_id=company_id, identity=identity
    )
    if not update_execution_queue:
        if not (
            task.status == TaskStatus.queued and task.execution and task.execution.queue
        ):
            raise errors.bad_request.ValidationError(
                "Cannot skip setting execution queue for a task "
                "that is not enqueued or does not have execution queue set"
            )

    if queue_name:
        queue = queue_bll.get_by_name(
            company_id=company_id, queue_name=queue_name, only=("id",)
        )
        if not queue:
            queue = queue_bll.create(company_id=company_id, name=queue_name)
        queue_id = queue.id

    if not queue_id:
        # try to get default queue
        queue_id = queue_bll.get_default(company_id).id

    user_id = identity.user
    if validate:
        TaskBLL.validate(task)

    before_enqueue_status = task.status
    if task.status == TaskStatus.queued and task.enqueue_status:
        before_enqueue_status = task.enqueue_status
    res = ChangeStatusRequest(
        task=task,
        new_status=TaskStatus.queued,
        status_reason=status_reason,
        status_message=status_message,
        force=force,
        user_id=user_id,
    ).execute(enqueue_status=before_enqueue_status)

    try:
        queue_bll.add_task(company_id=company_id, queue_id=queue_id, task_id=task.id)
    except Exception:
        # failed enqueueing, revert to previous state
        ChangeStatusRequest(
            task=task,
            current_status_override=TaskStatus.queued,
            new_status=task.status,
            force=True,
            status_reason="failed enqueueing",
            user_id=user_id,
        ).execute(enqueue_status=None)
        raise

    # set the current queue ID in the task
    if update_execution_queue:
        if task.execution:
            Task.objects(id=task_id).update(execution__queue=queue_id, multi=False)
        else:
            Task.objects(id=task_id).update(
                execution=Execution(queue=queue_id), multi=False
            )
        nested_set(res, ("fields", "execution.queue"), queue_id)

    # make sure that the task is not queued in any other queue
    TaskBLL.remove_task_from_all_queues(
        company_id=company_id, task_id=task_id, exclude=queue_id
    )
    return 1, res


def move_tasks_to_trash(tasks: Sequence[str]) -> int:
    try:
        collection_name = Task._get_collection_name()
        trash_collection_name = f"{collection_name}__trash"
        Task.aggregate(
            [
                {"$match": {"_id": {"$in": tasks}}},
                {
                    "$merge": {
                        "into": trash_collection_name,
                        "on": "_id",
                        "whenMatched": "replace",
                        "whenNotMatched": "insert",
                    }
                },
            ],
            allow_disk_use=True,
        )
    except Exception as ex:
        log.error(f"Error copying tasks to trash {str(ex)}")

    return Task.objects(id__in=tasks).delete()


def delete_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    move_to_trash: bool,
    force: bool,
    delete_output_models: bool,
    status_message: str,
    status_reason: str,
    include_pipeline_steps: bool,
) -> Tuple[int, Task, CleanupResult]:
    user_id = identity.user
    task = get_task_with_write_access(task_id, company_id=company_id, identity=identity)

    if (
        task.status != TaskStatus.created
        and EntityVisibility.archived.value not in task.system_tags
        and not force
    ):
        raise errors.bad_request.TaskCannotBeDeleted(
            "due to status, use force=True",
            task=task.id,
            expected=TaskStatus.created,
            current=task.status,
        )

    def delete_task_core(task_: Task, force_: bool) -> CleanupResult:
        try:
            TaskBLL.dequeue_and_change_status(
                task_,
                company_id=company_id,
                user_id=user_id,
                status_message=status_message,
                status_reason=status_reason,
                remove_from_all_queues=True,
            )
        except APIError:
            # dequeue may fail if the task was not enqueued
            pass

        res = cleanup_task(
            company=company_id,
            user=user_id,
            task=task_,
            force=force_,
            delete_output_models=delete_output_models,
        )

        if move_to_trash:
            # make sure that whatever changes were done to the task are saved
            # the task itself will be deleted later in the move_tasks_to_trash operation
            task_.last_update = datetime.utcnow()
            task_.save()
        else:
            task_.delete()

        return res

    task_ids = [task.id]
    cleanup_res = CleanupResult.empty()
    if include_pipeline_steps and (
        step_tasks := _get_pipeline_steps_for_controller_task(task, company_id)
    ):
        for step in step_tasks:
            cleanup_res += delete_task_core(step, True)
            task_ids.append(step.id)

    cleanup_res = delete_task_core(task, force)
    if move_to_trash:
        move_tasks_to_trash(task_ids)

    update_project_time(task.project)
    return 1, task, cleanup_res


def reset_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    force: bool,
    delete_output_models: bool,
    clear_all: bool,
) -> Tuple[dict, CleanupResult, dict]:
    user_id = identity.user
    task = get_task_with_write_access(task_id, company_id=company_id, identity=identity)

    if not force and task.status == TaskStatus.published:
        raise errors.bad_request.InvalidTaskStatus(task_id=task.id, status=task.status)

    dequeued = {}
    updates = {}

    try:
        dequeued = TaskBLL.dequeue(
            task, company_id=company_id, user_id=user_id, silent_fail=True
        )
    except APIError:
        # dequeue may fail if the task was not enqueued
        pass

    TaskBLL.remove_task_from_all_queues(company_id=company_id, task_id=task.id)

    cleaned_up = cleanup_task(
        company=company_id,
        user=user_id,
        task=task,
        force=force,
        update_children=False,
        delete_output_models=delete_output_models,
    )

    updates.update(
        set__last_iteration=DEFAULT_LAST_ITERATION,
        set__last_metrics={},
        set__unique_metrics=[],
        set__metric_stats={},
        set__models__output=[],
        set__runtime={},
        unset__output__result=1,
        unset__output__error=1,
        unset__last_worker=1,
        unset__last_worker_report=1,
        unset__started=1,
        unset__completed=1,
        unset__published=1,
        unset__active_duration=1,
        unset__enqueue_status=1,
    )

    if clear_all:
        updates.update(
            set__execution=Execution(),
            unset__script=1,
        )
    else:
        updates.update(unset__execution__queue=1)
        if task.execution and task.execution.artifacts:
            updates.update(
                set__execution__artifacts={
                    key: artifact
                    for key, artifact in task.execution.artifacts.items()
                    if artifact.mode == ArtifactModes.input
                }
            )

    res = ChangeStatusRequest(
        task=task,
        new_status=TaskStatus.created,
        force=force,
        status_reason="reset",
        status_message="reset",
        user_id=user_id,
    ).execute(
        **updates,
    )

    return dequeued, cleaned_up, res


def publish_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    force: bool,
    publish_model_func: Callable[[str, str, Identity], Any] = None,
    status_message: str = "",
    status_reason: str = "",
) -> dict:
    user_id = identity.user
    task = get_task_with_write_access(task_id, company_id=company_id, identity=identity)
    if not force:
        validate_status_change(task.status, TaskStatus.published)

    previous_task_status = task.status
    output = task.output or Output()
    publish_failed = False

    try:
        # set state to publishing
        task.status = TaskStatus.publishing
        task.save()

        # publish task models
        if task.models and task.models.output and publish_model_func:
            model_id = task.models.output[-1].model
            model = (
                Model.objects(id=model_id, company=company_id)
                .only("id", "ready")
                .first()
            )
            if model and not model.ready:
                publish_model_func(model.id, company_id, identity)

        # set task status to published, and update (or set) it's new output (view and models)
        return ChangeStatusRequest(
            task=task,
            new_status=TaskStatus.published,
            force=force,
            status_reason=status_reason,
            status_message=status_message,
            user_id=user_id,
        ).execute(published=datetime.utcnow(), output=output)

    except Exception as ex:
        publish_failed = True
        raise ex
    finally:
        if publish_failed:
            task.status = previous_task_status
            task.save()


def stop_task(
    task_id: str,
    company_id: str,
    identity: Identity,
    user_name: str,
    status_reason: str,
    force: bool,
    include_pipeline_steps: bool,
) -> dict:
    """
    Stop a running task. Requires task status 'in_progress' and
    execution_progress 'running', or force=True. Development task or
    task that has no associated worker is stopped immediately.
    For a non-development task with worker only the status message
    is set to 'stopping' to allow the worker to stop the task and report by itself
    :return: updated task fields
    """
    user_id = identity.user
    fields = (
        "status",
        "project",
        "tags",
        "system_tags",
        "last_worker",
        "last_update",
        "execution.queue",
        "type",
    )
    task = get_task_with_write_access(
        task_id,
        company_id=company_id,
        identity=identity,
        only=fields,
    )

    def is_run_by_worker(t: Task) -> bool:
        """Checks if there is an active worker running the task"""
        update_timeout = config.get("apiserver.workers.task_update_timeout", 600)
        return (
            t.last_worker
            and t.last_update
            and (datetime.utcnow() - t.last_update).total_seconds() < update_timeout
        )

    def stop_task_core(task_: Task, force_: bool):
        is_queued = task_.status == TaskStatus.queued
        set_stopped = (
            is_queued
            or TaskSystemTags.development in task_.system_tags
            or not is_run_by_worker(task_)
        )

        if set_stopped:
            if is_queued:
                try:
                    TaskBLL.dequeue(
                        task_, company_id=company_id, user_id=user_id, silent_fail=True
                    )
                except APIError:
                    # dequeue may fail if the task was not enqueued
                    pass

            new_status = TaskStatus.stopped
            status_message = f"Stopped by {user_name}"
        else:
            new_status = task_.status
            status_message = TaskStatusMessage.stopping

        return ChangeStatusRequest(
            task=task_,
            new_status=new_status,
            status_reason=status_reason,
            status_message=status_message,
            force=force_,
            user_id=user_id,
        ).execute()

    if include_pipeline_steps and (
        step_tasks := _get_pipeline_steps_for_controller_task(
            task, company_id, only=fields
        )
    ):
        for step in step_tasks:
            stop_task_core(step, True)

    return stop_task_core(task, force)
