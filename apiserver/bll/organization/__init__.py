from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Sequence, Dict, Type

from apiserver.apierrors import errors
from apiserver.bll.util import update_project_time
from apiserver.config_repo import config
from apiserver.database.model.model import AttributedDocument
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task
from apiserver.redis_manager import redman
from .tags_cache import _TagsCache

log = config.logger(__file__)


class Tags(Enum):
    Task = "task"
    Model = "model"


class OrgBLL:
    def __init__(self, redis=None):
        self.redis = redis or redman.connection("apiserver")
        self._task_tags = _TagsCache(Task, self.redis)
        self._model_tags = _TagsCache(Model, self.redis)

    def edit_entity_tags(
        self,
        company_id,
        user_id: str,
        entity_cls: Type[AttributedDocument],
        entity_ids: Sequence[str],
        add_tags: Sequence[str],
        remove_tags: Sequence[str],
    ) -> int:
        if entity_cls not in (Task, Model):
            raise errors.bad_request.ValidationError(
                "Tags editing can be called on tasks or models only"
            )
        if not entity_ids:
            raise errors.bad_request.ValidationError(
                "No entity ids provided for editing tags"
            )
        if not (add_tags or remove_tags):
            raise errors.bad_request.ValidationError(
                "Either add tags or remove tags should be provided"
            )

        updated = 0
        last_changed = {
            "set__last_change": datetime.utcnow(),
            "set__last_changed_by": user_id,
        }
        if add_tags:
            updated += entity_cls.objects(company=company_id, id__in=entity_ids).update(
                add_to_set__tags=add_tags, **last_changed,
            )
        if remove_tags:
            updated += entity_cls.objects(company=company_id, id__in=entity_ids).update(
                pull_all__tags=remove_tags, **last_changed,
            )
        if not updated:
            return 0

        projects = entity_cls.objects(company=company_id, id__in=entity_ids).distinct(
            "project"
        )
        update_project_time(project_ids=projects)
        self.update_tags(
            company_id,
            entity=Tags.Task if entity_cls is Task else Tags.Model,
            projects=projects,
            tags=add_tags or remove_tags
        )
        return updated

    def get_tags(
        self,
        company_id: str,
        entity: Tags,
        include_system: bool = False,
        filter_: Dict[str, Sequence[str]] = None,
        projects: Sequence[str] = None,
    ) -> dict:
        tags_cache = self._get_tags_cache_for_entity(entity)
        if not projects:
            return tags_cache.get_tags(
                company_id, include_system=include_system, filter_=filter_
            )

        ret = defaultdict(set)
        for project in projects:
            project_tags = tags_cache.get_tags(
                company_id,
                include_system=include_system,
                filter_=filter_,
                project=project,
            )
            for field, tags in project_tags.items():
                ret[field] |= tags

        return ret

    def update_tags(
        self, company_id: str, entity: Tags, projects: Sequence[str], tags=None, system_tags=None,
    ):
        tags_cache = self._get_tags_cache_for_entity(entity)
        tags_cache.update_tags(company_id, projects, tags, system_tags)

    def reset_tags(self, company_id: str, entity: Tags, projects: Sequence[str]):
        tags_cache = self._get_tags_cache_for_entity(entity)
        tags_cache.reset_tags(company_id, projects=projects)

    def _get_tags_cache_for_entity(self, entity: Tags) -> _TagsCache:
        return self._task_tags if entity == Tags.Task else self._model_tags
