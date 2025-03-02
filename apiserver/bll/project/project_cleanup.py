from collections import defaultdict
from datetime import datetime
from typing import Tuple, Set, Sequence

import attr
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.bll.event import EventBLL
from apiserver.bll.task.task_cleanup import (
    TaskUrls,
    schedule_for_delete,
    delete_task_events_and_collect_urls,
)
from apiserver.config_repo import config
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, ArtifactModes, TaskType, TaskStatus
from .project_bll import (
    ProjectBLL,
    pipeline_tag,
    pipelines_project_name,
    dataset_tag,
    datasets_project_name,
    reports_tag,
)
from .sub_projects import _ids_with_children

log = config.logger(__file__)
event_bll = EventBLL()


@attr.s(auto_attribs=True)
class DeleteProjectResult:
    deleted: int = 0
    disassociated_tasks: int = 0
    deleted_models: int = 0
    deleted_tasks: int = 0
    urls: TaskUrls = None


def _get_child_project_ids(
    project_id: str,
) -> Tuple[Sequence[str], Sequence[str], Sequence[str]]:
    project_ids = _ids_with_children([project_id])
    pipeline_ids = list(
        Project.objects(
            id__in=project_ids,
            system_tags__in=[pipeline_tag],
            basename__ne=pipelines_project_name,
        ).scalar("id")
    )
    dataset_ids = list(
        Project.objects(
            id__in=project_ids,
            system_tags__in=[dataset_tag],
            basename__ne=datasets_project_name,
        ).scalar("id")
    )
    return project_ids, pipeline_ids, dataset_ids


def validate_project_delete(company: str, project_id: str):
    project = Project.get_for_writing(
        company=company, id=project_id, _only=("id", "path", "system_tags")
    )
    if not project:
        raise errors.bad_request.InvalidProjectId(id=project_id)

    project_ids, pipeline_ids, dataset_ids = _get_child_project_ids(project_id)
    ret = {}
    if pipeline_ids:
        pipelines_with_active_controllers = Task.objects(
            project__in=pipeline_ids,
            type=TaskType.controller,
            system_tags__nin=[EntityVisibility.archived.value],
        ).distinct("project")
        ret["pipelines"] = len(pipelines_with_active_controllers)
    else:
        ret["pipelines"] = 0
    if dataset_ids:
        datasets_with_data = Task.objects(
            project__in=dataset_ids,
            system_tags__nin=[EntityVisibility.archived.value],
        ).distinct("project")
        ret["datasets"] = len(datasets_with_data)
    else:
        ret["datasets"] = 0

    project_ids = list(set(project_ids) - set(pipeline_ids) - set(dataset_ids))
    if project_ids:
        in_project_query = Q(project__in=project_ids)
        for cls in (Task, Model):
            query = (
                in_project_query & Q(system_tags__nin=[reports_tag])
                if cls is Task
                else in_project_query
            )
            ret[f"{cls.__name__.lower()}s"] = cls.objects(query).count()
            ret[f"non_archived_{cls.__name__.lower()}s"] = cls.objects(
                query & Q(system_tags__nin=[EntityVisibility.archived.value])
            ).count()
        ret["reports"] = Task.objects(
            in_project_query & Q(system_tags__in=[reports_tag])
        ).count()
        ret["non_archived_reports"] = Task.objects(
            in_project_query
            & Q(
                system_tags__in=[reports_tag],
                system_tags__nin=[EntityVisibility.archived.value],
            )
        ).count()
    else:
        for cls in (Task, Model):
            ret[f"{cls.__name__.lower()}s"] = 0
            ret[f"non_archived_{cls.__name__.lower()}s"] = 0
        ret["reports"] = 0
        ret["non_archived_reports"] = 0

    return ret


