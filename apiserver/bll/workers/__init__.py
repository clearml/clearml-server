import itertools
import re
from datetime import datetime, timedelta
from time import time
from typing import Sequence, Set, Optional

import attr
import elasticsearch.helpers
from boltons.iterutils import partition, chunked_iter
from mongoengine import Q
from pyhocon import ConfigTree

from apiserver.es_factory import es_factory
from apiserver.apierrors import APIError
from apiserver.apierrors.errors import bad_request, server_error
from apiserver.apimodels.workers import (
    IdNameEntry,
    WorkerEntry,
    StatusReportRequest,
    WorkerResponseEntry,
    QueueEntry,
    MachineStats, Resources,
)
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import User
from apiserver.database.model.company import Company
from apiserver.database.model.project import Project
from apiserver.database.model.queue import Queue
from apiserver.database.model.task.task import Task
from apiserver.redis_manager import redman
from apiserver.utilities.dicts import nested_get
from .stats import WorkerStats

log = config.logger(__file__)


class WorkerBLL:
    _key_regex_trans = str.maketrans({"*": ".*", "?": ".?"})

    def __init__(self, es=None, redis=None):
        self.es_client = es or es_factory.connect("workers")
        self.config = config.get("services.workers", ConfigTree())
        self.redis = redis or redman.connection("workers")
        self._stats = WorkerStats(self.es_client)

    @property
    def stats(self) -> WorkerStats:
        return self._stats

    def register_worker(
        self,
        company_id: str,
        user_id: str,
        worker: str,
        ip: str = "",
        queues: Sequence[str] = None,
        timeout: int = 0,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        resources: Resources = None,
    ) -> WorkerEntry:
        """
        Register a worker
        :param company_id: worker's company ID
        :param user_id: user ID under which this worker is running
        :param worker: worker ID
        :param ip: the real ip of the worker
        :param queues: queues reported as being monitored by the worker
        :param timeout: registration expiration timeout in seconds
        :param tags: a list of tags for this worker
        :raise bad_request.InvalidUserId: in case the calling user or company does not exist
        :return: worker entry instance
        """
        key = WorkerBLL._get_worker_key(company_id, user_id, worker)

        timeout = timeout or int(self.config.get("default_worker_timeout_sec", 10 * 60))
        queues = queues or []

        with translate_errors_context():
            query = dict(id=user_id, company=company_id)
            user = User.objects(**query).only("id", "name").first()
            if not user:
                raise bad_request.InvalidUserId(**query)
            company = Company.objects(id=company_id).only("id", "name").first()
            if not company:
                raise bad_request.InvalidId("invalid company", company=company_id)

            queues_query = Q(company=company_id, id__in=queues)
            queue_objs = Queue.objects(queues_query).only("id")
            if len(queue_objs) < len(queues):
                invalid = set(queues).difference(q.id for q in queue_objs)
                raise bad_request.InvalidQueueId(ids=invalid)

            if resources and queues:
                Queue.objects(queues_query).update(resources=resources.to_struct())

            now = datetime.utcnow()
            entry = WorkerEntry(
                key=key,
                id=worker,
                user=user.to_proper_dict(),
                company=company.to_proper_dict(),
                ip=ip,
                queues=queues,
                register_time=now,
                register_timeout=timeout,
                last_activity_time=now,
                tags=tags,
                system_tags=system_tags,
            )

            self._save_worker_data(entry)

        return entry

    def unregister_worker(self, company_id: str, user_id: str, worker: str) -> None:
        """
        Unregister a worker
        :param company_id: worker's company ID
        :param user_id: user ID under which this worker is running
        :param worker: worker ID
        :raise bad_request.WorkerNotRegistered: the worker was not previously registered
        """
        res = self.redis.delete(
            company_id, self._get_worker_key(company_id, user_id, worker)
        )
        if not res and not config.get("apiserver.workers.auto_unregister", False):
            raise bad_request.WorkerNotRegistered(worker=worker)

    def status_report(
        self,
        company_id: str,
        user_id: str,
        ip: str,
        report: StatusReportRequest,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
    ) -> None:
        """
        Write worker status report
        :param company_id: worker's company ID
        :param user_id: user_id ID under which this worker is running
        :param ip: worker IP
        :param report: the report itself
        :param tags: tags for this worker
        :raise bad_request.InvalidTaskId: the reported task was not found
        :return: worker entry instance
        """
        entry = self._get_worker(company_id, user_id, report.worker)

        try:
            entry.ip = ip

            if tags is not None:
                entry.tags = tags
            if system_tags is not None:
                entry.system_tags = system_tags

            if report.machine_stats:
                self.log_stats_to_es(
                    company_id=company_id,
                    worker_id=report.worker,
                    timestamp=report.timestamp,
                    task=report.task,
                    machine_stats=report.machine_stats,
                )

            now = datetime.utcnow()
            entry.last_activity_time = now
            entry.queue = report.queue

            if report.queues:
                entry.queues = report.queues

            if not report.task:
                entry.task = None
                entry.project = None
            else:
                with translate_errors_context():
                    query = dict(id=report.task, company=company_id)
                    update = dict(
                        last_worker=report.worker,
                        last_worker_report=now,
                        last_update=now,
                        last_change=now,
                        last_changed_by=user_id,
                    )
                    # modify(new=True, ...) returns the modified object
                    task = Task.objects(**query).modify(new=True, **update)
                    if not task:
                        raise bad_request.InvalidTaskId(**query)
                    entry.task = IdNameEntry(id=task.id, name=task.name)

                    entry.project = None
                    if task.project:
                        project = Project.objects(id=task.project).only("name").first()
                        if project:
                            entry.project = IdNameEntry(
                                id=project.id, name=project.name
                            )

            entry.last_report_time = now
        except APIError:
            raise
        except Exception as e:
            msg = "Failed processing worker status report"
            log.exception(msg)
            raise server_error.DataError(msg, err=e.args[0])
        finally:
            self._save_worker(entry)

    def get_count(
        self,
        company_id: str,
        last_seen: Optional[int] = None,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        worker_pattern: str = None,
    ):
        if not last_seen:
            return len(
                self._get_keys(
                    company_id,
                    user_tags=tags,
                    system_tags=system_tags,
                    worker_pattern=worker_pattern,
                )
            )

        return len(
            self.get_all(
                company_id,
                last_seen=last_seen,
                tags=tags,
                system_tags=system_tags,
                worker_pattern=worker_pattern,
            )
        )

    def get_all(
        self,
        company_id: str,
        last_seen: Optional[int] = None,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        worker_pattern: str = None,
    ) -> Sequence[WorkerEntry]:
        """
        Get all the company workers that were active during the last_seen period
        :param company_id: worker's company id
        :param last_seen: period in seconds to check. Min value is 1 second
        :return:
        """
        try:
            workers = self._get(
                company_id,
                user_tags=tags,
                system_tags=system_tags,
                worker_pattern=worker_pattern,
            )
        except Exception as e:
            raise server_error.DataError("failed loading worker entries", err=e.args[0])

        if last_seen:
            ref_time = datetime.utcnow() - timedelta(seconds=max(1, last_seen))
            workers = [
                w
                for w in workers
                if w.last_activity_time.replace(tzinfo=None) >= ref_time
            ]

        return workers

    def get_all_with_projection(
        self,
        company_id: str,
        last_seen: int,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        worker_pattern: str = None,
    ) -> Sequence[WorkerResponseEntry]:
        helpers = [
            WorkerConversionHelper.from_worker_entry(entry)
            for entry in self.get_all(
                company_id=company_id,
                last_seen=last_seen,
                tags=tags,
                system_tags=system_tags,
                worker_pattern=worker_pattern,
            )
        ]

        task_ids = set(filter(None, (helper.task_id for helper in helpers)))
        all_queues = set(
            itertools.chain.from_iterable(helper.queue_ids for helper in helpers)
        )

        queues_info = {}
        if all_queues:
            projection = [
                {"$match": {"_id": {"$in": list(all_queues)}}},
                {
                    "$project": {
                        "name": 1,
                        "display_name": 1,
                        "next_entry": {"$arrayElemAt": ["$entries", 0]},
                        "num_entries": {"$size": "$entries"},
                    }
                },
            ]
            queues_info = {res["_id"]: res for res in Queue.aggregate(projection)}
            task_ids = task_ids.union(
                filter(
                    None,
                    (
                        nested_get(info, ("next_entry", "task"))
                        for info in queues_info.values()
                    ),
                )
            )

        tasks_info = {}
        if task_ids:
            tasks_info = {
                task.id: task
                for task in Task.objects(id__in=task_ids).only(
                    "name", "started", "last_iteration", "active_duration"
                )
            }

        def update_queue_entries(*entries):
            for entry in entries:
                if not entry:
                    continue
                info = queues_info.get(entry.id, None)
                if not info:
                    continue
                entry.name = info.get("name", None)
                entry.display_name = info.get("display_name", None)
                entry.num_tasks = info.get("num_entries", 0)
                task_id = nested_get(info, ("next_entry", "task"))
                if task_id:
                    task = tasks_info.get(task_id, None)
                    entry.next_task = IdNameEntry(
                        id=task_id, name=task.name if task else None
                    )

        for helper in helpers:
            worker = helper.worker
            if helper.task_id:
                task: Task = tasks_info.get(helper.task_id, None)
                if task:
                    worker.task.running_time = (task.active_duration or 0) * 1000
                    worker.task.last_iteration = task.last_iteration

            update_queue_entries(worker.queue)
            if worker.queues:
                update_queue_entries(*worker.queues)

        return [helper.worker for helper in helpers]

    @staticmethod
    def _get_worker_key(company: str, user: str, worker_id: str) -> str:
        """Build redis key from company, user and worker_id"""
        return f"worker_{company}_{user}_{worker_id}"

    def _get_worker(self, company_id: str, user_id: str, worker: str) -> WorkerEntry:
        """
        Get a worker entry for the provided worker ID. The entry is loaded from Redis
        if it exists (i.e. worker has already been registered), otherwise the worker
        is registered and its entry stored into Redis).
        :param company_id: worker's company ID
        :param user_id: user ID under which this worker is running
        :param worker: worker ID
        :raise bad_request.InvalidWorkerId: in case the worker id was not found
        :return: worker entry instance
        """
        key = self._get_worker_key(company_id, user_id, worker)

        data = self.redis.get(key)

        if data:
            try:
                entry = WorkerEntry.from_json(data)
                if not entry.key:
                    entry.key = key
                    self._save_worker(entry)
                return entry
            except Exception as e:
                msg = "Failed parsing worker entry"
                log.exception(msg)
                raise server_error.DataError(msg, err=e.args[0])

        # Failed loading worker from Redis
        if config.get("apiserver.workers.auto_register", False):
            try:
                return self.register_worker(company_id, user_id, worker)
            except Exception:
                log.error(
                    "Failed auto registration of {} for company {}".format(
                        worker, company_id
                    )
                )

        raise bad_request.InvalidWorkerId(worker=worker)

    @staticmethod
    def _get_tagged_workers_key(company: str, tags_field: str, tag: str) -> str:
        """Build redis key from company, user and worker_id"""
        return f"workers.{tags_field}_{company}_{tag}"

    @staticmethod
    def _get_all_workers_key(company: str) -> str:
        """Build redis key from company, user and worker_id"""
        return f"workers_{company}"

    def _save_worker_data(self, entry: WorkerEntry):
        self.redis.setex(
            entry.key, timedelta(seconds=entry.register_timeout), entry.to_json()
        )
        company_id = entry.company.id
        expiration = int(time()) + entry.register_timeout
        worker_item = {entry.key: expiration}
        self.redis.zadd(self._get_all_workers_key(company_id), worker_item)
        for tags, tags_field in (
            (entry.tags, "tags"),
            (entry.system_tags, "systemtags"),
        ):
            for tag in tags:
                name = self._get_tagged_workers_key(company_id, tags_field, tag)
                self.redis.zadd(name, worker_item)

    def _save_worker(self, entry: WorkerEntry) -> None:
        """Save worker entry in Redis"""
        try:
            self._save_worker_data(entry)
        except Exception:
            msg = "Failed saving worker entry"
            log.exception(msg)

    def _get_keys(
        self,
        company: str,
        user: str = "*",
        user_tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        worker_pattern: str = None,
    ) -> Sequence[bytes]:
        if not (user_tags or system_tags):
            match = self._get_worker_key(company, user, worker_pattern or "*")
            return list(self.redis.scan_iter(match))

        def filter_by_user_and_pattern(in_keys: Set[bytes]) -> Set[bytes]:
            if user != "*":
                user_bytes = user.encode()
                in_keys = {k for k in in_keys if user_bytes in k}

            if worker_pattern:
                worker_pattern_bytes = (
                    f"{worker_pattern.translate(self._key_regex_trans)}$".encode()
                )
                regex = re.compile(worker_pattern_bytes)
                in_keys = {k for k in in_keys if regex.search(k)}

            return in_keys

        worker_keys = set()
        for tags, tags_field in (
            (user_tags, "tags"),
            (system_tags, "systemtags"),
        ):
            if not tags:
                continue

            timestamp = int(time())
            include, exclude = partition(tags, key=lambda x: x[0] != "-")
            if include:
                tagged_workers = set()
                for tag in include:
                    tagged_workers_key = self._get_tagged_workers_key(
                        company, tags_field, tag
                    )
                    self.redis.zremrangebyscore(
                        tagged_workers_key, min=0, max=timestamp
                    )
                    tagged_workers.update(self.redis.zrange(tagged_workers_key, 0, -1))

                tagged_workers = filter_by_user_and_pattern(tagged_workers)
                worker_keys = (
                    worker_keys.intersection(tagged_workers)
                    if worker_keys
                    else tagged_workers
                )
                if not worker_keys:
                    return []

            if exclude:
                if not worker_keys:
                    all_workers_key = self._get_all_workers_key(company)
                    self.redis.zremrangebyscore(all_workers_key, min=0, max=timestamp)
                    worker_keys.update(self.redis.zrange(all_workers_key, 0, -1))
                    worker_keys = filter_by_user_and_pattern(worker_keys)
                    if not worker_keys:
                        return []

                for tag in exclude:
                    tagged_workers_key = self._get_tagged_workers_key(
                        company, tags_field, tag[1:]
                    )
                    self.redis.zremrangebyscore(
                        tagged_workers_key, min=0, max=timestamp
                    )
                    worker_keys.difference_update(
                        self.redis.zrange(tagged_workers_key, 0, -1)
                    )
                    if not worker_keys:
                        return []

        return list(worker_keys)

    def _get(
        self,
        company: str,
        user: str = "*",
        user_tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        worker_pattern: str = None,
    ) -> Sequence[WorkerEntry]:
        """Get worker entries matching the company and user, worker patterns"""

        entries = []
        for keys in chunked_iter(
            self._get_keys(
                company,
                user=user,
                user_tags=user_tags,
                system_tags=system_tags,
                worker_pattern=worker_pattern,
            ),
            1000,
        ):
            data = self.redis.mget(keys)
            if data:
                entries.extend(WorkerEntry.from_json(d) for d in data if d)

        return entries

    @staticmethod
    def _get_es_index_suffix():
        """Get the index name suffix for storing current month data"""
        return datetime.utcnow().strftime("%Y-%m")

    def log_stats_to_es(
        self,
        company_id: str,
        worker_id: str,
        timestamp: int,
        task: str,
        machine_stats: MachineStats,
    ) -> int:
        """
        Actually writing the worker statistics to Elastic
        :return: The amount of logged documents
        """
        es_index = (
            f"{self._stats.worker_stats_prefix_for_company(company_id)}"
            f"{self._get_es_index_suffix()}"
        )

        def make_doc(category, metric, variant, value) -> dict:
            return dict(
                _index=es_index,
                _source=dict(
                    timestamp=timestamp,
                    worker=worker_id,
                    task=task,
                    category=category,
                    metric=metric,
                    variant=variant,
                    value=float(value),
                ),
            )

        actions = []
        for field, value in machine_stats.to_struct().items():
            if not value:
                continue
            category = field.partition("_")[0]
            metric = field
            if not isinstance(value, (list, tuple)):
                actions.append(make_doc(category, metric, "total", value))
            else:
                actions.extend(
                    make_doc(category, metric, str(i), val)
                    for i, val in enumerate(value)
                )

        es_res = elasticsearch.helpers.bulk(self.es_client, actions)
        added, errors = es_res[:2]
        return added


@attr.s(auto_attribs=True)
class WorkerConversionHelper:
    worker: WorkerResponseEntry
    task_id: str
    queue_ids: Set[str]

    @classmethod
    def from_worker_entry(cls, worker: WorkerEntry):
        data = worker.to_struct()
        queue = data.pop("queue", None) or None
        queue_ids = set(data.pop("queues", []))
        queues = [QueueEntry(id=id) for id in queue_ids]
        if queue:
            queue = next((q for q in queues if q.id == queue), None)
        return cls(
            worker=WorkerResponseEntry(queues=queues, queue=queue, **data),
            task_id=worker.task.id if worker.task else None,
            queue_ids=queue_ids,
        )
