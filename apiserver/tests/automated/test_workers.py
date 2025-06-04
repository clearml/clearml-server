import statistics
import time
from uuid import uuid4
from typing import Sequence

from apiserver.apierrors.errors import bad_request
from apiserver.tests.automated import TestService
from apiserver.config_repo import config

log = config.logger(__file__)


class TestWorkersService(TestService):
    def _check_exists(self, worker: str, exists: bool = True, tags: list = None):
        workers = self.api.workers.get_all(last_seen=100, tags=tags).workers
        found = any(w for w in workers if w.id == worker)
        assert exists == found

    def test_workers_register(self):
        test_worker = f"test_{uuid4().hex}"
        self._check_exists(test_worker, False)

        self.api.workers.register(worker=test_worker)
        self._check_exists(test_worker)

        self.api.workers.unregister(worker=test_worker)
        self._check_exists(test_worker, False)

    def test_get_count(self):
        test_workers = [f"test_{uuid4().hex}" for _ in range(2)]
        system_tag = f"tag_{uuid4().hex}"
        for w in test_workers:
            self.api.workers.register(worker=w, system_tags=[system_tag])
        # total workers count include the new ones
        count = self.api.workers.get_count().count
        self.assertGreaterEqual(count, len(test_workers))
        # filter by system tag and last seen
        count = self.api.workers.get_count(system_tags=[system_tag], last_seen=4).count
        self.assertEqual(count, len(test_workers))
        time.sleep(5)
        # workers not seen recently
        count = self.api.workers.get_count(system_tags=[system_tag], last_seen=4).count
        self.assertEqual(count, 0)
        # but still visible without the last seen filter
        count = self.api.workers.get_count(system_tags=[system_tag]).count
        self.assertEqual(count, len(test_workers))

    def test_workers_timeout(self):
        test_worker = f"test_{uuid4().hex}"
        self._check_exists(test_worker, False)

        self.api.workers.register(worker=test_worker, timeout=3)
        self._check_exists(test_worker)

        time.sleep(5)
        self._check_exists(test_worker, False)

    def test_system_tags(self):
        test_worker = f"test_{uuid4().hex}"
        tag = uuid4().hex
        system_tag = uuid4().hex
        self.api.workers.register(
            worker=test_worker, tags=[tag], system_tags=[system_tag], timeout=5
        )

        # system_tags support
        worker = self.api.workers.get_all(tags=[tag], system_tags=[system_tag]).workers[
            0
        ]
        self.assertEqual(worker.id, test_worker)
        self.assertEqual(worker.tags, [tag])
        self.assertEqual(worker.system_tags, [system_tag])

        workers = self.api.workers.get_all(
            tags=[tag], system_tags=[f"-{system_tag}"]
        ).workers
        self.assertFalse(workers)

    def test_filters(self):
        test_worker = f"test_{uuid4().hex}"
        self.api.workers.register(worker=test_worker, tags=["application"], timeout=3)
        self._check_exists(test_worker)
        self._check_exists(test_worker, tags=["application", "test"])
        self._check_exists(test_worker, False, tags=["test"])
        self._check_exists(test_worker, False, tags=["-application"])

    def _simulate_workers(self, start: int, with_gpu: bool = False) -> dict:
        """
        Two workers writing the same metrics. One for 4 seconds. Another one for 2
        The first worker reports a task
        :return: worker ids
        """

        task_id = self._create_running_task(task_name="task-1")

        workers = [f"test_{uuid4().hex}", f"test_{uuid4().hex}"]
        if with_gpu:
            gpu_usage = [dict(gpu_usage=[60, 70]), dict(gpu_usage=[40])]
        else:
            gpu_usage = [{}, {}]

        worker_stats = [
            (
                dict(cpu_usage=[10, 20], memory_used=50, **gpu_usage[0]),
                dict(cpu_usage=[5], memory_used=30, **gpu_usage[1]),
            )
        ] * 4
        worker_activity = [
            (workers[0], workers[1]),
            (workers[0], workers[1]),
            (workers[0],),
            (workers[0],),
        ]
        timestamp = start * 1000
        for ws, stats in zip(worker_activity, worker_stats):
            for w, s in zip(ws, stats):
                data = dict(
                    worker=w,
                    timestamp=timestamp,
                    machine_stats=s,
                )
                if w == workers[0]:
                    data["task"] = task_id
                self.api.workers.status_report(**data)
                timestamp += 60*1000

        return {
            w: s
            for w, s in zip(workers, worker_stats[0])
        }

    def _create_running_task(self, task_name):
        task_input = dict(name=task_name, type="testing")

        task_id = self.create_temp("tasks", **task_input)

        self.api.tasks.started(task=task_id)
        return task_id

    def test_get_keys(self):
        workers = self._simulate_workers(int(time.time()))
        time.sleep(5)  # give to es time to refresh
        res = self.api.workers.get_metric_keys(worker_ids=list(workers))
        assert {"cpu", "memory"} == set(c.name for c in res["categories"])
        assert all(
            c.metric_keys == ["cpu_usage"] for c in res["categories"] if c.name == "cpu"
        )
        assert all(
            c.metric_keys == ["memory_used"]
            for c in res["categories"]
            if c.name == "memory"
        )

        with self.api.raises(bad_request.WorkerStatsNotFound):
            self.api.workers.get_metric_keys(worker_ids=["Non existing worker id"])

    def test_get_stats(self):
        start = int(time.time())
        workers = self._simulate_workers(start, with_gpu=True)

        time.sleep(5)  # give to ES time to refresh
        from_date = start
        to_date = start + 40*10
        # no variants
        res = self.api.workers.get_stats(
            items=[
                dict(key="cpu_usage", aggregation="avg"),
                dict(key="cpu_usage", aggregation="max"),
                dict(key="gpu_usage", aggregation="avg"),
                dict(key="gpu_usage", aggregation="max"),
                dict(key="memory_used", aggregation="max"),
            ],
            from_date=from_date,
            to_date=to_date,
            # split_by_variant=True,
            interval=1,
            worker_ids=list(workers),
        )
        self.assertWorkersInStats(list(workers), res.workers)
        for worker in res.workers:
            self.assertEqual(
                set(metric.metric for metric in worker.metrics),
                {"cpu_usage", "gpu_usage", "memory_used"},
            )

        for worker in res.workers:
            worker_id = worker.worker
            for metric, metric_stats in zip(
                worker.metrics, ({"avg", "max"}, {"avg", "max"}, {"max"})
            ):
                metric_name = metric.metric
                self.assertEqual(
                    set(stat.aggregation for stat in metric.stats), metric_stats
                )
                for stat in metric.stats:
                    expected = workers[worker_id][metric_name]
                    self.assertTrue(11 >= len(stat.dates) >= 10)
                    self.assertFalse(stat.get("resource_series"))
                    agg = stat.aggregation
                    if isinstance(expected, list):
                        if agg == "avg":
                            val = statistics.mean(expected)
                        elif agg == "min":
                            val = min(expected)
                        else:
                            val = max(expected)
                    else:
                        val = expected
                    self.assertEqual(set(stat["values"]), {val, 0})

        # split by resources
        res = self.api.workers.get_stats(
            items=[dict(key="gpu_usage", aggregation="avg")],
            from_date=from_date,
            to_date=to_date,
            split_by_resource=True,
            interval=1,
            worker_ids=list(workers),
        )
        self.assertWorkersInStats(list(workers), res.workers)

        for worker in res.workers:
            worker_id = worker.worker
            for metric in worker.metrics:
                metric_name = metric.metric
                for stat in metric.stats:
                    expected = workers[worker_id][metric_name]
                    if metric_name.startswith("gpu") and len(expected) > 1:
                        resource_series = stat.get("resource_series")
                        self.assertEqual(len(resource_series), len(expected))
                        for rs, value in zip(resource_series, expected):
                            self.assertEqual(set(rs["values"]), {value, 0})
                    else:
                        self.assertEqual(stat.get("resource_series"), [])

        res = self.api.workers.get_stats(
            items=[dict(key="cpu_usage", aggregation="avg")],
            from_date=from_date,
            to_date=to_date,
            interval=1,
            worker_ids=["Non existing worker id"],
        )
        assert not res.workers

    def assertWorkersInStats(self, workers: Sequence[str], stats: Sequence):
        self.assertEqual(set(workers), set(item.worker for item in stats))

    def test_get_activity_report(self):
        # test no workers data
        # run on an empty es db since we have no way
        # to pass non-existing workers to this api
        # res = self.api.workers.get_activity_report(
        #     from_timestamp=from_timestamp.timestamp(),
        #     to_timestamp=to_timestamp.timestamp(),
        #     interval=20,
        # )
        start = int(time.time())
        self._simulate_workers(start)

        time.sleep(5)  # give to es time to refresh
        # no variants
        res = self.api.workers.get_activity_report(
            from_date=start, to_date=start + 10*40, interval=2
        )
        self.assertWorkerSeries(res["total"], 2, 10)
        self.assertWorkerSeries(res["active"], 1, 10)

    def assertWorkerSeries(self, series_data: dict, count: int, size: int):
        self.assertEqual(len(series_data["dates"]), size)
        self.assertEqual(len(series_data["counts"]), size)
        # self.assertTrue(any(c == count for c in series_data["counts"]))
        # self.assertTrue(all(c <= count for c in series_data["counts"]))