def delete_project(
    company: str,
    user: str,
    project_id: str,
    force: bool,
    delete_contents: bool,
    delete_external_artifacts: bool,
) -> Tuple[DeleteProjectResult, Set[str]]:
    project = Project.get_for_writing(
        company=company, id=project_id, _only=("id", "path", "system_tags")
    )
    if not project:
        raise errors.bad_request.InvalidProjectId(id=project_id)

    delete_external_artifacts = delete_external_artifacts and config.get(
        "services.async_urls_delete.enabled", True
    )
    project_ids, pipeline_ids, dataset_ids = _get_child_project_ids(project_id)
    if not force:
        if pipeline_ids:
            active_controllers = Task.objects(
                project__in=pipeline_ids,
                type=TaskType.controller,
                system_tags__nin=[EntityVisibility.archived.value],
            ).only("id")
            if active_controllers:
                raise errors.bad_request.ProjectHasPipelines(
                    "please archive all the controllers or use force=true",
                    id=project_id,
                )
        if dataset_ids:
            datasets_with_data = Task.objects(
                project__in=dataset_ids,
                system_tags__nin=[EntityVisibility.archived.value],
            ).only("id")
            if datasets_with_data:
                raise errors.bad_request.ProjectHasDatasets(
                    "please delete all the dataset versions or use force=true",
                    id=project_id,
                )

        regular_projects = list(set(project_ids) - set(pipeline_ids) - set(dataset_ids))
        if regular_projects:
            for cls, error in (
                (Task, errors.bad_request.ProjectHasTasks),
                (Model, errors.bad_request.ProjectHasModels),
            ):
                non_archived = cls.objects(
                    project__in=regular_projects,
                    system_tags__nin=[EntityVisibility.archived.value],
                ).only("id")
                if non_archived:
                    raise error("use force=true", id=project_id)

    if not delete_contents:
        disassociated = defaultdict(int)
        for cls in ProjectBLL.child_classes:
            disassociated[cls] = cls.objects(project__in=project_ids).update(
                project=None
            )
        res = DeleteProjectResult(disassociated_tasks=disassociated[Task])
    else:
        deleted_models, model_event_urls, model_urls = _delete_models(
            company=company, user=user, projects=project_ids
        )
        deleted_tasks, task_event_urls, artifact_urls = _delete_tasks(
            company=company, user=user, projects=project_ids
        )
        event_urls = task_event_urls | model_event_urls
        if delete_external_artifacts:
            scheduled = schedule_for_delete(
                task_id=project_id,
                company=company,
                user=user,
                urls=event_urls | model_urls | artifact_urls,
                can_delete_folders=True,
            )
            for urls in (event_urls, model_urls, artifact_urls):
                urls.difference_update(scheduled)
        res = DeleteProjectResult(
            deleted_tasks=deleted_tasks,
            deleted_models=deleted_models,
            urls=TaskUrls(
                model_urls=list(model_urls),
                artifact_urls=list(artifact_urls),
            ),
        )

    affected = {*project_ids, *(project.path or [])}
    res.deleted = Project.objects(id__in=project_ids).delete()

    return res, affected


def _delete_tasks(
    company: str, user: str, projects: Sequence[str]
) -> Tuple[int, Set, Set]:
    """
    Delete only the task themselves and their non published version.
    Child models under the same project are deleted separately.
    Children tasks should be deleted in the same api call.
    If any child entities are left in another projects then updated their parent task to None
    """
    tasks = Task.objects(project__in=projects).only("id", "execution__artifacts")
    if not tasks:
        return 0, set(), set()

    task_ids = list({t.id for t in tasks})
    now = datetime.utcnow()
    Task.objects(parent__in=task_ids, project__nin=projects).update(
        parent=None,
        last_change=now,
        last_changed_by=user,
    )
    Model.objects(task__in=task_ids, project__nin=projects).update(
        task=None,
        last_change=now,
        last_changed_by=user,
    )

    artifact_urls = set()
    for task in tasks:
        if task.execution and task.execution.artifacts:
            artifact_urls.update(
                {
                    a.uri
                    for a in task.execution.artifacts.values()
                    if a.mode == ArtifactModes.output and a.uri
                }
            )

    event_urls = delete_task_events_and_collect_urls(
        company=company, task_ids=task_ids, wait_for_delete=False
    )
    deleted = tasks.delete()

    return deleted, event_urls, artifact_urls


def _delete_models(
    company: str, user: str, projects: Sequence[str]
) -> Tuple[int, Set[str], Set[str]]:
    """
    Delete project models and update the tasks from other projects
    that reference them to reference None.
    """
    models = Model.objects(project__in=projects).only("task", "id", "uri")
    if not models:
        return 0, set(), set()

    model_ids = list({m.id for m in models})
    deleted = "__DELETED__"
    Task._get_collection().update_many(
        filter={
            "project": {"$nin": projects},
            "models.input.model": {"$in": model_ids},
        },
        update={"$set": {"models.input.$[elem].model": deleted}},
        array_filters=[{"elem.model": {"$in": model_ids}}],
        upsert=False,
    )

    model_tasks = list({m.task for m in models if m.task})
    if model_tasks:
        now = datetime.utcnow()
        # update published tasks
        Task._get_collection().update_many(
            filter={
                "_id": {"$in": model_tasks},
                "project": {"$nin": projects},
                "models.output.model": {"$in": model_ids},
                "status": TaskStatus.published,
            },
            update={
                "$set": {
                    "models.output.$[elem].model": deleted,
                    "last_change": now,
                    "last_changed_by": user,
                }
            },
            array_filters=[{"elem.model": {"$in": model_ids}}],
            upsert=False,
        )
        # update unpublished tasks
        Task.objects(
            id__in=model_tasks,
            project__nin=projects,
            status__ne=TaskStatus.published,
        ).update(
            pull__models__output__model__in=model_ids,
            set__last_change=now,
            set__last_changed_by=user,
        )

    model_urls = {m.uri for m in models if m.uri}
    event_urls = delete_task_events_and_collect_urls(
        company=company, task_ids=model_ids, model=True, wait_for_delete=False
    )
    deleted = models.delete()

    return deleted, event_urls, model_urls
