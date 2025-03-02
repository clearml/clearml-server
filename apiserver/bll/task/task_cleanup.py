from datetime import datetime
from itertools import chain
from operator import attrgetter
from typing import Sequence, Set, Tuple, Union

import attr
from boltons.iterutils import partition, bucketize, first, chunked_iter
from furl import furl
from mongoengine import NotUniqueError
from pymongo.errors import DuplicateKeyError

from apiserver.apierrors import errors
from apiserver.bll.event import EventBLL
from apiserver.bll.event.event_bll import PlotFields
from apiserver.bll.task.utils import deleted_prefix
from apiserver.config_repo import config
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task, TaskStatus, ArtifactModes
from apiserver.database.model.url_to_delete import (
    StorageType,
    UrlToDelete,
    FileType,
    DeletionStatus,
)
from apiserver.database.utils import id as db_id

log = config.logger(__file__)
event_bll = EventBLL()


@attr.s(auto_attribs=True)
class TaskUrls:
    model_urls: Sequence[str]
    artifact_urls: Sequence[str]
    event_urls: Sequence[str] = []  # left here is in order not to break the api

    def __add__(self, other: "TaskUrls"):
        if not other:
            return self

        return TaskUrls(
            model_urls=list(set(self.model_urls) | set(other.model_urls)),
            artifact_urls=list(set(self.artifact_urls) | set(other.artifact_urls)),
        )


@attr.s(auto_attribs=True)
class CleanupResult:
    """
    Counts of objects modified in task cleanup operation
    """

    updated_children: int
    updated_models: int
    deleted_models: int
    deleted_model_ids: Set[str]
    urls: TaskUrls = None

    def to_res_dict(self, return_file_urls: bool) -> dict:
        remove_fields = ["deleted_model_ids"]
        if not return_file_urls:
            remove_fields.append("urls")

        # noinspection PyTypeChecker
        res = attr.asdict(
            self, filter=lambda attrib, value: attrib.name not in remove_fields
        )
        if not return_file_urls:
            res["urls"] = None

        return res

    def __add__(self, other: "CleanupResult"):
        if not other:
            return self

        return CleanupResult(
            updated_children=self.updated_children + other.updated_children,
            updated_models=self.updated_models + other.updated_models,
            deleted_models=self.deleted_models + other.deleted_models,
            urls=self.urls + other.urls if self.urls else other.urls,
            deleted_model_ids=self.deleted_model_ids | other.deleted_model_ids,
        )

    @staticmethod
    def empty():
        return CleanupResult(
            updated_children=0,
            updated_models=0,
            deleted_models=0,
            deleted_model_ids=set(),
        )


def collect_plot_image_urls(
    company: str, task_or_model: Union[str, Sequence[str]]
) -> Set[str]:
    urls = set()
    task_ids = task_or_model if isinstance(task_or_model, list) else [task_or_model]
    for tasks in chunked_iter(task_ids, 100):
        next_scroll_id = None
        while True:
            events, next_scroll_id = event_bll.get_plot_image_urls(
                company_id=company, task_ids=tasks, scroll_id=next_scroll_id
            )
            if not events:
                break
            for event in events:
                event_urls = event.get(PlotFields.source_urls)
                if event_urls:
                    urls.update(set(event_urls))

    return urls


def collect_debug_image_urls(
    company: str, task_or_model: Union[str, Sequence[str]]
) -> Set[str]:
    """
    Return the set of unique image urls
    Uses DebugImagesIterator to make sure that we do not retrieve recycled urls
    """
    urls = set()
    task_ids = task_or_model if isinstance(task_or_model, list) else [task_or_model]
    for tasks in chunked_iter(task_ids, 100):
        after_key = None
        while True:
            res, after_key = event_bll.get_debug_image_urls(
                company_id=company,
                task_ids=tasks,
                after_key=after_key,
            )
            urls.update(res)
            if not after_key:
                break

    return urls


supported_storage_types = {
    "s3://": StorageType.s3,
    "azure://": StorageType.azure,
    "gs://": StorageType.gs,
}

supported_storage_types.update(
    {
        p: StorageType.fileserver
        for p in config.get(
            "services.async_urls_delete.fileserver.url_prefixes",
            ["https://", "http://"],
        )
    }
)


def schedule_for_delete(
    company: str,
    user: str,
    task_id: str,
    urls: Set[str],
    can_delete_folders: bool,
) -> Set[str]:
    urls_per_storage = bucketize(
        urls,
        key=lambda u: first(
            type_
            for prefix, type_ in supported_storage_types.items()
            if u.startswith(prefix)
        ),
    )
    urls_per_storage.pop(None, None)

    processed_urls = set()
    for storage_type, storage_urls in urls_per_storage.items():
        delete_folders = (storage_type == StorageType.fileserver) and can_delete_folders
        scheduled_to_delete = set()
        for url in storage_urls:
            folder = None
            if delete_folders:
                try:
                    parsed = furl(url)
                    if parsed.path and len(parsed.path.segments) > 1:
                        folder = parsed.remove(
                            args=True, fragment=True, path=parsed.path.segments[-1]
                        ).url.rstrip("/")
                except Exception as ex:
                    pass

            to_delete = folder or url
            if to_delete in scheduled_to_delete:
                processed_urls.add(url)
                continue

            try:
                UrlToDelete(
                    id=db_id(),
                    company=company,
                    user=user,
                    url=to_delete,
                    task=task_id,
                    created=datetime.utcnow(),
                    storage_type=storage_type,
                    type=FileType.folder if folder else FileType.file,
                ).save()
            except (DuplicateKeyError, NotUniqueError):
                existing = UrlToDelete.objects(company=company, url=to_delete).first()
                if existing:
                    existing.update(
                        user=user,
                        task=task_id,
                        created=datetime.utcnow(),
                        retry_count=0,
                        unset__last_failure_time=1,
                        unset__last_failure_reason=1,
                        status=DeletionStatus.created,
                    )
            processed_urls.add(url)
            scheduled_to_delete.add(to_delete)

    return processed_urls


