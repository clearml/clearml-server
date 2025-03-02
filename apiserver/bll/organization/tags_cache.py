from itertools import chain
from typing import Sequence, Union, Type, Dict

from mongoengine import Q
from redis import Redis

from apiserver.config_repo import config
from apiserver.bll.project import project_ids_with_children
from apiserver.database.model.base import GetMixin
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task

log = config.logger(__file__)
_settings_prefix = "services.organization"


class _TagsCache:
    _tags_field = "tags"
    _system_tags_field = "system_tags"
    _dummy_tag = "__dummy__"
    # prepend our list in redis with this tag since empty lists are auto deleted

    def __init__(self, db_cls: Union[Type[Model], Type[Task]], redis: Redis):
        self.db_cls = db_cls
        self.redis = redis

    @property
    def _tags_cache_expiration_seconds(self):
        return config.get(f"{_settings_prefix}.tags_cache.expiration_seconds", 3600)

    def _get_tags_from_db(
        self,
        company_id: str,
        field: str,
        project: str = None,
        filter_: Dict[str, Sequence[str]] = None,
    ) -> set:
        query = Q(company=company_id)
        if filter_:
            for name, vals in filter_.items():
                if vals:
                    query &= GetMixin.get_list_field_query(name, vals)
        if project:
            query &= Q(project__in=project_ids_with_children([project]))
        # else:
        #     query &= Q(system_tags__nin=[EntityVisibility.hidden.value])

        return self.db_cls.objects(query).distinct(field)

    def _get_tags_cache_key(
        self,
        company_id: str,
        field: str,
        project: str = None,
        filter_: Dict[str, Sequence[str]] = None,
    ):
        """
        Project None means 'from all company projects'
        The key is built in the way that scanning company keys for 'all company projects'
        will not return the keys related to the particular company projects and vice versa.
        So that we can have a fine grain control on what redis keys to invalidate
        """
        filter_str = None
        if filter_:
            filter_str = "_".join(
                ["filter", *chain.from_iterable([f, *v] for f, v in filter_.items())]
            )
        key_parts = [field, company_id, project, self.db_cls.__name__, filter_str]
        return "_".join(filter(None, key_parts))

    def get_tags(
        self,
        company_id: str,
        include_system: bool = False,
        filter_: Dict[str, Sequence[str]] = None,
        project: str = None,
    ) -> dict:
        """
        Get tags and optionally system tags for the company
        Return the dictionary of tags per tags field name
        The function retrieves both cached values from Redis in one call
        and re calculates any of them if missing in Redis
        """
        fields = [self._tags_field]
        if include_system:
            fields.append(self._system_tags_field)

        ret = {}
        for field in fields:
            redis_key = self._get_tags_cache_key(
                company_id, field=field, project=project, filter_=filter_
            )
            cached_tags = self.redis.lrange(redis_key, 0, -1)
            if cached_tags:
                tags = [c.decode() for c in cached_tags[1:]]
            else:
                tags = list(
                    self._get_tags_from_db(
                        company_id, field=field, project=project, filter_=filter_
                    )
                )
                self.redis.rpush(redis_key, self._dummy_tag, *tags)
                self.redis.expire(redis_key, self._tags_cache_expiration_seconds)

            ret[field] = set(tags)

        return ret

    def update_tags(self, company_id: str, projects: Sequence[str], tags=None, system_tags=None):
        """
        Updates tags. If reset is set then both tags and system_tags
        are recalculated. Otherwise only those that are not 'None'
        """
        fields = [
            field
            for field, update in (
                (self._tags_field, tags),
                (self._system_tags_field, system_tags),
            )
            if update is not None
        ]
        if not fields:
            return

        self._delete_redis_keys(company_id, projects=projects, fields=fields)

    def reset_tags(self, company_id: str, projects: Sequence[str]):
        self._delete_redis_keys(
            company_id,
            projects=projects,
            fields=(self._tags_field, self._system_tags_field),
        )

    def _delete_redis_keys(
        self, company_id: str, projects: [Sequence[str]], fields: Sequence[str]
    ):
        redis_keys = list(
            chain.from_iterable(
                self.redis.keys(
                    self._get_tags_cache_key(company_id, field=f, project=p) + "*"
                )
                for f in fields
                for p in set(projects) | {None}
            )
        )
        if redis_keys:
            self.redis.delete(*redis_keys)
