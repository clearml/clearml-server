import itertools
from datetime import datetime
from typing import Tuple, Optional, Sequence, Mapping

from boltons.iterutils import first

from apiserver import database
from apiserver.apierrors import errors
from apiserver.database.model import EntityVisibility
from apiserver.database.model.project import Project

name_separator = "/"


def _get_project_depth(project_name: str) -> int:
    return len(list(filter(None, project_name.split(name_separator))))


def _validate_project_name(project_name: str, raise_if_empty=True) -> Tuple[str, str]:
    """
    Remove redundant '/' characters. Ensure that the project name is not empty
    Return the cleaned up project name and location
    """
    name_parts = [p.strip() for p in project_name.split(name_separator) if p]
    if not name_parts:
        if raise_if_empty:
            raise errors.bad_request.InvalidProjectName(name=project_name)
        return "", ""

    return name_separator.join(name_parts), name_separator.join(name_parts[:-1])


def _get_basename_from_name(name: str, raise_on_blank: bool = True) -> str:
    basename = name.split("/")[-1].strip()
    if raise_on_blank and (Project.min_name_length > len(basename)):
        raise errors.bad_request.ValidationError("Project name cannot be blank")

    return basename


def _ensure_project(
    company: str, user: str, name: str, creation_params: dict = None
) -> Optional[Project]:
    """
    Makes sure that the project with the given name exists
    If needed auto-create the project and all the missing projects in the path to it
    Return the project
    """
    name, location = _validate_project_name(name, raise_if_empty=False)
    if not name:
        return None

    project = _get_writable_project_from_name(company, name)
    if project:
        return project

    now = datetime.utcnow()
    project = Project(
        id=database.utils.id(),
        user=user,
        company=company,
        created=now,
        last_update=now,
        name=name,
        basename=_get_basename_from_name(name),
        **(creation_params or dict(description="")),
    )
    parent = _ensure_project(company, user, location, creation_params=creation_params)
    _save_under_parent(project=project, parent=parent)
    if parent:
        parent.update(last_update=now)

    return project


def _save_under_parent(project: Project, parent: Optional[Project]):
    """
    Save the project under the given parent project or top level (parent=None)
    Check that the project location matches the parent name
    """
    location, _, _ = project.name.rpartition(name_separator)
    if not parent:
        if location:
            raise ValueError(
                f"Project location {location} does not match empty parent name"
            )
        project.parent = None
        project.path = []
        project.save()
        return

    if location != parent.name:
        raise ValueError(
            f"Project location {location} does not match parent name {parent.name}"
        )
    project.parent = parent.id
    project.path = [*(parent.path or []), parent.id]
    project.save()


def _get_writable_project_from_name(
    company,
    name,
    _only: Optional[Sequence[str]] = ("id", "name", "path", "company", "parent"),
) -> Optional[Project]:
    """
    Return a project from name. If the project not found then return None
    """
    qs = Project.objects(company__in=[company, ""], name=name)
    if _only:
        if "company" not in _only:
            _only = ["company", *_only]
        qs = qs.only(*_only)
    projects = list(qs)

    if not projects:
        return

    project = first(p for p in projects if p.company == company)
    if not project:
        raise errors.bad_request.PublicProjectExists(name=name)

    return project


ProjectsChildren = Mapping[str, Sequence[Project]]


def _get_sub_projects(
    project_ids: Sequence[str],
    _only: Sequence[str] = ("id", "path"),
    search_hidden=True,
    allowed_ids: Sequence[str] = None,
) -> ProjectsChildren:
    """
    Return the list of child projects of all the levels for the parent project ids
    """
    query = dict(path__in=project_ids)
    if not search_hidden:
        query["system_tags__nin"] = [EntityVisibility.hidden.value]
    if allowed_ids:
        query["id__in"] = allowed_ids

    qs = Project.objects(**query)
    if _only:
        _only = set(_only) | {"path"}
        qs = qs.only(*_only)
    subprojects = list(qs)

    return {
        pid: [s for s in subprojects if pid in (s.path or [])] for pid in project_ids
    }


def _ids_with_parents(project_ids: Sequence[str]) -> Sequence[str]:
    """
    Return project ids with all the parent projects
    """
    projects = Project.objects(id__in=project_ids).only("id", "path")
    parent_ids = set(itertools.chain.from_iterable(p.path for p in projects if p.path))
    return list({*(p.id for p in projects), *parent_ids})


def _ids_with_children(project_ids: Sequence[str]) -> Sequence[str]:
    """
    Return project ids with the ids of all the subprojects
    """
    children_ids = Project.objects(path__in=project_ids).scalar("id")
    return list({*project_ids, *children_ids})


def _update_subproject_names(
    project: Project,
    children: Sequence[Project],
    old_name: str,
    update_path: bool = False,
    old_path: Sequence[str] = None,
) -> int:
    """
    Update sub project names when the base project name changes
    Optionally update the paths
    """
    updated = 0
    now = datetime.utcnow()
    for child in children:
        child_suffix = name_separator.join(
            child.name.split(name_separator)[len(old_name.split(name_separator)):]
        )
        updates = {
            "name": name_separator.join((project.name, child_suffix)),
            "last_update": now,
        }
        if update_path:
            updates["path"] = project.path + child.path[len(old_path):]
        updated += child.update(upsert=False, **updates)

    return updated


def _reposition_project_with_children(
    project: Project, children: Sequence[Project], parent: Project
) -> int:
    new_location = parent.name if parent else None
    old_name = project.name
    old_path = project.path
    project.name = name_separator.join(
        filter(None, (new_location, project.name.split(name_separator)[-1]))
    )
    project.last_update = datetime.utcnow()
    _save_under_parent(project, parent=parent)

    moved = 1 + _update_subproject_names(
        project=project,
        children=children,
        old_name=old_name,
        update_path=True,
        old_path=old_path,
    )
    return moved