def delete_task_events_and_collect_urls(
    company: str, task_ids: Sequence[str], wait_for_delete: bool, model=False
) -> Set[str]:
    event_urls = collect_debug_image_urls(company, task_ids) | collect_plot_image_urls(
        company, task_ids
    )

    event_bll.delete_task_events(
        company, task_ids, model=model, wait_for_delete=wait_for_delete
    )

    return event_urls


def cleanup_task(
    company: str,
    user: str,
    task: Task,
    force: bool = False,
    update_children=True,
    delete_output_models=True,
) -> CleanupResult:
    """
    Validate task deletion and delete/modify all its output.
    :param task: task object
    :param force: whether to delete task with published outputs
    :return: count of delete and modified items
    """
    published_models, draft_models, in_use_model_ids = verify_task_children_and_ouptuts(
        task, force
    )
    artifact_urls = (
        {
            a.uri
            for a in task.execution.artifacts.values()
            if a.mode == ArtifactModes.output and a.uri
        }
        if task.execution and task.execution.artifacts
        else {}
    )
    model_urls = {m.uri for m in draft_models if m.uri and m.id not in in_use_model_ids}

    deleted_task_id = f"{deleted_prefix}{task.id}"
    updated_children = 0
    now = datetime.utcnow()
    if update_children:
        updated_children = Task.objects(parent=task.id).update(
            parent=deleted_task_id,
            last_change=now,
            last_changed_by=user,
        )

    deleted_models = 0
    updated_models = 0
    deleted_model_ids = set()
    for models, allow_delete in ((draft_models, True), (published_models, False)):
        if not models:
            continue
        if delete_output_models and allow_delete:
            model_ids = list({m.id for m in models if m.id not in in_use_model_ids})
            if model_ids:
                deleted_models += Model.objects(id__in=model_ids).delete()
                deleted_model_ids.update(model_ids)

            if in_use_model_ids:
                Model.objects(id__in=list(in_use_model_ids)).update(
                    unset__task=1,
                    set__last_change=now,
                    set__last_changed_by=user,
                )
            continue

        if update_children:
            updated_models += Model.objects(id__in=[m.id for m in models]).update(
                task=deleted_task_id,
                last_change=now,
                last_changed_by=user,
            )
        else:
            Model.objects(id__in=[m.id for m in models]).update(
                unset__task=1,
                set__last_change=now,
                set__last_changed_by=user,
            )

    return CleanupResult(
        deleted_models=deleted_models,
        updated_children=updated_children,
        updated_models=updated_models,
        urls=TaskUrls(
            artifact_urls=list(artifact_urls),
            model_urls=list(model_urls),
        ),
        deleted_model_ids=deleted_model_ids,
    )


def verify_task_children_and_ouptuts(
    task, force: bool
) -> Tuple[Sequence[Model], Sequence[Model], Set[str]]:
    if not force:
        published_children_count = Task.objects(
            parent=task.id, status=TaskStatus.published
        ).count()
        if published_children_count:
            raise errors.bad_request.TaskCannotBeDeleted(
                "has children, use force=True",
                task=task.id,
                children=published_children_count,
            )

    model_fields = ["id", "ready", "uri"]
    published_models, draft_models = partition(
        Model.objects(task=task.id).only(*model_fields),
        key=attrgetter("ready"),
    )
    if not force and published_models:
        raise errors.bad_request.TaskCannotBeDeleted(
            "has output models, use force=True",
            task=task.id,
            models=len(published_models),
        )

    if task.models and task.models.output:
        model_ids = [m.model for m in task.models.output]
        for output_model in Model.objects(id__in=model_ids).only(*model_fields):
            if output_model.ready:
                if not force:
                    raise errors.bad_request.TaskCannotBeDeleted(
                        "has output model, use force=True",
                        task=task.id,
                        model=output_model.id,
                    )
                published_models.append(output_model)
            else:
                draft_models.append(output_model)

    in_use_model_ids = {}
    if draft_models:
        model_ids = {m.id for m in draft_models}
        dependent_tasks = Task.objects(models__input__model__in=list(model_ids)).only(
            "id", "models"
        )
        in_use_model_ids = model_ids & {
            m.model
            for m in chain.from_iterable(
                t.models.input for t in dependent_tasks if t.models
            )
        }

    return published_models, draft_models, in_use_model_ids
