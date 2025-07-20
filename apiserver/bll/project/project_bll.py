from datetime import datetime, timedelta
from functools import reduce
from itertools import chain
from operator import itemgetter
from typing import (
    Sequence,
    Optional,
    Type,
    Tuple,
    Dict,
    Set,
    TypeVar,
    Callable,
    Mapping,
    Any,
    Union,
)

from mongoengine import Q, Document

from apiserver import database
from apiserver.apierrors import errors
from apiserver.apimodels.projects import ProjectChildrenType
from apiserver.config_repo import config
from apiserver.database.model import EntityVisibility, AttributedDocument, User
from apiserver.database.model.base import GetMixin
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskStatus, external_task_types
from apiserver.database.utils import get_options, get_company_or_none_constraint
from .sub_projects import (
    _reposition_project_with_children,
    _ensure_project,
    _validate_project_name,
    _update_subproject_names,
    _save_under_parent,
    _get_sub_projects,
    _ids_with_children,
    _ids_with_parents,
    _get_project_depth,
    ProjectsChildren,
    _get_writable_project_from_name,
    _get_basename_from_name,
)

log = config.logger(__file__)
max_depth = config.get("services.projects.sub_projects.max_depth", 10)
reports_project_name = ".reports"
datasets_project_name = ".datasets"
pipelines_project_name = ".pipelines"
reports_tag = "reports"
dataset_tag = "dataset"
pipeline_tag = "pipeline"


