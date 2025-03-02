from collections import defaultdict
from datetime import datetime
from typing import Sequence, Optional, Tuple, Union, Iterable

from elasticsearch import Elasticsearch
from mongoengine import Q

from apiserver import database
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.es_factory import es_factory
from apiserver.apierrors import errors
from apiserver.bll.queue.queue_metrics import QueueMetrics
from apiserver.bll.workers import WorkerBLL
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.queue import Queue, Entry

log = config.logger(__file__)
MOVE_FIRST = "first"
MOVE_LAST = "last"


class QueueBLL(object):
    def __init__(self, worker_bll: WorkerBLL = None, es: Elasticsearch = None):
        self.worker_bll = worker_bll or WorkerBLL()
        self.es = es or es_factory.connect("workers")
        self._metrics = QueueMetrics(self.es)

    @property
    def metrics(self) -> QueueMetrics:
        return self._metrics

    @staticmethod
    def create(
        company_id: str,
        name: str,
        display_name: str = None,
        tags: Optional[Sequence[str]] = None,
        system_tags: Optional[Sequence[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Queue:
        """Creates a queue"""
        with translate_errors_context():
            now = datetime.utcnow()
            queue = Queue(
                id=database.utils.id(),
                company=company_id,
                created=now,
                name=name,
                display_name=display_name,
                tags=tags or [],
                system_tags=system_tags or [],
                metadata=metadata,
                last_update=now,
            )
            queue.save()
            return queue

    def get_by_name(
        self, company_id: str, queue_name: str, only: Optional[Sequence[str]] = None,
    ) -> Queue:
        qs = Queue.objects(name=queue_name, company=company_id)
        if only:
            qs = qs.only(*only)

        return qs.first()

    @staticmethod
    def _get_task_entries_projection(max_task_entries: int) -> dict:
        return dict(slice__entries=max_task_entries)

    def get_by_id(
        self,
        company_id: str,
        queue_id: str,
        only: Optional[Sequence[str]] = None,
        max_task_entries: int = None,
    ) -> Queue:
        """
        Get queue by id
        :raise errors.bad_request.InvalidQueueId: if the queue is not found
        """
        with translate_errors_context():
            query = dict(id=queue_id, company=company_id)
            qs = Queue.objects(**query)
            if only:
                qs = qs.only(*only)
            if max_task_entries:
                qs = qs.fields(**self._get_task_entries_projection(max_task_entries))
            queue = qs.first()
            if not queue:
                raise errors.bad_request.InvalidQueueId(**query)

            return queue

    @classmethod
    def get_queue_with_task(cls, company_id: str, queue_id: str, task_id: str) -> Queue:
        with translate_errors_context():
            query = dict(id=queue_id, company=company_id)
            queue = Queue.objects(entries__task=task_id, **query).first()
            if not queue:
                raise errors.bad_request.InvalidQueueOrTaskNotQueued(
                    task=task_id, **query
                )

            return queue

    def get_default(self, company_id: str) -> Queue:
        """
        Get the default queue
        :raise errors.bad_request.NoDefaultQueue: if the default queue not found
        :raise errors.bad_request.MultipleDefaultQueues: if more than one default queue is found
        """
        with translate_errors_context():
            res = Queue.objects(company=company_id, system_tags="default").only(
                "id", "name"
            )
            if not res:
                raise errors.bad_request.NoDefaultQueue()
            if len(res) > 1:
                raise errors.bad_request.MultipleDefaultQueues(
                    queues=tuple(r.id for r in res)
                )

            return res.first()

    def update(
        self, company_id: str, queue_id: str, **update_fields
    ) -> Tuple[int, dict]:
        """
        Partial update of the queue from update_fields
        :raise errors.bad_request.InvalidQueueId: if the queue is not found
        :return: number of updated objects and updated fields dictionary
        """
        with translate_errors_context():
            # validate the queue exists
            self.get_by_id(company_id=company_id, queue_id=queue_id, only=("id",))
            return Queue.safe_update(company_id, queue_id, update_fields)

    def _update_task_status_on_removal_from_queue(
        self,
        company_id: str,
        user_id: str,
        task_ids: Iterable[str],
        queue_id: str,
        reason: str
    ) -> Sequence[str]:
        from apiserver.bll.task import ChangeStatusRequest
        tasks = []
        for task_id in task_ids:
            try:
                task = Task.get(
                    company=company_id,
                    id=task_id,
                    execution__queue=queue_id,
                    _only=[
                        "id",
                        "company",
                        "status",
                        "enqueue_status",
                        "project",
                    ],
                )
                if not task:
                    continue

                tasks.append(task.id)
                ChangeStatusRequest(
                    task=task,
                    new_status=task.enqueue_status or TaskStatus.created,
                    status_reason=reason,
                    status_message="",
                    user_id=user_id,
                    force=True,
                ).execute(
                    enqueue_status=None,
                    unset__execution__queue=1,
                )
            except Exception as ex:
                log.error(
                    f"Failed updating task {task_id} status on removal from queue: {queue_id}, {str(ex)}"
                )

        return tasks

    def delete(self, company_id: str, user_id: str, queue_id: str, force: bool) -> Sequence[str]:
        """
        Delete the queue
        :raise errors.bad_request.InvalidQueueId: if the queue is not found
        :raise errors.bad_request.QueueNotEmpty: if the queue is not empty and 'force' not set
        """
        queue = self.get_by_id(company_id=company_id, queue_id=queue_id)
        if not queue.entries:
            queue.delete()
            return []

        if not force:
            raise errors.bad_request.QueueNotEmpty(
                "use force=true to delete", id=queue_id
            )

        tasks = self._update_task_status_on_removal_from_queue(
            company_id=company_id,
            user_id=user_id,
            task_ids={item.task for item in queue.entries},
            queue_id=queue_id,
            reason=f"Queue {queue_id} was deleted",
        )

        queue.delete()
        return tasks

    def get_all(
        self,
        company_id: str,
        query_dict: dict,
        query: Q = None,
        max_task_entries: int = None,
        ret_params: dict = None,
    ) -> Sequence[dict]:
        """Get all the queues according to the query"""
        with translate_errors_context():
            return Queue.get_many(
                company=company_id,
                parameters=query_dict,
                query_dict=query_dict,
                query=query,
                projection_fields=self._get_task_entries_projection(max_task_entries)
                if max_task_entries
                else None,
                ret_params=ret_params,
            )

    def check_for_workers(self, company_id: str, queue_id: str) -> bool:
        for worker in self.worker_bll.get_all(company_id):
            if queue_id in worker.queues:
                return True

        return False

    def get_queue_infos(
        self,
        company_id: str,
        query_dict: dict,
        query: Q = None,
        max_task_entries: int = None,
        ret_params: dict = None,
    ) -> Sequence[dict]:
        """
        Get infos on all the company queues, including queue tasks and workers
        """
        projection = Queue.get_extra_projection("entries.task.name")
        with translate_errors_context():
            res = Queue.get_many_with_join(
                company=company_id,
                query_dict=query_dict,
                query=query,
                override_projection=projection,
                projection_fields=self._get_task_entries_projection(max_task_entries)
                if max_task_entries
                else None,
                ret_params=ret_params,
            )

            queue_workers = defaultdict(list)
            for worker in self.worker_bll.get_all(company_id):
                for queue in worker.queues:
                    queue_workers[queue].append(worker)

            for item in res:
                item["workers"] = [
                    {
                        "name": w.id,
                        "ip": w.ip,
                        "key": w.key,
                        "task": w.task.to_struct() if w.task else None,
                    }
                    for w in queue_workers.get(item["id"], [])
                ]

        return res

    def add_task(self, company_id: str, queue_id: str, task_id: str) -> dict:
        """
        Add the task to the queue and return the queue update results
        :raise errors.bad_request.TaskAlreadyQueued: if the task is already in the queue
        :raise errors.bad_request.InvalidQueueOrTaskNotQueued: if the queue update operation failed
        """
        with translate_errors_context():
            queue = self.get_by_id(company_id=company_id, queue_id=queue_id)
            if any(e.task == task_id for e in queue.entries):
                raise errors.bad_request.TaskAlreadyQueued(task=task_id)

            entry = Entry(added=datetime.utcnow(), task=task_id)
            query = dict(id=queue_id, company=company_id)
            res = Queue.objects(entries__task__ne=task_id, **query).update_one(
                push__entries=entry, last_update=datetime.utcnow(), upsert=False
            )

            queue.reload()
            self.metrics.log_queue_metrics_to_es(company_id=company_id, queues=[queue])

            if not res:
                raise errors.bad_request.InvalidQueueOrTaskNotQueued(
                    task=task_id, **query
                )

            return res

    def get_next_task(
        self, company_id: str, queue_id: str, task_id: str = None
    ) -> Optional[Entry]:
        """
        Atomically pop and return the first task from the queue (or None)
        :raise errors.bad_request.InvalidQueueId: if the queue does not exist
        """
        with translate_errors_context():
            query = dict(id=queue_id, company=company_id)
            queue = Queue.objects(
                **query, **({"entries__0__task": task_id} if task_id else {})
            ).modify(pop__entries=-1, upsert=False)
            if not queue:
                if not task_id or not Queue.objects(**query).first():
                    raise errors.bad_request.InvalidQueueId(**query)
                return

            self.metrics.log_queue_metrics_to_es(company_id, queues=[queue])

            if not queue.entries:
                return

            try:
                Queue.objects(**query).update(last_update=datetime.utcnow())
            except Exception:
                log.exception("Error while updating Queue.last_update")

            return queue.entries[0]

    def clear_queue(
        self,
        company_id: str,
        user_id: str,
        queue_id: str,
    ):
        queue = Queue.objects(company=company_id, id=queue_id).first()
        if not queue:
            raise errors.bad_request.InvalidQueueId(
                queue=queue_id
            )

        if not queue.entries:
            return []

        tasks = self._update_task_status_on_removal_from_queue(
            company_id=company_id,
            user_id=user_id,
            task_ids={item.task for item in queue.entries},
            queue_id=queue_id,
            reason=f"Queue {queue_id} was cleared",
        )

        queue.update(entries=[])
        queue.reload()
        self.metrics.log_queue_metrics_to_es(company_id=company_id, queues=[queue])

        return tasks

    def remove_task(self, company_id: str, user_id: str, queue_id: str, task_id: str, update_task_status: bool = False) -> int:
        """
        Removes the task from the queue and returns the number of removed items
        :raise errors.bad_request.InvalidQueueOrTaskNotQueued: if the task is not found in the queue
        """
        with translate_errors_context():
            queue = self.get_queue_with_task(
                company_id=company_id, queue_id=queue_id, task_id=task_id
            )

            entries_to_remove = [e for e in queue.entries if e.task == task_id]
            query = dict(id=queue_id, company=company_id)
            res = Queue.objects(entries__task=task_id, **query).update_one(
                pull_all__entries=entries_to_remove, last_update=datetime.utcnow()
            )
            if res and update_task_status:
                self._update_task_status_on_removal_from_queue(
                    company_id=company_id,
                    user_id=user_id,
                    task_ids=[task_id],
                    queue_id=queue_id,
                    reason=f"Task was removed from the queue {queue_id}",
                )

            queue.reload()
            self.metrics.log_queue_metrics_to_es(company_id=company_id, queues=[queue])

            return len(entries_to_remove) if res else 0

    def reposition_task(
        self, company_id: str, queue_id: str, task_id: str, move_count: Union[int, str],
    ) -> int:
        """
        Moves the task in the queue to the position calculated by pos_func
        Returns the updated task position in the queue
        """

        def get_queue_and_task_position():
            q = self.get_queue_with_task(
                company_id=company_id, queue_id=queue_id, task_id=task_id
            )
            return q, next(i for i, e in enumerate(q.entries) if e.task == task_id)

        with translate_errors_context():
            queue, position = get_queue_and_task_position()
            if move_count == MOVE_FIRST:
                new_position = 0
            elif move_count == MOVE_LAST:
                new_position = len(queue.entries) - 1
            else:
                new_position = position + move_count
            if new_position == position:
                return new_position

            without_entry = {
                "$filter": {
                    "input": "$entries",
                    "as": "entry",
                    "cond": {"$ne": ["$$entry.task", task_id]},
                }
            }
            task_entry = {
                "$filter": {
                    "input": "$entries",
                    "as": "entry",
                    "cond": {"$eq": ["$$entry.task", task_id]},
                }
            }
            if move_count == MOVE_FIRST:
                operations = [
                    {
                        "$set": {
                            "entries": {"$concatArrays": [task_entry, without_entry]}
                        }
                    }
                ]
            elif move_count == MOVE_LAST:
                operations = [
                    {
                        "$set": {
                            "entries": {"$concatArrays": [without_entry, task_entry]}
                        }
                    }
                ]
            else:
                operations = [
                    {
                        "$set": {
                            "new_pos": {
                                "$add": [
                                    {"$indexOfArray": ["$entries.task", task_id]},
                                    move_count,
                                ]
                            },
                            "without_entry": without_entry,
                            "task_entry": task_entry,
                        }
                    },
                    {
                        "$set": {
                            "entries": {
                                "$switch": {
                                    "branches": [
                                        {
                                            "case": {"$lte": ["$new_pos", 0]},
                                            "then": {
                                                "$concatArrays": [
                                                    "$task_entry",
                                                    "$without_entry",
                                                ]
                                            },
                                        },
                                        {
                                            "case": {
                                                "$gte": [
                                                    "$new_pos",
                                                    {"$size": "$without_entry"},
                                                ]
                                            },
                                            "then": {
                                                "$concatArrays": [
                                                    "$without_entry",
                                                    "$task_entry",
                                                ]
                                            },
                                        },
                                    ],
                                    "default": {
                                        "$concatArrays": [
                                            {"$slice": ["$without_entry", "$new_pos"]},
                                            "$task_entry",
                                            {
                                                "$slice": [
                                                    "$without_entry",
                                                    "$new_pos",
                                                    {"$size": "$without_entry"},
                                                ]
                                            },
                                        ]
                                    },
                                }
                            }
                        }
                    },
                    {"$unset": ["new_pos", "without_entry", "task_entry"]},
                ]

            updated = Queue.objects(
                id=queue_id, company=company_id, entries__task=task_id
            ).update_one(__raw__=operations)

            if not updated:
                raise errors.bad_request.FailedAddingDuringReposition(task=task_id)

            return get_queue_and_task_position()[1]

    def count_entries(self, company: str, queue_id: str) -> Optional[int]:
        res = next(
            Queue.aggregate(
                [
                    {
                        "$match": {
                            "company": {"$in": ["", company]},
                            "_id": queue_id,
                        }
                    },
                    {"$project": {"count": {"$size": "$entries"}}},
                ]
            ),
            None,
        )
        if res is None:
            raise errors.bad_request.InvalidQueueId(queue_id=queue_id)
        return int(res.get("count"))
