import time
from datetime import timedelta
from operator import itemgetter
from typing import Sequence

from apiserver.tests.api_client import AttrDict
from apiserver.tests.automated import TestService, utc_now_tz_aware


class TestQueues(TestService):
    def setUp(self, **kwargs):
        super().setUp(**kwargs)
        self.user = self.api.users.get_current_user().user

    def test_default_queue(self):
        res = self.api.queues.get_default()
        self.assertIsNotNone(res.id)

    def test_create_update_delete(self):
        queue = self._temp_queue("TempTest", tags=["hello", "world"])
        res = self.api.queues.update(queue=queue, tags=["test"])
        assert res.updated == 1
        assert res.fields.tags == ["test"]

    def test_queue_metrics(self):
        queue_id = self._temp_queue("TestTempQueue")
        self._create_temp_queued_task("temp task 1", queue_id)
        time.sleep(1)
        task2 = self._create_temp_queued_task("temp task 2", queue_id)
        self.api.queues.get_next_task(queue=queue_id)
        self.api.queues.remove_task(queue=queue_id, task=task2["id"])
        to_date = utc_now_tz_aware()
        from_date = to_date - timedelta(hours=1)
        res = self.api.queues.get_queue_metrics(
            queue_ids=[queue_id],
            from_date=from_date.timestamp(),
            to_date=to_date.timestamp(),
            interval=5,
        )
        self.assertMetricQueues(res["queues"], queue_id)

    def test_add_remove_clear(self):
        queue1 = self._temp_queue("TestTempQueue1")
        queue2 = self._temp_queue("TestTempQueue2")

        task_names = ["TempDevTask1", "TempDevTask2"]
        tasks = [self._temp_task(name) for name in task_names]

        for task in tasks:
            self.api.tasks.enqueue(task=task, queue=queue1)

        # remove task with and without status update
        res = self.api.queues.remove_task(task=tasks[0], queue=queue1)
        self.assertEqual(res.removed, 1)
        res = self.api.tasks.get_by_id(task=tasks[0])
        self.assertEqual(res.task.status, "queued")
        self.assertEqual(res.task.execution.queue, queue1)

        res = self.api.queues.remove_task(task=tasks[1], queue=queue1, update_task_status=True)
        self.assertEqual(res.removed, 1)
        res = self.api.tasks.get_by_id(task=tasks[1])
        self.assertEqual(res.task.status, "created")

        res = self.api.queues.get_by_id(queue=queue1)
        self.assertQueueTasks(res.queue, [])

        # add task
        res = self.api.queues.add_task(queue=queue2, task=tasks[0])
        self.assertEqual(res.added, 1)
        res = self.api.tasks.get_by_id(task=tasks[0])
        self.assertEqual(res.task.status, "queued")
        self.assertEqual(res.task.execution.queue, queue2)

        res = self.api.queues.get_by_id(queue=queue2)
        self.assertQueueTasks(res.queue, [tasks[0]])

        # clear queue
        res = self.api.queues.clear_queue(queue=queue1)
        self.assertEqual(res.removed_tasks, [])
        res = self.api.queues.clear_queue(queue=queue2)
        self.assertEqual(res.removed_tasks, [tasks[0]])

        res = self.api.tasks.get_by_id(task=tasks[0])
        self.assertEqual(res.task.status, "created")

        res = self.api.queues.get_by_id(queue=queue2)
        self.assertQueueTasks(res.queue, [])

    def test_hidden_queues(self):
        hidden_name = "TestHiddenQueue"
        hidden_queue = self._temp_queue(hidden_name, system_tags=["k8s-glue"])
        non_hidden_queue = self._temp_queue("TestNonHiddenQueue")

        queues = self.api.queues.get_all_ex().queues
        ids = {q.id for q in queues}
        self.assertFalse(hidden_queue in ids)
        self.assertTrue(non_hidden_queue in ids)

        queues = self.api.queues.get_all_ex(search_hidden=True).queues
        ids = {q.id for q in queues}
        self.assertTrue(hidden_queue in ids)
        self.assertTrue(non_hidden_queue in ids)

        queues = self.api.queues.get_all_ex(name=f"^{hidden_name}$").queues
        self.assertEqual(hidden_queue, queues[0].id)

        queues = self.api.queues.get_all_ex(id=[hidden_queue]).queues
        self.assertEqual(hidden_queue, queues[0].id)

    def test_reset_task(self):
        queue = self._temp_queue("TestTempQueue")
        task = self._temp_task("TempTask", is_development=True)

        self.api.tasks.enqueue(task=task, queue=queue)
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, [task])

        res = self.api.tasks.reset(task=task)
        self.assertEqual(res.dequeued.removed, 1)
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, [])

    def test_enqueue_dev_task(self):
        queue = self._temp_queue("TestTempQueue")
        task_name = "TempDevTask"
        task = self._temp_task(task_name, is_development=True)
        self.assertTaskTags(task, system_tags=["development"])

        self.api.tasks.enqueue(task=task, queue=queue)
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, [task])
        self.assertTaskTags(task, system_tags=[])
        self.api.tasks.dequeue(task=task, new_status="published")
        res = self.api.tasks.get_by_id(task=task)
        self.assertEqual(res.task.status, "published")

    def test_dequeue_not_queued_task(self):
        # queue = self._temp_queue("TestTempQueue")
        task_name = "TempDevTask"
        task = self._temp_task(task_name)
        self.api.tasks.edit(task=task, status="queued")  # , execution={"queue": queue})
        res = self.api.tasks.get_by_id(task=task)
        self.assertEqual(res.task.status, "queued")

        self.api.tasks.dequeue(task=task)
        res = self.api.tasks.get_by_id(task=task)
        self.assertEqual(res.task.status, "created")

    def test_dequeue_from_deleted_queue(self):
        queue = self._temp_queue("TestTempQueue")
        task_name = "TempDevTask"
        task = self._temp_task(task_name)

        self.api.tasks.enqueue(task=task, queue=queue)
        res = self.api.tasks.get_by_id(task=task)
        self.assertEqual(res.task.status, "queued")

        self.api.queues.delete(queue=queue, force=True)
        res = self.api.tasks.get_by_id(task=task)
        self.assertEqual(res.task.status, "created")

    def test_max_queue_entries(self):
        queue = self._temp_queue("TestTempQueue")
        tasks = [
            self._create_temp_queued_task(t, queue)["id"]
            for t in ("temp task1", "temp task2", "temp task3")
        ]

        num = self.api.queues.get_num_entries(queue=queue).num
        self.assertEqual(num, 3)

        task_id = self.api.queues.peek_task(queue=queue).task
        self.assertEqual(task_id, tasks[0])

        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, tasks)

        res = self.api.queues.get_all(id=[queue]).queues[0]
        self.assertQueueTasks(res, tasks)

        res = self.api.queues.get_all(id=[queue], max_task_entries=2).queues[0]
        self.assertQueueTasks(res, tasks[:2])

        res = self.api.queues.get_all_ex(id=[queue]).queues[0]
        self.assertEqual([e.task.id for e in res.entries], tasks)

        res = self.api.queues.get_all_ex(id=[queue], max_task_entries=2).queues[0]
        self.assertEqual([e.task.id for e in res.entries], tasks[:2])

    def test_move_task(self):
        queue = self._temp_queue("TestTempQueue")
        tasks = [
            self._create_temp_queued_task(t, queue)["id"]
            for t in ("temp task1", "temp task2", "temp task3", "temp task4")
        ]
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, tasks)

        # no change in position
        new_pos = self.api.queues.move_task_to_front(
            queue=queue, task=tasks[0]
        ).position
        self.assertEqual(new_pos, 0)
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, tasks)

        # move backwards in the middle
        new_pos = self.api.queues.move_task_backward(
            queue=queue, task=tasks[0], count=2
        ).position
        self.assertEqual(new_pos, 2)
        res = self.api.queues.get_by_id(queue=queue)
        changed_tasks = tasks[1:3] + [tasks[0], tasks[3]]
        self.assertQueueTasks(res.queue, changed_tasks)

        # move backwards beyond the end
        new_pos = self.api.queues.move_task_backward(
            queue=queue, task=tasks[0], count=100
        ).position
        self.assertEqual(new_pos, 3)
        res = self.api.queues.get_by_id(queue=queue)
        changed_tasks = tasks[1:] + [tasks[0]]
        self.assertQueueTasks(res.queue, changed_tasks)

        # move forwards in the middle
        new_pos = self.api.queues.move_task_forward(
            queue=queue, task=tasks[0], count=2
        ).position
        self.assertEqual(new_pos, 1)
        res = self.api.queues.get_by_id(queue=queue)
        changed_tasks = [tasks[1], tasks[0]] + tasks[2:]
        self.assertQueueTasks(res.queue, changed_tasks)

        # move forwards beyond the beginning
        new_pos = self.api.queues.move_task_forward(
            queue=queue, task=tasks[0], count=100
        ).position
        self.assertEqual(new_pos, 0)
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, tasks)

        # move to back
        new_pos = self.api.queues.move_task_to_back(
            queue=queue, task=tasks[0]
        ).position
        self.assertEqual(new_pos, 3)
        res = self.api.queues.get_by_id(queue=queue)
        changed_tasks = tasks[1:] + [tasks[0]]
        self.assertQueueTasks(res.queue, changed_tasks)

        # move to front
        new_pos = self.api.queues.move_task_to_front(
            queue=queue, task=tasks[0]
        ).position
        self.assertEqual(new_pos, 0)
        res = self.api.queues.get_by_id(queue=queue)
        self.assertQueueTasks(res.queue, tasks)

    def test_get_all_ex(self):
        queue_name = "TestTempQueue1"
        queue_display_name = "Test display name"
        queue_tags = ["Test1", "Test2"]
        queue = self._temp_queue(queue_name, display_name=queue_display_name, tags=queue_tags)

        res = self.api.queues.get_all_ex(name="TestTempQueue*").queues
        self.assertQueue(
            res,
            queue_id=queue,
            display_name=queue_display_name,
            name=queue_name,
            tags=queue_tags,
            tasks=[],
            workers=[],
        )

        tasks = [
            self._create_temp_queued_task(t, queue)
            for t in ("temp task1", "temp task2")
        ]
        workers = [
            self._create_temp_worker(w, queue) for w in ("temp worker1", "temp worker2")
        ]
        res = self.api.queues.get_all_ex(name="TestTempQueue*").queues
        self.assertQueue(
            res,
            queue_id=queue,
            name=queue_name,
            display_name=queue_display_name,
            tags=queue_tags,
            tasks=tasks,
            workers=workers,
        )

    def assertMetricQueues(self, queues_data, queue_id):
        self.assertEqual(len(queues_data), 1)
        queue_res = queues_data[0]

        self.assertEqual(queue_res.queue, queue_id)
        dates_len = len(queue_res["dates"])
        self.assertTrue(2 >= dates_len >= 1)
        for prop in ("avg_waiting_times", "queue_lengths"):
            self.assertEqual(len(queue_res[prop]), dates_len)

        dates_in_sec = [d / 1000 for d in queue_res["dates"]]
        self.assertGreater(
            dates_in_sec[0], (utc_now_tz_aware() - timedelta(seconds=15)).timestamp()
        )
        if dates_len > 1:
            self.assertAlmostEqual(dates_in_sec[1] - dates_in_sec[0], 5, places=0)

    def assertQueue(
        self,
        queues: Sequence[AttrDict],
        queue_id: str,
        name: str,
        display_name: str,
        tags: Sequence[str],
        tasks: Sequence[dict],
        workers: Sequence[dict],
    ):
        queue = next(q for q in queues if q.id == queue_id)
        assert queue.last_update
        self.assertEqualNoOrder(queue.tags, tags)
        self.assertEqual(queue.name, name)
        self.assertEqual(queue.display_name, display_name)
        self.assertQueueTasks(queue, tasks, name, display_name)
        self.assertQueueWorkers(queue, workers, name, display_name)

    def assertTaskTags(self, task, system_tags):
        res = self.api.tasks.get_by_id(task=task)
        self.assertSequenceEqual(res.task.system_tags, system_tags)

    def assertQueueTasks(
        self,
        queue: AttrDict,
        tasks: Sequence,
        queue_name: str = None,
        display_queue_name: str = None,
    ):
        self.assertEqual([e.task for e in queue.entries], tasks)
        if queue_name:
            for task in tasks:
                execution = self.api.tasks.get_by_id_ex(
                    id=[task["id"]],
                    only_fields=[
                        "execution.queue.name",
                        "execution.queue.display_name",
                    ],
                ).tasks[0].execution
                self.assertEqual(execution.queue.name, queue_name)
                self.assertEqual(execution.queue.display_name, display_queue_name)

    def assertGetNextTasks(self, queue, tasks):
        for task_id in tasks:
            res = self.api.queues.get_next_task(queue=queue)
            self.assertEqual(res.entry.task, task_id)
        assert not self.api.queues.get_next_task(queue=queue)

    def assertQueueWorkers(
        self,
        queue: AttrDict,
        workers: Sequence[dict],
        queue_name: str = None,
        display_queue_name: str = None,
    ):
        sort_key = itemgetter("name")
        self.assertEqual(
            sorted(queue.workers, key=sort_key), sorted(workers, key=sort_key)
        )
        if not workers:
            return

        res = self.api.workers.get_all()
        worker_ids = {w["key"] for w in workers}
        found = [w for w in res.workers if w.key in worker_ids]
        self.assertEqual(len(found), len(worker_ids))
        for worker in found:
            for queue in worker.queues:
                self.assertEqual(queue.name, queue_name)
                self.assertEqual(queue.display_name, display_queue_name)

    def _temp_queue(self, queue_name, **kwargs):
        return self.create_temp("queues", name=queue_name, **kwargs)

    def _temp_task(self, task_name, is_testing=False, is_development=False):
        task_input = dict(
            name=task_name,
            type="testing" if is_testing else "training",
            script={"repository": "test", "entry_point": "test"},
            system_tags=["development"] if is_development else None,
        )
        return self.create_temp("tasks", **task_input)

    def _create_temp_queued_task(self, task_name, queue) -> dict:
        task_id = self._temp_task(task_name)
        self.api.tasks.enqueue(task=task_id, queue=queue)
        return dict(id=task_id, name=task_name)

    def _create_temp_running_task(self, task_name) -> dict:
        task_id = self._temp_task(task_name, is_testing=True)
        self.api.tasks.started(task=task_id)
        return dict(id=task_id, name=task_name)

    def _create_temp_worker(self, worker, queue):
        self.api.workers.register(worker=worker, queues=[queue])
        task = self._create_temp_running_task(f"temp task for worker {worker}")
        self.api.workers.status_report(
            worker=worker,
            timestamp=int(utc_now_tz_aware().timestamp() * 1000),
            machine_stats=dict(cpu_usage=[10, 20]),
            task=task["id"],
        )
        return dict(
            name=worker,
            ip="127.0.0.1",
            task=task,
            key=f"worker_{self.user.company.id}_{self.user.id}_{worker}"
        )
