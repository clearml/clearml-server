from collections import defaultdict
from datetime import datetime, timezone, timedelta, date, time
from itertools import chain
from operator import itemgetter
from typing import Sequence, Mapping, Type

from boltons.iterutils import bucketize
from mongoengine import Document

from apiserver.apimodels.organization import UsageBreakdownKeys, UsageAggFields
from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model import User
from apiserver.database.model.project import Project
from apiserver.database.model.queue import Queue
from apiserver.database.model.task.task import Task
from .sub_projects import _get_sub_projects

log = config.logger(__file__)


class ProjectUsages:
    _default_cpu_usage = config.get("services.queues.resource_usages.cpu")
    _default_gpu_usage = config.get("services.queues.resource_usages.gpu")
    _conf = config.get("services.organization.project_usages")
    _excluded_task_tags = _conf.get("excluded_task_tags", [])
    _excluded_task_types = _conf.get("excluded_task_types", [])
    _exclude_app_parent_tasks = _conf.get("exclude_app_parent_tasks", True)
    _exclude_tasks_without_queue = _conf.get("exclude_tasks_without_queue", True)

    _field_to_cls = {
        UsageBreakdownKeys.project: Project,
        UsageBreakdownKeys.user: User,
        UsageBreakdownKeys.queue: Queue,
    }
    _other_q_id = "_other_"

    @classmethod
    def _select_project_tasks(
        cls,
        company_id: str,
        project_ids: Sequence[str],
        start: datetime,
        end: datetime,
        include_development: bool,
    ) -> dict:
        """
        The tasks that belong to the passed projects, have started field set
        and are either in_progress or competed after the start date.
        For the tasks that are not running but do not have completed field (for example failed tasks)
        we check for the status_changed field
        Since mongo date time is always stored as utc make sure to convert the incoming dates to UTC
        """
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        optional_filters = {}
        if project_ids:
            optional_filters["project"] = {"$in": project_ids}
        if not include_development and cls._exclude_tasks_without_queue:
            optional_filters["execution.queue"] = {"$exists": True, "$nin": [None, ""]}

        excluded_tags = [] if include_development else ["development"]
        if cls._excluded_task_tags:
            excluded_tags.extend(cls._excluded_task_tags)
        if excluded_tags:
            optional_filters["system_tags"] = {"$nin": excluded_tags}

        if cls._excluded_task_types:
            optional_filters["type"] = {"$nin": cls._excluded_task_types}

        if cls._exclude_app_parent_tasks:
            optional_filters["application"] = {"$eq": None}

        return {
            "$match": {
                "company": {"$in": ["", company_id]},
                "started": {"$exists": True, "$lt": end},
                "$or": [
                    {"status": "in_progress"},
                    {"completed": {"$gt": start}},
                    {"completed": {"$exists": False}, "status_changed": {"$gt": start}},
                ],
                **optional_filters,
            }
        }

    @staticmethod
    def _reduce_task_fields(start: datetime, end: datetime) -> dict:
        """
        Leve the minimal need fields.
        Set started to the latest from started field and start parameter
        If the task is still running then set stopped to the current date
        otherwise to completed (or status_changed is completed does not exist)
        Since mongo date time is always stored as utc make sure to convert the incoming dates to UTC
        """
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        return {
            "$project": {
                "_id": 0,
                "project": 1,
                "user": 1,
                "queue": "$execution.queue",
                "started": {"$max": ["$started", start]},
                "stopped": {
                    "$cond": {
                        "if": {"$eq": ["$status", "in_progress"]},
                        "then": min(datetime.now(timezone.utc), end),
                        "else": {
                            "$min": [
                                {"$ifNull": ["$completed", "$status_changed"]},
                                end,
                            ]
                        },
                    }
                },
            }
        }

    @staticmethod
    def _merge_queue_resources():
        """
        Merge with the referenced queue and bring its resources
        """
        return {
            "$lookup": {
                "from": "queue",
                "localField": "queue",
                "foreignField": "_id",
                "as": "queues",
                "pipeline": [{"$project": {"resources": 1, "_id": 0}}],
            }
        }

    @classmethod
    def _add_dates_range_per_task(cls, tz_str: str):
        """
        Set cpu and gpu usage from the merged queue resources. Put default values if queue resources not defined
        Generate array of dates between start and end dates.
        Each date points to beginning of the day in the passed time zone
        """
        return {
            "$addFields": {
                "cpu_usage": {
                    "$ifNull": [
                        {"$first": "$queues.resources.cpu_usage"},
                        cls._default_cpu_usage,
                    ],
                },
                "def_cpu": {
                    "$cond": {
                        # $eq does not work here since 'undefined' value is separate from null
                        # for aggregation equality comparison
                        "if": {
                            "$lte": [{"$first": "$queues.resources.cpu_usage"}, None]
                        },
                        "then": 1,
                        "else": 0,
                    }
                },
                "gpu_usage": {
                    "$ifNull": [
                        {"$first": "$queues.resources.gpu_usage"},
                        cls._default_gpu_usage,
                    ],
                },
                "def_gpu": {
                    "$cond": {
                        "if": {
                            "$lte": [{"$first": "$queues.resources.gpu_usage"}, None]
                        },
                        "then": 1,
                        "else": 0,
                    }
                },
                "queues": "$$REMOVE",
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        1,
                                        {
                                            "$dateDiff": {
                                                "startDate": "$started",
                                                "endDate": "$stopped",
                                                "unit": "day",
                                                "timezone": tz_str,
                                            }
                                        },
                                    ]
                                },
                            ]
                        },
                        "in": {
                            "$dateTrunc": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": "$started",
                                        "unit": "day",
                                        "amount": "$$this",
                                        "timezone": tz_str,
                                    }
                                },
                                "unit": "day",
                                "timezone": tz_str,
                            }
                        },
                    }
                },
            }
        }

    @staticmethod
    def _split_by_date():
        """
        Generate a separate document for each task and date
        """
        return {"$unwind": {"path": "$dates"}}

    @staticmethod
    def _calc_task_usages_per_date(tz_str: str):
        """
        For each task date calculate its running time and resource usages
        Running time is calculated as amount of seconds that the task was running during the date
        If the task started before the date and stopped after the date then the usage on that date is 24h (86400 sec)
        Otherwise the exact running hours during the date are calculated
        """
        return {
            "$addFields": {
                "usage": {
                    "$let": {
                        "vars": {
                            "seconds": {
                                "$subtract": [
                                    {
                                        "$min": [
                                            {
                                                "$dateDiff": {
                                                    "startDate": "$dates",
                                                    "endDate": "$stopped",
                                                    "unit": "second",
                                                    "timezone": tz_str,
                                                }
                                            },
                                            86400,
                                        ]
                                    },
                                    {
                                        "$max": [
                                            {
                                                "$dateDiff": {
                                                    "startDate": "$dates",
                                                    "endDate": "$started",
                                                    "unit": "second",
                                                    "timezone": tz_str,
                                                }
                                            },
                                            0,
                                        ]
                                    },
                                ]
                            }
                        },
                        "in": {
                            "running_seconds": "$$seconds",
                            "cpu_usage": {"$multiply": ["$$seconds", "$cpu_usage"]},
                            "gpu_usage": {"$multiply": ["$$seconds", "$gpu_usage"]},
                        },
                    }
                },
                "started": "$$REMOVE",
                "stopped": "$$REMOVE",
                "cpu_usage": "$$REMOVE",
                "gpu_usage": "$$REMOVE",
            }
        }

    @staticmethod
    def _group_by_field(field: str):
        """
        Group task date documents by the passed field and date
        For each date calculates the total amount of seconds
        """
        return {
            "$group": {
                "_id": {"key": f"${field}", "date": "$dates"},
                "duration": {"$sum": "$usage.running_seconds"},
                "cpu_usage": {"$sum": "$usage.cpu_usage"},
                "gpu_usage": {"$sum": "$usage.gpu_usage"},
                "def_cpu": {"$max": "$def_cpu"},
                "def_gpu": {"$max": "$def_gpu"},
            }
        }

    @staticmethod
    def _flatten_group_key():
        return {
            "$addFields": {
                "key": "$_id.key",
                "date": "$_id.date",
                "_id": "$$REMOVE",
            }
        }

    @classmethod
    def _group_by_dates(cls, breakdown_keys: Sequence[str]):
        return {
            "$facet": {
                f"{field}s": [cls._group_by_field(field), cls._flatten_group_key()]
                for field in breakdown_keys
            }
        }

    @classmethod
    def _get_names(cls, ids, cls_: Type[Document]):
        if cls_ != Queue:
            return dict(cls_.objects(id__in=ids).scalar("id", "name"))

        q_names = {cls._other_q_id: "other"}
        q_names.update(
            {
                q_id: display_name or name
                for q_id, display_name, name in cls_.objects(id__in=ids).scalar(
                    "id", "display_name", "name"
                )
            }
        )
        return q_names

    @staticmethod
    def _get_date_from_iso_str(iso_str: str) -> date:
        try:
            date_str, _, _ = iso_str.partition("T")
            return date.fromisoformat(date_str)
        except Exception as ex:
            raise errors.bad_request.ValidationError(
                f"Invalid ISO 8601 date {iso_str}: {str(ex)}"
            )

    @classmethod
    def get_project_usages_internal(
        cls,
        company_id: str,
        from_date: datetime,
        to_date: datetime,
        include_development: bool,
        breakdown_keys: Sequence[str],
        usage_fields: Sequence[str],
        project_ids: Sequence[str] = None,
        projects_child_to_parent: Mapping[str, Sequence[str]] = None,
        allowed_queues: Sequence[str] = None,
    ):
        """
        This api does not check permissions and intended for internal use only
        If to_date and from_date contain timezones then the calculation will be done
        for that timezone otherwise for UTC
        """
        unsupported_breakdown_keys = set(breakdown_keys) - set(cls._field_to_cls)
        if unsupported_breakdown_keys:
            raise errors.bad_request.ValidationError(
                "Unsupported breakdown fields", fields=unsupported_breakdown_keys
            )

        if from_date.tzinfo is None:
            from_date = from_date.replace(tzinfo=timezone.utc)
        if to_date.tzinfo is None:
            to_date = to_date.replace(tzinfo=timezone.utc)
        request_tz_str = from_date.strftime("%z")
        request_tz = from_date.tzinfo
        start = datetime.combine(from_date, time.min, tzinfo=request_tz)
        end = datetime.combine(to_date, time.max, tzinfo=request_tz)

        pipeline = [
            cls._select_project_tasks(
                company_id,
                project_ids,
                start,
                end,
                include_development,
            ),
            cls._reduce_task_fields(start, end),
            cls._merge_queue_resources(),
            cls._add_dates_range_per_task(request_tz_str),
            cls._split_by_date(),
            cls._calc_task_usages_per_date(request_tz_str),
            cls._group_by_dates(breakdown_keys),
        ]
        res = list(Task.aggregate(pipeline))

        def get_parent_project_id(item: dict):
            p_id = item.get("key")
            return projects_child_to_parent.get(p_id, p_id)

        if allowed_queues is None:
            allowed_queues = list(
                Queue.objects(company__in=[company_id, "", None]).scalar("id")
            )

        def get_queue_id(item: dict):
            """
            If task is not enqueued or queue_id in allowed_queues then return queue_id
            Otherwise return 'other' queue id
            """
            q_id = item.get("key")
            if q_id is None:
                return q_id

            if q_id is None or q_id in allowed_queues:
                return q_id

            return cls._other_q_id

        ret = {}
        key_funcs = {
            Project: get_parent_project_id,
            Queue: get_queue_id,
        }
        for field in breakdown_keys:
            cls_ = cls._field_to_cls[field]
            data = res[0][f"{field}s"]
            key_func = key_funcs.get(cls_, lambda x: x.get("key", None))
            data_by_key = bucketize(data, key=key_func)
            key_values = set(data_by_key)
            if cls_ == Project and project_ids:
                key_values.update(project_ids)
            names = cls._get_names(key_values, cls_)

            field_ret = {
                "total": [],
                "series": [],
            }
            ret[f"{field}s"] = field_ret
            for key in key_values:
                key_data = data_by_key.get(key)
                if not key_data:
                    continue

                name = names.get(key, key)
                total_usages = defaultdict(int)
                dates = []
                date_usages = defaultdict(list)
                next_date = start
                for date_data in sorted(key_data, key=itemgetter("date")):
                    # the mongo aggregation dates are always returned as UTC
                    # need to convert them to the requested timezone
                    current_date = (
                        date_data["date"]
                        .replace(tzinfo=timezone.utc)
                        .astimezone(request_tz)
                    )
                    while current_date.date() > next_date.date():
                        dates.append(next_date.timestamp())
                        next_date += timedelta(days=1)
                        for usage in usage_fields:
                            date_usages[usage].append(0)

                    dates.append(current_date.timestamp())
                    next_date = current_date + timedelta(days=1)
                    for usage in usage_fields:
                        total_usages[usage] += date_data[usage]
                        date_usages[usage].append(date_data[usage])

                while next_date.date() <= end.date():
                    dates.append(next_date.timestamp())
                    next_date += timedelta(days=1)
                    for usage in usage_fields:
                        date_usages[usage].append(0)

                more_params = {}
                for usage in usage_fields:
                    usage_type, _, __ = usage.rpartition("_")
                    if not usage_type:
                        continue
                    def_field = f"def_{usage_type}"
                    if any(d.get(def_field) for d in key_data):
                        more_params[f"{usage_type}_artificial_weights"] = True

                field_ret["series"].append(
                    {
                        "name": name,
                        "id": key,
                        "dates": dates,
                        **date_usages,
                        **more_params,
                    }
                )
                field_ret["total"].append(
                    {
                        "name": name,
                        "id": key,
                        **total_usages,
                        **more_params,
                    }
                )

        return ret

    @staticmethod
    def fromisoformat(date_str: str) -> datetime:
        """
        Overcome python fromisoformat function limitations
        """
        if date_str.upper().endswith("Z"):
            date_str = date_str[:-1]

        return datetime.fromisoformat(date_str)

    @classmethod
    def get_project_usages(
        cls,
        company_id: str,
        project_ids: Sequence[str],
        from_date_str: str,
        to_date_str: str,
        include_development: bool,
        breakdown_keys: Sequence[str] = None,
        usage_fields: Sequence[str] = None,
    ):
        """
        Return project usage per day grouped by project, queue and user
        If several project ids were passed then group by those projects
        If no project ids passed then group by top level projects
        If only one project was passed then group by its top level children
        In the last 2 cases also calculate the usage for the tasks sitting directly under the root or passed project
        to_day_str and from_date_str should contain date-times in ISO format with the timezone
        """
        breakdown_keys = breakdown_keys or (
            UsageBreakdownKeys.project,
            UsageBreakdownKeys.queue,
            UsageBreakdownKeys.user,
        )
        usage_fields = usage_fields or (
            UsageAggFields.duration,
            UsageAggFields.gpu_usage,
        )
        from_date = cls.fromisoformat(from_date_str)
        to_date = cls.fromisoformat(to_date_str)
        if from_date > to_date:
            raise errors.bad_request.ValidationError(
                "from_date cannot be later than to_date"
            )

        empty = {
            f"{field}s": {
                "total": [],
                "series": [],
            }
            for field in breakdown_keys
        }

        add_root = False
        project_root = None
        if len(project_ids) == 1:
            add_root = True
            project_root = project_ids[0]
            project_ids = list(Project.objects(parent=project_root).scalar("id"))

        child_projects = _get_sub_projects(
            project_ids,
            search_hidden=True,
        )
        project_ids_with_children = list(
            set(project_ids)
            | {p.id for p in chain.from_iterable(child_projects.values())}
        )
        if add_root:
            project_ids_with_children.append(project_root)

        if not project_ids_with_children:
            return empty

        child_to_parent = {}
        if child_projects:
            child_to_parent = {
                child.id: parent_id
                for parent_id, children in child_projects.items()
                for child in children
            }

        return cls.get_project_usages_internal(
            company_id=company_id,
            from_date=from_date,
            to_date=to_date,
            include_development=include_development,
            project_ids=project_ids_with_children,
            projects_child_to_parent=child_to_parent,
            breakdown_keys=breakdown_keys,
            usage_fields=usage_fields,
        )