class ProjectBLL:
    child_classes = (Task, Model)

    @classmethod
    def merge_project(
        cls, company: str, source_id: str, destination_id: str
    ) -> Tuple[int, int, Set[str]]:
        """
        Move all the tasks and sub projects from the source project to the destination
        Remove the source project
        Return the amounts of moved entities and subprojects + set of all the affected project ids
        """
        if source_id == destination_id:
            raise errors.bad_request.ProjectSourceAndDestinationAreTheSame(
                source=source_id
            )
        source = Project.get(company, source_id)
        if destination_id:
            destination = Project.get(company, destination_id)
            if source_id in destination.path:
                raise errors.bad_request.ProjectCannotBeMergedIntoItsChild(
                    source=source_id, destination=destination_id
                )
        else:
            destination = None

        children = _get_sub_projects(
            [source.id], _only=("id", "name", "parent", "path")
        )[source.id]
        if destination:
            cls.validate_projects_depth(
                projects=children,
                old_parent_depth=len(source.path) + 1,
                new_parent_depth=len(destination.path) + 1,
            )

        moved_entities = 0
        for entity_type in cls.child_classes:
            moved_entities += entity_type.objects(
                company=company,
                project=source_id,
                system_tags__nin=[EntityVisibility.archived.value],
            ).update(upsert=False, project=destination_id)

        moved_sub_projects = 0
        for child in Project.objects(company=company, parent=source_id):
            _reposition_project_with_children(
                project=child,
                children=[c for c in children if c.parent == child.id],
                parent=destination,
            )
            moved_sub_projects += 1

        affected = {source.id, *(source.path or [])}
        source.delete()

        if destination:
            destination.update(last_update=datetime.utcnow())
            affected.update({destination.id, *(destination.path or [])})

        return moved_entities, moved_sub_projects, affected

    @staticmethod
    def validate_projects_depth(
        projects: Sequence[Project], old_parent_depth: int, new_parent_depth: int
    ):
        for current in projects:
            current_depth = len(current.path) + 1
            if current_depth - old_parent_depth + new_parent_depth > max_depth:
                raise errors.bad_request.ProjectPathExceedsMax(max_depth=max_depth)

    @classmethod
    def move_project(
        cls, company: str, user: str, project_id: str, new_location: str
    ) -> Tuple[int, Set[str]]:
        """
        Move project with its sub projects from its current location to the target one.
        If the target location does not exist then it will be created. If it exists then
        it should be writable. The source location should be writable too.
        Return the number of moved projects + set of all the affected project ids
        """
        project = Project.get(company, project_id)
        old_parent_id = project.parent
        old_parent = (
            Project.get_for_writing(company=project.company, id=old_parent_id)
            if old_parent_id
            else None
        )

        children = _get_sub_projects([project.id], _only=("id", "name", "path"))[
            project.id
        ]
        cls.validate_projects_depth(
            projects=[project, *children],
            old_parent_depth=len(project.path),
            new_parent_depth=_get_project_depth(new_location),
        )

        new_parent = _ensure_project(company=company, user=user, name=new_location)
        new_parent_id = new_parent.id if new_parent else None
        if old_parent_id == new_parent_id:
            raise errors.bad_request.ProjectSourceAndDestinationAreTheSame(
                location=new_parent.name if new_parent else ""
            )
        if new_parent and (
            project_id == new_parent.id or project_id in new_parent.path
        ):
            raise errors.bad_request.ProjectCannotBeMovedUnderItself(
                project=project_id, parent=new_parent.id
            )
        moved = _reposition_project_with_children(
            project, children=children, parent=new_parent
        )

        now = datetime.utcnow()
        affected = set()
        p: Project
        for p in filter(None, (old_parent, new_parent)):
            p.update(last_update=now)
            affected.update({p.id, *(p.path or [])})

        return moved, affected

    @classmethod
    def update(cls, company: str, project_id: str, **fields):
        project = Project.get_for_writing(company=company, id=project_id)
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        new_name = fields.pop("name", None)
        if new_name:
            # noinspection PyTypeChecker
            new_name, new_location = _validate_project_name(new_name)
            old_name, old_location = _validate_project_name(project.name)
            if new_location != old_location:
                raise errors.bad_request.CannotUpdateProjectLocation(name=new_name)
            fields["name"] = new_name
            fields["basename"] = _get_basename_from_name(new_name)

        fields["last_update"] = datetime.utcnow()
        updated = project.update(upsert=False, **fields)

        if new_name:
            old_name = project.name
            project.name = new_name
            children = _get_sub_projects([project.id], _only=("id", "name", "path"))[
                project.id
            ]
            _update_subproject_names(
                project=project, children=children, old_name=old_name
            )

        return updated

    @classmethod
    def create(
        cls,
        user: str,
        company: str,
        name: str,
        description: str = "",
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        default_output_destination: str = None,
        parent_creation_params: dict = None,
    ) -> str:
        """
        Create a new project.
        Returns project ID
        """
        if _get_project_depth(name) > max_depth:
            raise errors.bad_request.ProjectPathExceedsMax(max_depth=max_depth)

        name, location = _validate_project_name(name)

        existing = _get_writable_project_from_name(
            company=company,
            name=name,
        )
        if existing:
            raise errors.bad_request.ExpectedUniqueData(
                replacement_msg="Project with the same name already exists",
                name=name,
                company=company,
            )

        now = datetime.utcnow()
        project = Project(
            id=database.utils.id(),
            user=user,
            company=company,
            name=name,
            basename=_get_basename_from_name(name),
            description=description,
            tags=tags,
            system_tags=system_tags,
            default_output_destination=default_output_destination,
            created=now,
            last_update=now,
        )
        parent = _ensure_project(
            company=company,
            user=user,
            name=location,
            creation_params=parent_creation_params,
        )
        _save_under_parent(project=project, parent=parent)
        if parent:
            parent.update(last_update=now)

        return project.id

    @classmethod
    def find_or_create(
        cls,
        user: str,
        company: str,
        project_name: str,
        description: str,
        project_id: str = None,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        default_output_destination: str = None,
        parent_creation_params: dict = None,
    ) -> str:
        """
        Find a project named `project_name` or create a new one.
        Returns project ID
        """
        if not project_id and not project_name:
            raise errors.bad_request.ValidationError("project id or name required")

        if project_id:
            project = Project.objects(company=company, id=project_id).only("id").first()
            if not project:
                raise errors.bad_request.InvalidProjectId(id=project_id)
            return project_id

        project_name, _ = _validate_project_name(project_name)
        project = Project.objects(company=company, name=project_name).only("id").first()
        if project:
            return project.id

        return cls.create(
            user=user,
            company=company,
            name=project_name,
            description=description,
            tags=tags,
            system_tags=system_tags,
            default_output_destination=default_output_destination,
            parent_creation_params=parent_creation_params,
        )

    @classmethod
    def move_under_project(
        cls,
        entity_cls: Type[Document],
        user: str,
        company: str,
        ids: Sequence[str],
        project: str = None,
        project_name: str = None,
    ):
        """
        Move a batch of entities to `project` or a project named `project_name` (create if does not exist)
        """
        if project_name or project:
            project = cls.find_or_create(
                user=user,
                company=company,
                project_id=project,
                project_name=project_name,
                description="",
            )

        extra = {}
        if hasattr(entity_cls, "last_change"):
            extra["set__last_change"] = datetime.utcnow()
        if hasattr(entity_cls, "last_changed_by"):
            extra["set__last_changed_by"] = user

        entity_cls.objects(company=company, id__in=ids).update(
            set__project=project, **extra
        )

        return project

    task_count_statuses = get_options(TaskStatus)

    @classmethod
    def make_projects_get_all_pipeline(
        cls,
        company_id: str,
        project_ids: Sequence[str],
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
    ) -> Sequence:
        def add_state_to_filter(f: Mapping[str, Any]) -> Mapping[str, Any]:
            f = f or {}
            new_f = {k: v for k, v in f.items() if k != "system_tags"}
            system_tags = [
                tag
                for tag in f.get("system_tags", [])
                if tag
                not in (
                    EntityVisibility.archived.value,
                    f"-{EntityVisibility.archived.value}",
                )
            ]

            system_tags.append(f"-{EntityVisibility.archived.value}")
            new_f["system_tags"] = system_tags

            return new_f

        def project_task_fields():
            return {
                "$project": {
                    "project": 1,
                    "status": 1,
                    "started": 1,
                    "completed": 1,
                }
            }

        def task_status_counts_subquery() -> dict:
            def get_status_condition(status) -> dict:
                if status == TaskStatus.unknown:
                    return {"$eq": [{"$ifNull": ["$status", status]}, status]}

                return {"$eq": ["$status", status]}

            return {
                status: {
                    "$sum": {
                        "$cond": {
                            "if": get_status_condition(status),
                            "then": 1,
                            "else": 0,
                        }
                    }
                }
                for status in cls.task_count_statuses
            }

        def completed_after_subquery(time_thresh: datetime):
            return {
                # the sum of
                "$sum": {
                    # for each task
                    "$cond": {
                        # if completed after the time_thresh
                        "if": {
                            "$and": [
                                "$completed",
                                {"$gt": ["$completed", time_thresh]},
                                {
                                    "$not": {
                                        "$in": [
                                            "$status",
                                            [
                                                TaskStatus.queued,
                                                TaskStatus.in_progress,
                                                TaskStatus.failed,
                                            ],
                                        ]
                                    }
                                },
                            ]
                        },
                        "then": 1,
                        "else": 0,
                    }
                }
            }

        def max_started_subquery():
            return {"$max": "$started"}

        def runtime_subquery():
            return {
                # the sum of
                "$sum": {
                    # for each task
                    "$cond": {
                        # if completed and started and completed > started
                        "if": {
                            "$and": [
                                "$started",
                                "$completed",
                                {"$gt": ["$completed", "$started"]},
                            ]
                        },
                        # then: floor((completed - started) / 1000)
                        "then": {
                            "$floor": {
                                "$divide": [
                                    {"$subtract": ["$completed", "$started"]},
                                    1000.0,
                                ]
                            }
                        },
                        "else": 0,
                    }
                }
            }

        time_thresh = datetime.utcnow() - timedelta(hours=24)
        runtime_pipeline = [
            # only count run time for these types of tasks
            {
                "$match": cls.get_match_conditions(
                    company=company_id,
                    project_ids=project_ids,
                    filter_=add_state_to_filter(filter_),
                    users=users,
                )
            },
            project_task_fields(),
            {
                # for each project
                "$group": {
                    "_id": "$project",
                    "total_runtime": runtime_subquery(),
                    "recently_completed": completed_after_subquery(
                        time_thresh=time_thresh
                    ),
                    "max_task_started": max_started_subquery(),
                    **task_status_counts_subquery(),
                }
            },
        ]

        return runtime_pipeline

    T = TypeVar("T")

    @staticmethod
    def aggregate_project_data(
        func: Callable[[T, T], T],
        project_ids: Sequence[str],
        child_projects: ProjectsChildren,
        data: Mapping[str, T],
    ) -> Dict[str, T]:
        """
        Given a list of project ids and data collected over these projects and their subprojects
        For each project aggregates the data from all of its subprojects
        """
        aggregated = {}
        if not data:
            return aggregated
        for pid in project_ids:
            relevant_projects = {p.id for p in child_projects.get(pid, [])} | {pid}
            relevant_data = [data for p, data in data.items() if p in relevant_projects]
            if not relevant_data:
                continue
            aggregated[pid] = reduce(func, relevant_data)
        return aggregated

    @classmethod
    def get_dataset_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        if not project_ids:
            return {}

        task_runtime_pipeline = [
            {
                "$match": {
                    **cls.get_match_conditions(
                        company=company,
                        project_ids=project_ids,
                        users=users,
                        filter_={
                            "system_tags": [f"-{EntityVisibility.archived.value}"]
                        },
                    ),
                    "runtime": {"$exists": True, "$gt": {}},
                }
            },
            {"$project": {"project": 1, "runtime": 1, "last_update": 1}},
            {"$sort": {"project": 1, "last_update": 1}},
            {"$group": {"_id": "$project", "runtime": {"$last": "$runtime"}}},
        ]

        return {
            r["_id"]: {
                "file_count": r["runtime"].get("ds_file_count", 0),
                "total_size": r["runtime"].get("ds_total_size", 0),
            }
            for r in Task.aggregate(task_runtime_pipeline)
        }

    @staticmethod
    def _get_projects_children(
        project_ids: Sequence[str],
        search_hidden: bool,
        allowed_ids: Sequence[str],
    ) -> Tuple[ProjectsChildren, Set[str]]:
        child_projects = _get_sub_projects(
            project_ids,
            _only=("id", "name"),
            search_hidden=search_hidden,
            allowed_ids=allowed_ids,
        )
        return (
            child_projects,
            {c.id for c in chain.from_iterable(child_projects.values())},
        )

    @staticmethod
    def _get_children_info(
        project_ids: Sequence[str], child_projects: ProjectsChildren
    ) -> dict:
        return {
            project: sorted(
                [{"id": c.id, "name": c.name} for c in child_projects.get(project, [])],
                key=itemgetter("name"),
            )
            for project in project_ids
        }

    @classmethod
    def _get_project_dataset_stats_core(
        cls,
        company: str,
        project_ids: Sequence[str],
        project_field: str,
        entity_class: Type[AttributedDocument],
        include_children: bool = True,
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
        selected_project_ids: Sequence[str] = None,
    ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        if not project_ids:
            return {}, {}

        child_projects = {}
        project_ids_with_children = set(project_ids)
        if include_children:
            child_projects, children_ids = cls._get_projects_children(
                project_ids,
                search_hidden=True,
                allowed_ids=selected_project_ids,
            )
            project_ids_with_children |= children_ids

        pipeline = [
            {
                "$match": cls.get_match_conditions(
                    company=company,
                    project_ids=list(project_ids_with_children),
                    filter_=filter_,
                    users=users,
                    project_field=project_field,
                )
            },
            {"$project": {project_field: 1, "tags": 1}},
            {
                "$group": {
                    "_id": f"${project_field}",
                    "count": {"$sum": 1},
                    "tags": {"$push": "$tags"},
                }
            },
        ]
        res = entity_class.aggregate(pipeline)

        project_stats = {
            result["_id"]: {
                "count": result.get("count", 0),
                "tags": set(chain.from_iterable(result.get("tags", []))),
            }
            for result in res
        }

        def concat_dataset_stats(a: dict, b: dict) -> dict:
            return {
                "count": a.get("count", 0) + b.get("count", 0),
                "tags": a.get("tags", {}) | b.get("tags", {}),
            }

        top_project_stats = cls.aggregate_project_data(
            func=concat_dataset_stats,
            project_ids=project_ids,
            child_projects=child_projects,
            data=project_stats,
        )
        for _, stat in top_project_stats.items():
            stat["tags"] = sorted(list(stat.get("tags", {})))

        empty_stats = {"count": 0, "tags": []}
        stats = {
            project: {"datasets": top_project_stats.get(project, empty_stats)}
            for project in project_ids
        }
        return stats, cls._get_children_info(project_ids, child_projects)

    @classmethod
    def get_project_dataset_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        include_children: bool = True,
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
        selected_project_ids: Sequence[str] = None,
    ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        filter_ = filter_ or {}
        filter_system_tags = filter_.get("system_tags")
        if not isinstance(filter_system_tags, list):
            filter_system_tags = []
        if dataset_tag not in filter_system_tags:
            filter_system_tags.append(dataset_tag)
            filter_["system_tags"] = filter_system_tags

        return cls._get_project_dataset_stats_core(
            company=company,
            project_ids=project_ids,
            project_field="parent",
            entity_class=Project,
            include_children=include_children,
            filter_=filter_,
            users=users,
            selected_project_ids=selected_project_ids,
        )

    @classmethod
    def get_project_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        include_children: bool = True,
        search_hidden: bool = False,
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
        selected_project_ids: Sequence[str] = None,
    ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        if not project_ids:
            return {}, {}

        child_projects = {}
        project_ids_with_children = set(project_ids)
        if include_children:
            child_projects, children_ids = cls._get_projects_children(
                project_ids,
                search_hidden=search_hidden,
                allowed_ids=selected_project_ids,
            )
            project_ids_with_children |= children_ids

        runtime_pipeline = cls.make_projects_get_all_pipeline(
            company,
            project_ids=list(project_ids_with_children),
            filter_=filter_,
            users=users,
        )

        runtime = {
            result["_id"]: {k: v for k, v in result.items() if k != "_id"}
            for result in Task.aggregate(runtime_pipeline)
        }

        def sum_runtime(
            a: Mapping[str, dict], b: Mapping[str, dict]
        ) -> Dict[str, dict]:
            ret = {}
            for key in set(a) | set(b):
                if key == "max_task_started":
                    val = max(a.get(key) or datetime.min, b.get(key) or datetime.min)
                else:
                    val = a.get(key, 0) + b.get(key, 0)

                ret[key] = val

            return ret

        runtime = cls.aggregate_project_data(
            func=sum_runtime,
            project_ids=project_ids,
            child_projects=child_projects,
            data=runtime,
        )

        def get_project_info(project_id):
            project_runtime = runtime.get(project_id, {})

            def get_time_or_none(value):
                return value if value != datetime.min else None

            status_counts = {
                status: project_runtime.get(status, 0)
                for status in cls.task_count_statuses
            }
            return {
                "status_count": status_counts,
                "total_tasks": sum(status_counts.values()),
                "total_runtime": project_runtime.get("total_runtime", 0),
                "completed_tasks_24h": project_runtime.get("recently_completed", 0),
                "last_task_run": get_time_or_none(
                    project_runtime.get("max_task_started", datetime.min)
                ),
            }

        stats = {
            project: {"active": get_project_info(project)} for project in project_ids
        }

        return stats, cls._get_children_info(project_ids, child_projects)

    @classmethod
    def get_active_users(
        cls,
        company,
        project_ids: Sequence[str],
        user_ids: Optional[Sequence[str]] = None,
    ) -> Set[Union[str, type(None)]]:
        """
        Get the set of user ids that created tasks/models in the given projects
        If project_ids is empty then all projects are examined
        If user_ids are passed then only subset of these users is returned
        """
        query = Q(company=company)
        if user_ids:
            query &= Q(user__in=user_ids)

        projects_query = query
        if project_ids:
            project_ids = _ids_with_children(project_ids)
            query &= Q(project__in=project_ids)
            projects_query &= Q(id__in=project_ids)

        res = set(Project.objects(projects_query).distinct(field="user"))
        for cls_ in cls.child_classes:
            res |= set(cls_.objects(query).distinct(field="user"))

        return res

    @classmethod
    def get_project_tags(
        cls,
        company_id: str,
        include_system: bool,
        projects: Sequence[str] = None,
        filter_: Dict[str, Sequence[str]] = None,
    ) -> Tuple[Sequence[str], Sequence[str]]:
        query = Q(company=company_id)
        if filter_:
            for name, vals in filter_.items():
                if vals:
                    query &= GetMixin.get_list_field_query(name, vals)

        if projects:
            query &= Q(id__in=_ids_with_children(projects))

        tags = Project.objects(query).distinct("tags")
        system_tags = (
            Project.objects(query).distinct("system_tags") if include_system else []
        )
        return tags, system_tags

    @classmethod
    def get_projects_with_selected_children(
        cls,
        company: str,
        users: Sequence[str] = None,
        project_ids: Optional[Sequence[str]] = None,
        allow_public: bool = True,
        children_type: ProjectChildrenType = None,
        children_tags: Sequence[str] = None,
        children_tags_filter: dict = None,
    ) -> Tuple[Sequence[str], Sequence[str]]:
        """
        Get the projects ids matching children_condition (if passed) or where the passed user created any tasks
        including all the parents of these projects
        If project ids are specified then filter the results by these project ids
        """
        if not (users or children_type):
            raise errors.bad_request.ValidationError(
                "Either active users or children_condition should be specified"
            )

        query = (
            get_company_or_none_constraint(company)
            if allow_public
            else Q(company=company)
        )
        if users:
            query &= Q(user__in=users)

        project_query = None
        if children_tags_filter:
            child_query = query & GetMixin.get_list_filter_query(
                "tags", children_tags_filter
            )
        elif children_tags:
            child_query = query & GetMixin.get_list_field_query("tags", children_tags)
        else:
            child_query = query

        if children_type == ProjectChildrenType.dataset:
            child_queries = {
                Project: child_query
                & Q(system_tags__in=[dataset_tag], basename__ne=datasets_project_name)
            }
        elif children_type == ProjectChildrenType.pipeline:
            child_queries = {
                Project: child_query
                & Q(system_tags__in=[pipeline_tag], basename__ne=pipelines_project_name)
            }
        elif children_type == ProjectChildrenType.report:
            child_queries = {Task: child_query & Q(system_tags__in=[reports_tag])}
        else:
            project_query = query
            child_queries = {entity_cls: query for entity_cls in cls.child_classes}

        if project_ids:
            ids_with_children = _ids_with_children(project_ids)
            if project_query:
                project_query &= Q(id__in=ids_with_children)
            for child_cls in child_queries:
                child_queries[child_cls] &= (
                    Q(parent__in=ids_with_children)
                    if child_cls is Project
                    else Q(project__in=ids_with_children)
                )

        res = (
            set(Project.objects(project_query).scalar("id")) if project_query else set()
        )
        for cls_, query_ in child_queries.items():
            res |= set(
                cls_.objects(query_).distinct(
                    field="id" if cls_ is Project else "project"
                )
            )

        res = list(res)
        if not res:
            return res, res

        selected_project_ids = _ids_with_parents(res)
        filtered_ids = (
            list(set(selected_project_ids) & set(project_ids))
            if project_ids
            else list(selected_project_ids)
        )

        return filtered_ids, selected_project_ids

    @staticmethod
    def _get_project_query(
        company: str,
        projects: Sequence,
        include_subprojects: bool = True,
        state: Optional[EntityVisibility] = None,
    ) -> Q:
        query = get_company_or_none_constraint(company)
        if projects:
            if include_subprojects:
                projects = _ids_with_children(projects)
            query &= Q(project__in=projects)
        # else:
        #     query &= Q(system_tags__nin=[EntityVisibility.hidden.value])

        if state == EntityVisibility.archived:
            query &= Q(system_tags__in=[EntityVisibility.archived.value])
        elif state == EntityVisibility.active:
            query &= Q(system_tags__nin=[EntityVisibility.archived.value])

        return query

    @classmethod
    def get_task_parents(
        cls,
        company_id: str,
        projects: Sequence[str],
        include_subprojects: bool,
        state: Optional[EntityVisibility] = None,
        name: str = None,
    ) -> Sequence[dict]:
        """
        Get list of unique parent tasks sorted by task name for the passed company projects
        If projects is None or empty then get parents for all the company tasks
        """
        query = cls._get_project_query(
            company_id, projects, include_subprojects=include_subprojects, state=state
        )

        parent_ids = set(Task.objects(query).distinct("parent"))
        if not parent_ids:
            return []

        parents: Sequence[dict] = Task.get_many_with_join(
            company_id,
            query=Q(id__in=parent_ids),
            query_dict={"name": name} if name else None,
            allow_public=True,
            override_projection=("id", "name", "project.name"),
        )

        return sorted(parents, key=itemgetter("name"))

    @classmethod
    def get_entity_users(
        cls,
        company: str,
        entity_cls: Type[Union[Task, Model]],
        projects: Sequence[str],
        include_subprojects: bool,
    ) -> Sequence[dict]:
        query = cls._get_project_query(
            company, projects, include_subprojects=include_subprojects
        )
        user_ids = entity_cls.objects(query).distinct(field="user")
        if not user_ids:
            return []
        users = User.objects(id__in=user_ids).only("id", "name")
        return [{"id": u.id, "name": u.name} for u in users]

    @classmethod
    def get_task_types(cls, company, project_ids: Optional[Sequence]) -> set:
        """
        Return the list of unique task types used by company and public tasks
        If project ids passed then only tasks from these projects are considered
        """
        query = cls._get_project_query(company, project_ids)
        res = Task.objects(query).distinct(field="type")
        return set(res).intersection(external_task_types)

    @classmethod
    def get_model_frameworks(cls, company, project_ids: Optional[Sequence]) -> Sequence:
        """
        Return the list of unique frameworks used by company and public models
        If project ids passed then only models from these projects are considered
        """
        query = cls._get_project_query(company, project_ids)
        return Model.objects(query).distinct(field="framework")

    @staticmethod
    def get_match_conditions(
        company: str,
        project_ids: Sequence[str],
        filter_: Mapping[str, Any],
        users: Sequence[str],
        project_field: str = "project",
    ):
        conditions = {
            "company": {"$in": ["", company]},
            project_field: {"$in": project_ids},
        }
        if users:
            conditions["user"] = {"$in": users}

        if not filter_:
            return conditions

        or_conditions = []
        for field, field_filter in filter_.items():
            if not (field_filter and isinstance(field_filter, (list, dict))):
                raise errors.bad_request.ValidationError(
                    f"Non empty list or dictionary expected for the field: {field}"
                )

            if isinstance(field_filter, list):
                if not all(isinstance(t, str) for t in field_filter):
                    raise errors.bad_request.ValidationError(
                        f"Only string values are allowed in the list filter: {field}"
                    )
                helper = GetMixin.NewListFieldBucketHelper(
                    field, data=field_filter, legacy=True
                )
                op = helper.global_operator
                db_query = {op: helper.actions}
            else:
                helper = GetMixin.ListQueryFilter.from_data(field, field_filter)
                db_query = helper.db_query

            for op, actions in db_query.items():
                field_conditions = {}
                for action, values in actions.items():
                    value = list(set(values)) if isinstance(values, list) else values
                    for key in reversed(action.split("__")):
                        value = {f"${key}": value}
                    field_conditions.update(value)

                if op == Q.OR and len(field_conditions) > 1:
                    or_conditions.append(
                        {
                            "$or": [
                                {field: {db_modifier: cond}}
                                for db_modifier, cond in field_conditions.items()
                            ]
                        }
                    )
                else:
                    conditions[field] = field_conditions

        if or_conditions:
            if len(or_conditions) == 1:
                conditions.update(next(iter(or_conditions)))
            else:
                # noinspection PyTypeChecker
                conditions["$and"] = [c for c in or_conditions]

        return conditions

    @classmethod
    def _calc_own_datasets_core(
        cls,
        company: str,
        project_ids: Sequence[str],
        project_field: str,
        entity_class: Type[AttributedDocument],
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        """
        Returns the amount of hyper datasets per requested project
        """
        if not project_ids:
            return {}

        pipeline = [
            {
                "$match": cls.get_match_conditions(
                    company=company,
                    project_ids=project_ids,
                    filter_=filter_,
                    users=users,
                    project_field=project_field,
                )
            },
            {"$project": {project_field: 1}},
            {"$group": {"_id": f"${project_field}", "count": {"$sum": 1}}},
        ]
        datasets = {
            data["_id"]: data["count"] for data in entity_class.aggregate(pipeline)
        }

        return {pid: {"own_datasets": datasets.get(pid, 0)} for pid in project_ids}

    @classmethod
    def calc_own_datasets(
        cls,
        company: str,
        project_ids: Sequence[str],
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        """
        Returns the amount of datasets per requested project
        """
        filter_ = filter_ or {}
        filter_system_tags = filter_.get("system_tags")
        if not isinstance(filter_system_tags, list):
            filter_system_tags = []
        if dataset_tag not in filter_system_tags:
            filter_system_tags.append(dataset_tag)
            filter_["system_tags"] = filter_system_tags

        return cls._calc_own_datasets_core(
            company=company,
            project_ids=project_ids,
            project_field="parent",
            entity_class=Project,
            filter_=filter_,
            users=users,
        )

    @classmethod
    def calc_own_contents(
        cls,
        company: str,
        project_ids: Sequence[str],
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        """
        Returns the amount of task/models per requested project
        Use separate aggregation calls on Task/Model instead of lookup
        aggregation on projects in order not to hit memory limits on large tasks
        """
        if not project_ids:
            return {}

        filter_ = filter_ or {}
        system_tags_filter = filter_.get("system_tags", [])
        archived = EntityVisibility.archived.value
        non_archived = f"-{EntityVisibility.archived.value}"
        if not any(t in system_tags_filter for t in (archived, non_archived)):
            filter_ = {k: v for k, v in filter_.items()}
            filter_["system_tags"] = [non_archived, *system_tags_filter]

        pipeline = [
            {
                "$match": cls.get_match_conditions(
                    company=company,
                    project_ids=project_ids,
                    filter_=filter_,
                    users=users,
                )
            },
            {"$project": {"project": 1}},
            {"$group": {"_id": "$project", "count": {"$sum": 1}}},
        ]

        def get_agrregate_res(cls_: Type[AttributedDocument]) -> dict:
            return {data["_id"]: data["count"] for data in cls_.aggregate(pipeline)}

        tasks = get_agrregate_res(Task)
        models = get_agrregate_res(Model)
        return {
            pid: {"own_tasks": tasks.get(pid, 0), "own_models": models.get(pid, 0)}
            for pid in project_ids
        }
