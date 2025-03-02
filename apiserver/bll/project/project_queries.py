import json
from collections import OrderedDict
from datetime import datetime
from typing import (
    Sequence,
    Optional,
    Tuple,
)

from redis import StrictRedis

from apiserver.config_repo import config
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task
from apiserver.redis_manager import redman
from apiserver.utilities.dicts import nested_get
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper
from .sub_projects import _ids_with_children

log = config.logger(__file__)


class ProjectQueries:
    def __init__(self, redis=None):
        self.redis: StrictRedis = redis or redman.connection("apiserver")

    @staticmethod
    def _get_project_constraint(
        project_ids: Sequence[str], include_subprojects: bool
    ) -> dict:
        """
        If passed projects is None means top level projects
        If passed projects is empty means no project filtering
        """
        if include_subprojects:
            if not project_ids:
                return {}
            project_ids = _ids_with_children(project_ids)

        if project_ids is None:
            project_ids = [None]
        if not project_ids:
            return {}

        return {"project": {"$in": project_ids}}

    @staticmethod
    def _get_company_constraint(company_id: str, allow_public: bool = True) -> dict:
        if allow_public:
            return {"company": {"$in": ["", company_id]}}

        return {"company": company_id}

    @classmethod
    def get_aggregated_project_parameters(
        cls,
        company_id,
        project_ids: Sequence[str],
        include_subprojects: bool,
        page: int = 0,
        page_size: int = 500,
    ) -> Tuple[int, int, Sequence[dict]]:
        page = max(0, page)
        page_size = max(1, page_size)
        pipeline = [
            {
                "$match": {
                    **cls._get_company_constraint(company_id),
                    **cls._get_project_constraint(project_ids, include_subprojects),
                    "hyperparams": {"$exists": True, "$gt": {}},
                }
            },
            {"$project": {"sections": {"$objectToArray": "$hyperparams"}}},
            {"$unwind": "$sections"},
            {
                "$project": {
                    "section": "$sections.k",
                    "names": {"$objectToArray": "$sections.v"},
                }
            },
            {"$unwind": "$names"},
            {"$group": {"_id": {"section": "$section", "name": "$names.k"}}},
            {"$sort": OrderedDict({"_id.section": 1, "_id.name": 1})},
            {"$skip": page * page_size},
            {"$limit": page_size},
            {
                "$group": {
                    "_id": 1,
                    "total": {"$sum": 1},
                    "results": {"$push": "$$ROOT"},
                }
            },
        ]

        result = next(Task.aggregate(pipeline), None)

        total = 0
        remaining = 0
        results = []

        if result:
            total = int(result.get("total", -1))
            results = [
                {
                    "section": ParameterKeyEscaper.unescape(
                        nested_get(r, ("_id", "section"))
                    ),
                    "name": ParameterKeyEscaper.unescape(
                        nested_get(r, ("_id", "name"))
                    ),
                }
                for r in result.get("results", [])
            ]
            remaining = max(0, total - (len(results) + page * page_size))

        return total, remaining, results

    ParamValues = Tuple[int, Sequence[str]]

    def _get_cached_param_values(
        self, key: str, last_update: datetime, allowed_delta_sec=0
    ) -> Optional[ParamValues]:
        try:
            cached = self.redis.get(key)
            if not cached:
                return

            data = json.loads(cached)
            cached_last_update = datetime.fromtimestamp(data["last_update"])
            if (last_update - cached_last_update).total_seconds() <= allowed_delta_sec:
                return data["total"], data["values"]
        except Exception as ex:
            log.error(f"Error retrieving params cached values: {str(ex)}")

    def get_task_hyperparam_distinct_values(
        self,
        company_id: str,
        project_ids: Sequence[str],
        section: str,
        name: str,
        include_subprojects: bool,
        allow_public: bool = True,
        pattern: str = None,
        page: int = 0,
        page_size: int = 500,
    ) -> ParamValues:
        page = max(0, page)
        page_size = max(1, page_size)
        company_constraint = self._get_company_constraint(company_id, allow_public)
        project_constraint = self._get_project_constraint(
            project_ids, include_subprojects
        )
        key_path = f"hyperparams.{ParameterKeyEscaper.escape(section)}.{ParameterKeyEscaper.escape(name)}"
        last_updated_task = (
            Task.objects(
                **company_constraint,
                **project_constraint,
                **{f"{key_path.replace('.', '__')}__exists": True},
            )
            .only("last_update")
            .order_by("-last_update")
            .limit(1)
            .first()
        )
        if not last_updated_task:
            return 0, []

        redis_key = "_".join(
            str(part)
            for part in (
                "hyperparam_values",
                company_id,
                "_".join(project_ids),
                section,
                name,
                allow_public,
                pattern,
                page,
                page_size,
            )
        )
        last_update = last_updated_task.last_update or datetime.utcnow()
        cached_res = self._get_cached_param_values(
            key=redis_key,
            last_update=last_update,
            allowed_delta_sec=config.get(
                "services.tasks.hyperparam_values.cache_allowed_outdate_sec", 60
            ),
        )
        if cached_res:
            return cached_res

        match_condition = {
            **company_constraint,
            **project_constraint,
            key_path: {"$exists": True},
        }
        if pattern:
            match_condition["$expr"] = {
                "$regexMatch": {
                    "input": f"${key_path}.value",
                    "regex": pattern,
                    "options": "i",
                }
            }

        pipeline = [
            {"$match": match_condition},
            {"$project": {"value": f"${key_path}.value"}},
            {"$group": {"_id": "$value"}},
            {"$sort": {"_id": 1}},
            {"$skip": page * page_size},
            {"$limit": page_size},
            {
                "$group": {
                    "_id": 1,
                    "total": {"$sum": 1},
                    "results": {"$push": "$$ROOT._id"},
                }
            },
        ]

        result = next(Task.aggregate(pipeline, collation=Task._numeric_locale), None)
        if not result:
            return 0, []

        total = int(result.get("total", 0))
        values = result.get("results", [])

        ttl = config.get("services.tasks.hyperparam_values.cache_ttl_sec", 86400)
        cached = dict(last_update=last_update.timestamp(), total=total, values=values)
        self.redis.setex(redis_key, ttl, json.dumps(cached))

        return total, values

    @classmethod
    def get_unique_metric_variants(
        cls,
        company_id,
        project_ids: Sequence[str],
        include_subprojects: bool,
        ids: Sequence[str],
        model_metrics: bool = False,
    ):
        pipeline = [
            {
                "$match": {
                    **cls._get_company_constraint(company_id),
                    **cls._get_project_constraint(project_ids, include_subprojects),
                    **({"_id": {"$in": ids}} if ids else {}),
                }
            },
            {"$project": {"metrics": {"$objectToArray": "$last_metrics"}}},
            {"$unwind": "$metrics"},
            {
                "$project": {
                    "metric": "$metrics.k",
                    "variants": {"$objectToArray": "$metrics.v"},
                }
            },
            {"$unwind": "$variants"},
            {
                "$group": {
                    "_id": {
                        "metric": "$variants.v.metric",
                        "variant": "$variants.v.variant",
                    },
                    "metrics": {
                        "$addToSet": {
                            "metric": "$variants.v.metric",
                            "metric_hash": "$metric",
                            "variant": "$variants.v.variant",
                            "variant_hash": "$variants.k",
                        }
                    },
                }
            },
            {"$sort": OrderedDict({"_id.metric": 1, "_id.variant": 1})},
        ]

        entity_cls = Model if model_metrics else Task
        result = entity_cls.aggregate(pipeline)
        return [r["metrics"][0] for r in result]

    @classmethod
    def get_model_metadata_keys(
        cls,
        company_id,
        project_ids: Sequence[str],
        include_subprojects: bool,
        page: int = 0,
        page_size: int = 500,
    ) -> Tuple[int, int, Sequence[dict]]:
        page = max(0, page)
        page_size = max(1, page_size)
        pipeline = [
            {
                "$match": {
                    **cls._get_company_constraint(company_id),
                    **cls._get_project_constraint(project_ids, include_subprojects),
                    "metadata": {"$exists": True, "$gt": {}},
                }
            },
            {"$project": {"metadata": {"$objectToArray": "$metadata"}}},
            {"$unwind": "$metadata"},
            {"$group": {"_id": "$metadata.k"}},
            {"$sort": {"_id": 1}},
            {"$skip": page * page_size},
            {"$limit": page_size},
            {
                "$group": {
                    "_id": 1,
                    "total": {"$sum": 1},
                    "results": {"$push": "$$ROOT"},
                }
            },
        ]

        result = next(Model.aggregate(pipeline), None)

        total = 0
        remaining = 0
        results = []

        if result:
            total = int(result.get("total", -1))
            results = [
                ParameterKeyEscaper.unescape(r.get("_id"))
                for r in result.get("results", [])
            ]
            remaining = max(0, total - (len(results) + page * page_size))

        return total, remaining, results

    def get_model_metadata_distinct_values(
        self,
        company_id: str,
        project_ids: Sequence[str],
        key: str,
        include_subprojects: bool,
        allow_public: bool = True,
        page: int = 0,
        page_size: int = 500,
    ) -> ParamValues:
        page = max(0, page)
        page_size = max(1, page_size)
        company_constraint = self._get_company_constraint(company_id, allow_public)
        project_constraint = self._get_project_constraint(
            project_ids, include_subprojects
        )
        key_path = f"metadata.{ParameterKeyEscaper.escape(key)}"
        last_updated_model = (
            Model.objects(
                **company_constraint,
                **project_constraint,
                **{f"{key_path.replace('.', '__')}__exists": True},
            )
            .only("last_update")
            .order_by("-last_update")
            .limit(1)
            .first()
        )
        if not last_updated_model:
            return 0, []

        redis_key = f"modelmetadata_values_{company_id}_{'_'.join(project_ids)}_{key}_{allow_public}_{page}_{page_size}"
        last_update = last_updated_model.last_update or datetime.utcnow()
        cached_res = self._get_cached_param_values(
            key=redis_key, last_update=last_update
        )
        if cached_res:
            return cached_res

        pipeline = [
            {
                "$match": {
                    **company_constraint,
                    **project_constraint,
                    key_path: {"$exists": True},
                }
            },
            {"$project": {"value": f"${key_path}.value"}},
            {"$group": {"_id": "$value"}},
            {"$sort": {"_id": 1}},
            {"$skip": page * page_size},
            {"$limit": page_size},
            {
                "$group": {
                    "_id": 1,
                    "total": {"$sum": 1},
                    "results": {"$push": "$$ROOT._id"},
                }
            },
        ]

        result = next(Model.aggregate(pipeline, collation=Model._numeric_locale), None)
        if not result:
            return 0, []

        total = int(result.get("total", 0))
        values = result.get("results", [])

        ttl = config.get("services.models.metadata_values.cache_ttl_sec", 86400)
        cached = dict(last_update=last_update.timestamp(), total=total, values=values)
        self.redis.setex(redis_key, ttl, json.dumps(cached))

        return total, values
