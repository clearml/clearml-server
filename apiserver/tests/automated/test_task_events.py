import json
import operator
import unittest
from statistics import mean
from typing import Sequence, Optional, Tuple

from boltons.iterutils import first

from apiserver.apierrors import errors
from apiserver.es_factory import es_factory
from apiserver.apierrors.errors.bad_request import EventsNotAdded
from apiserver.tests.automated import TestService


class TestTaskEvents(TestService):
    delete_params = dict(can_fail=True, force=True)
    default_task_name = "test task events"

    def _temp_project(self, name=default_task_name):
        return self.create_temp(
            "projects",
            name=name,
            description="test",
            delete_params=self.delete_params,
        )

    def _temp_task(self, name=default_task_name, **kwargs):
        self.update_missing(kwargs, name=name, type="training")
        return self.create_temp(
            "tasks", delete_paramse=self.delete_params, **kwargs
        )

    def _temp_model(self, name="test model events", **kwargs):
        self.update_missing(kwargs, name=name, uri="file:///a/b", labels={})
        return self.create_temp("models", delete_params=self.delete_params, **kwargs)

    @staticmethod
    def _create_task_event(type_, task, iteration, **kwargs):
        return {
            "worker": "test",
            "type": type_,
            "task": task,
            "iter": iteration,
            "timestamp": kwargs.get("timestamp") or es_factory.get_timestamp_millis(),
            **kwargs,
        }

    def test_task_metrics(self):
        tasks = {
            self._temp_task(): {
                "Metric1": ["training_debug_image"],
                "Metric2": ["training_debug_image", "log"],
            },
            self._temp_task(): {"Metric3": ["training_debug_image"]},
        }
        events = [
            self._create_task_event(
                event_type,
                task=task,
                iteration=1,
                metric=metric,
                variant="Test variant",
            )
            for task, metrics in tasks.items()
            for metric, event_types in metrics.items()
            for event_type in event_types
        ]
        self.send_batch(events)
        self._assert_task_metrics(tasks, "training_debug_image")
        self._assert_task_metrics(tasks, "log")
        self._assert_task_metrics(tasks, "training_stats_scalar")

        self._assert_multitask_metrics(
            tasks=list(tasks), metrics=["Metric1", "Metric2", "Metric3"]
        )
        self._assert_multitask_metrics(
            tasks=list(tasks),
            event_type="training_debug_image",
            metrics=["Metric1", "Metric2", "Metric3"],
        )
        self._assert_multitask_metrics(tasks=list(tasks), event_type="plot", metrics=[])

    def _assert_multitask_metrics(
        self, tasks: Sequence[str], metrics: Sequence[str], event_type: str = None
    ):
        res = self.api.events.get_multi_task_metrics(
            tasks=tasks,
            **({"event_type": event_type} if event_type else {}),
        ).metrics
        self.assertEqual([r.metric for r in res], metrics)
        self.assertTrue(all(r.variants == ["Test variant"] for r in res))

    def _assert_task_metrics(self, tasks: dict, event_type: str):
        res = self.api.events.get_task_metrics(tasks=list(tasks), event_type=event_type)
        for task, metrics in tasks.items():
            res_metrics = next(
                (tm.metrics for tm in res.metrics if tm.task == task), ()
            )
            self.assertEqual(
                set(res_metrics),
                set(
                    metric for metric, events in metrics.items() if event_type in events
                ),
            )

    def test_task_single_value_metrics(self):
        metric = "Metric1"
        variant = "Variant1"
        iter_count = 10
        task = self._temp_task()
        special_iteration = -(2 ** 31)
        events = [
            {
                **self._create_task_event(
                    "training_stats_scalar", task, iteration or special_iteration
                ),
                "metric": metric,
                "variant": variant,
                "value": iteration,
            }
            for iteration in range(iter_count)
        ]
        self.send_batch(events)

        # special iteration is present in the events retrieval
        metric_param = {"metric": metric, "variants": [variant]}
        res = self.api.events.scalar_metrics_iter_raw(
            task=task, batch_size=100, metric=metric_param, count_total=True
        )
        self.assertEqual(res.returned, iter_count)
        self.assertEqual(res.total, iter_count)
        self.assertEqual(
            res.variants[variant]["iter"],
            [x or special_iteration for x in range(iter_count)],
        )
        self.assertEqual(res.variants[variant]["y"], list(range(iter_count)))

        # but not in the histogram
        data = self.api.events.scalar_metrics_iter_histogram(task=task)
        self.assertEqual(data[metric][variant]["x"], list(range(1, iter_count)))

        # new api
        res = self.api.events.get_task_single_value_metrics(tasks=[task]).tasks
        self.assertEqual(len(res), 1)
        data = res[0]
        self.assertEqual(data.task, task)
        self.assertEqual(data.task_name, self.default_task_name)
        self.assertEqual(len(data["values"]), 1)
        value = data["values"][0]
        self.assertEqual(value.metric, metric)
        self.assertEqual(value.variant, variant)
        self.assertEqual(value.value, 0)
        # test metrics parameter
        res = self.api.events.get_task_single_value_metrics(
            tasks=[task], metrics=[{"metric": metric, "variants": [variant]}]
        ).tasks
        self.assertEqual(len(res), 1)
        res = self.api.events.get_task_single_value_metrics(
            tasks=[task], metrics=[{"metric": "non_existing", "variants": [variant]}]
        ).tasks
        self.assertEqual(len(res), 0)

        # update is working
        task_data = self.api.tasks.get_by_id(task=task).task
        last_metrics = first(first(task_data.last_metrics.values()).values())
        self.assertEqual(last_metrics.value, iter_count - 1)
        new_value = 1000
        new_event = {
            **self._create_task_event("training_stats_scalar", task, special_iteration),
            "metric": metric,
            "variant": variant,
            "value": new_value,
        }
        self.send(new_event)

        res = self.api.events.scalar_metrics_iter_raw(
            task=task, batch_size=100, metric=metric_param, count_total=True
        )
        self.assertEqual(
            res.variants[variant]["y"], [y or new_value for y in range(iter_count)],
        )

        task_data = self.api.tasks.get_by_id(task=task).task
        last_metrics = first(first(task_data.last_metrics.values()).values())
        self.assertEqual(last_metrics.value, new_value)

        data = self.api.events.get_task_single_value_metrics(tasks=[task]).tasks[0]
        self.assertEqual(data.task, task)
        self.assertEqual(data.task_name, self.default_task_name)
        self.assertEqual(len(data["values"]), 1)
        value = data["values"][0]
        self.assertEqual(value.value, new_value)

    def test_last_scalar_metrics(self):
        metric = "Metric1"
        for variant in ("Variant1", None):
            iter_count = 100
            task = self._temp_task()
            events = [
                {
                    **self._create_task_event("training_stats_scalar", task, iteration),
                    "metric": metric,
                    "variant": variant,
                    "value": iteration,
                }
                for iteration in range(iter_count)
            ]
            # send 2 batches to check the interaction with already stored db value
            # each batch contains multiple iterations
            self.send_batch(events[:50])
            self.send_batch(events[50:])

            task_data = self.api.tasks.get_by_id(task=task).task
            metric_data = first(first(task_data.last_metrics.values()).values())
            self.assertEqual(iter_count - 1, metric_data.value)
            self.assertEqual(iter_count - 1, metric_data.max_value)
            self.assertEqual(iter_count - 1, metric_data.max_value_iteration)
            self.assertEqual(0, metric_data.min_value)
            self.assertEqual(0, metric_data.min_value_iteration)
            self.assertEqual(0, metric_data.first_value_iteration)
            self.assertEqual(0, metric_data.first_value)
            self.assertEqual(iter_count, metric_data.count)
            self.assertEqual(sum(i for i in range(iter_count)) / iter_count, metric_data.mean_value)
            res = self.api.events.get_task_latest_scalar_values(task=task)
            self.assertEqual(iter_count - 1, res.last_iter)

    def test_model_events(self):
        model = self._temp_model(ready=False)

        # task log events are not allowed
        log_event = self._create_task_event(
            "log",
            task=model,
            iteration=0,
            msg=f"This is a log message",
            model_event=True,
        )
        with self.api.raises(errors.bad_request.EventsNotAdded):
            self.send(log_event)

        # mixed batch
        events = [
            {
                **self._create_task_event("training_stats_scalar", model, iteration),
                "metric": f"Metric{metric_idx}",
                "variant": f"Variant{variant_idx}",
                "value": iteration,
                "model_event": True,
                "x_axis_label": f"Label_{metric_idx}_{variant_idx}"
            }
            for iteration in range(2)
            for metric_idx in range(5)
            for variant_idx in range(5)
        ]
        task = self._temp_task()
        # noinspection PyTypeChecker
        events.append(
            self._create_task_event(
                "log",
                task=task,
                iteration=0,
                msg=f"This is a log message",
                metric="Metric0",
                variant="Variant0",
                allow_locked=True,
            )
        )
        self.send_batch(events)
        data = self.api.events.scalar_metrics_iter_histogram(
            task=model, model_events=True
        )
        self.assertEqual(list(data), [f"Metric{idx}" for idx in range(5)])
        metric_data = data.Metric0
        self.assertEqual(list(metric_data), [f"Variant{idx}" for idx in range(5)])
        variant_data = metric_data.Variant0
        self.assertEqual(variant_data.x, [0, 1])
        self.assertEqual(variant_data.y, [0.0, 1.0])
        self.assertEqual(variant_data.x_axis_label, "Label_0_0")

        model_data = self.api.models.get_all_ex(
            id=[model], only_fields=["last_metrics", "last_iteration"]
        ).models[0]
        metric_data = first(first(model_data.last_metrics.values()).values())
        self.assertEqual(1, model_data.last_iteration)
        self.assertEqual(1, metric_data.value)
        self.assertEqual(1, metric_data.max_value)
        self.assertEqual(1, metric_data.max_value_iteration)
        self.assertEqual(0, metric_data.min_value)
        self.assertEqual(0, metric_data.min_value_iteration)
        self.assertEqual("Label_4_4", metric_data.x_axis_label)

        self._assert_log_events(task=task, expected_total=1)

        metrics = self.api.events.get_multi_task_metrics(
            tasks=[model],
            event_type="training_stats_scalar",
            model_events=True,
        ).metrics
        self.assertEqual([m.metric for m in metrics], [f"Metric{i}" for i in range(5)])
        variants = [f"Variant{i}" for i in range(5)]
        self.assertTrue(all(m.variants == variants for m in metrics))

    def test_error_events(self):
        task = self._temp_task()
        events = [
            self._create_task_event("unknown type", task, iteration=1),
            self._create_task_event("training_debug_image", task=None, iteration=1),
            self._create_task_event(
                "training_debug_image", task="Invalid task", iteration=1
            ),
        ]
        # failure if no events added
        with self.api.raises(EventsNotAdded):
            self.send_batch(events)

        events.append(
            self._create_task_event("training_debug_image", task=task, iteration=1)
        )
        # success if at least one event added
        res = self.send_batch(events)
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["errors"], 3)
        self.assertEqual(len(res["errors_info"]), 3)
        res = self.api.events.get_task_events(task=task)
        self.assertEqual(len(res.events), 1)

    def test_task_logs(self):
        task = self._temp_task()
        timestamp = es_factory.get_timestamp_millis()
        events = [
            self._create_task_event(
                "log",
                task=task,
                iteration=iter_,
                timestamp=timestamp + iter_ * 1000,
                msg=f"This is a log message from test task iter {iter_}",
            )
            for iter_ in range(10)
        ]
        self.send_batch(events)

        # test forward navigation
        ftime, ltime = None, None
        for page in range(2):
            ftime, ltime = self._assert_log_events(
                task=task, timestamp=ltime, expected_page=page
            )

        # test backwards navigation
        self._assert_log_events(task=task, timestamp=ftime, navigate_earlier=False)

        # test order
        self._assert_log_events(task=task, order="asc")

        metric = "metric"
        variant = "variant"
        events = [
            self._create_task_event(
                "log",
                task=task,
                iteration=iter_,
                timestamp=timestamp + iter_ * 1000,
                msg=f"This is a log message from test task iter {iter_}",
                metric=metric,
                variant=variant,
            )
            for iter_ in range(2)
        ]
        self.send_batch(events)
        res = self.api.events.get_task_log(task=task)
        self.assertEqual(res.total, 12)
        res = self.api.events.get_task_log(task=task, metrics=[{"metric": metric}])
        self.assertEqual(res.total, 2)

        # test clear
        self.api.events.clear_task_log(task=task, exclude_metrics=[metric])
        res = self.api.events.get_task_log(task=task)
        self.assertEqual(res.total, 2)
        self.api.events.clear_task_log(task=task)
        res = self.api.events.get_task_log(task=task)
        self.assertEqual(res.total, 0)

    def _assert_log_events(
        self,
        task,
        batch_size: int = 5,
        timestamp: Optional[int] = None,
        expected_total: int = 10,
        expected_page: int = 0,
        **extra_params,
    ) -> Tuple[int, int]:
        res = self.api.events.get_task_log(
            task=task, batch_size=batch_size, from_timestamp=timestamp, **extra_params,
        )
        self.assertEqual(res.total, expected_total)
        expected_events = max(
            0, batch_size - max(0, (expected_page + 1) * batch_size - expected_total)
        )
        self.assertEqual(res.returned, expected_events)
        self.assertEqual(len(res.events), expected_events)
        unique_events = len({ev.iter for ev in res.events})
        self.assertEqual(len(res.events), unique_events)
        if res.events:
            cmp_operator = operator.ge
            if (
                not extra_params.get("navigate_earlier", True)
                or extra_params.get("order", None) == "asc"
            ):
                cmp_operator = operator.le
            self.assertTrue(
                all(
                    cmp_operator(first.timestamp, second.timestamp)
                    for first, second in zip(res.events, res.events[1:])
                )
            )

        return (
            (res.events[0].timestamp, res.events[-1].timestamp)
            if res.events
            else (None, None)
        )

    def test_task_unique_metric_variants(self):
        project = self._temp_project()
        task1 = self._temp_task(project=project)
        task2 = self._temp_task(project=project)
        metric1 = "Metric1"
        metric2 = "Metric2"
        events = [
            {
                **self._create_task_event("training_stats_scalar", task, 0),
                "metric": metric,
                "variant": "Variant",
                "value": 10,
            }
            for task, metric in ((task1, metric1), (task2, metric2))
        ]
        self.send_batch(events)

        metrics = self.api.projects.get_unique_metric_variants(project=project).metrics
        self.assertEqual({m.metric for m in metrics}, {metric1, metric2})
        metrics = self.api.projects.get_unique_metric_variants(ids=[task1, task2]).metrics
        self.assertEqual({m.metric for m in metrics}, {metric1, metric2})
        metrics = self.api.projects.get_unique_metric_variants(ids=[task1]).metrics
        self.assertEqual([m.metric for m in metrics], [metric1])

    def test_task_metric_value_intervals_keys(self):
        metric = "Metric1"
        variant = "Variant1"
        iter_count = 100
        task = self._temp_task()
        events = [
            {
                **self._create_task_event("training_stats_scalar", task, iteration),
                "metric": metric,
                "variant": variant,
                "value": iteration,
            }
            for iteration in range(iter_count)
        ]
        self.send_batch(events)
        for key in None, "iter", "timestamp", "iso_time":
            with self.subTest(key=key):
                data = self.api.events.scalar_metrics_iter_histogram(
                    task=task, **(dict(key=key) if key is not None else {})
                )
                self.assertIn(metric, data)
                self.assertIn(variant, data[metric])
                self.assertIn("x", data[metric][variant])
                self.assertIn("y", data[metric][variant])

    def test_multitask_events_many_metrics(self):
        tasks = [
            self._temp_task(name="test events1"),
            self._temp_task(name="test events2"),
        ]
        iter_count = 10
        metrics_count = 10
        variants_count = 10
        events = [
            {
                **self._create_task_event("training_stats_scalar", task, iteration),
                "metric": f"Metric{metric_idx}",
                "variant": f"Variant{variant_idx}",
                "value": iteration,
            }
            for iteration in range(iter_count)
            for task in tasks
            for metric_idx in range(metrics_count)
            for variant_idx in range(variants_count)
        ]
        self.send_batch(events)
        data = self.api.events.multi_task_scalar_metrics_iter_histogram(tasks=tasks)
        self._assert_metrics_and_variants(
            data.metrics,
            metrics=metrics_count,
            variants=variants_count,
            tasks=tasks,
            iterations=iter_count,
        )

        # test metrics
        data = self.api.events.multi_task_scalar_metrics_iter_histogram(
            tasks=tasks,
            metrics=[
                {
                    "metric": f"Metric{m_idx}",
                    "variants": [f"Variant{v_idx}" for v_idx in range(4)],
                }
                for m_idx in range(2)
            ],
        )
        self._assert_metrics_and_variants(
            data.metrics,
            metrics=2,
            variants=4,
            tasks=tasks,
            iterations=iter_count,
        )

    def _assert_metrics_and_variants(
        self, data: dict, metrics: int, variants: int, tasks: Sequence, iterations: int
    ):
        self.assertEqual(len(data), metrics)
        for m in range(metrics):
            metric_data = data[f"Metric{m}"]
            self.assertEqual(len(metric_data), variants)
            for v in range(variants):
                variant_data = metric_data[f"Variant{v}"]
                self.assertEqual(len(variant_data), len(tasks))
                for t in tasks:
                    task_data = variant_data[t]
                    self.assertEqual(len(task_data["x"]), iterations)
                    self.assertEqual(len(task_data["y"]), iterations)

    def test_task_metric_raw(self):
        metric = "Metric1"
        variant = "Variant1"
        iter_count = 100
        task = self._temp_task()
        events = [
            {
                **self._create_task_event("training_stats_scalar", task, iteration),
                "metric": metric,
                "variant": variant,
                "value": iteration,
            }
            for iteration in range(iter_count)
        ]
        self.send_batch(events)

        batch_size = 15
        metric_param = {"metric": metric, "variants": [variant]}
        res = self.api.events.scalar_metrics_iter_raw(
            task=task, batch_size=batch_size, metric=metric_param, count_total=True
        )
        self.assertEqual(res.total, len(events))
        self.assertTrue(res.scroll_id)
        res_iters = []
        res_ys = []
        calls = 0
        while res.returned or calls > 10:
            calls += 1
            res_iters.extend(res.variants[variant]["iter"])
            res_ys.extend(res.variants[variant]["y"])
            scroll_id = res.scroll_id
            res = self.api.events.scalar_metrics_iter_raw(
                task=task, metric=metric_param, scroll_id=scroll_id
            )

        self.assertEqual(calls, len(events) // batch_size + 1)
        self.assertEqual(res_iters, [ev["iter"] for ev in events])
        self.assertEqual(res_ys, [ev["value"] for ev in events])

    def test_task_metric_value_intervals(self):
        metric = "Metric1"
        variant = "Variant1"
        iter_count = 100
        task = self._temp_task()
        events = [
            {
                **self._create_task_event("training_stats_scalar", task, iteration),
                "metric": metric,
                "variant": variant,
                "value": iteration,
            }
            for iteration in range(iter_count)
        ]
        self.send_batch(events)

        data = self.api.events.scalar_metrics_iter_histogram(task=task)
        self._assert_metrics_histogram(data[metric][variant], iter_count, 100)

        data = self.api.events.scalar_metrics_iter_histogram(task=task, samples=100)
        self._assert_metrics_histogram(data[metric][variant], iter_count, 100)

        data = self.api.events.scalar_metrics_iter_histogram(task=task, samples=10)
        self._assert_metrics_histogram(data[metric][variant], iter_count, 10)

    def _assert_metrics_histogram(self, data, iters, samples):
        interval = iters // samples
        self.assertEqual(len(data["x"]), samples)
        self.assertEqual(len(data["y"]), samples)
        for curr in range(samples):
            self.assertEqual(data["x"][curr], curr * interval)
            self.assertEqual(
                data["y"][curr],
                mean(v for v in range(curr * interval, (curr + 1) * interval)),
            )

    def test_multitask_plots(self):
        task1 = self._temp_task()
        events = [
            self._create_task_event("plot", task1, 1, metric="A", variant="AX", plot_str="Task1_1_A_AX"),
            self._create_task_event("plot", task1, 2, metric="B", variant="BX", plot_str="Task1_2_B_BX"),
            self._create_task_event("plot", task1, 3, metric="B", variant="BX", plot_str="Task1_3_B_BX"),
            self._create_task_event("plot", task1, 3, metric="C", variant="CX", plot_str="Task1_3_C_CX"),
        ]
        self.send_batch(events)
        task2 = self._temp_task()
        events = [
            self._create_task_event("plot", task2, 1, metric="C", variant="CX", plot_str="Task2_1_C_CX"),
            self._create_task_event("plot", task2, 2, metric="A", variant="AY", plot_str="Task2_2_A_AY"),
        ]
        self.send_batch(events)
        plots = self.api.events.get_multi_task_plots(tasks=[task1, task2]).plots
        self.assertEqual(len(plots), 3)
        self.assertEqual(len(plots.A), 2)
        self.assertEqual(len(plots.A.AX), 1)
        self.assertEqual(len(plots.A.AY), 1)
        self.assertEqual(plots.A.AX[task1]["1"]["plots"][0]["plot_str"], "Task1_1_A_AX")
        self.assertEqual(plots.A.AY[task2]["2"]["plots"][0]["plot_str"], "Task2_2_A_AY")
        self.assertEqual(len(plots.B), 1)
        self.assertEqual(len(plots.B.BX), 1)
        self.assertEqual(plots.B.BX[task1]["3"]["plots"][0]["plot_str"], "Task1_3_B_BX")
        self.assertEqual(len(plots.C), 1)
        self.assertEqual(len(plots.C.CX), 2)
        self.assertEqual(plots.C.CX[task1]["3"]["plots"][0]["plot_str"], "Task1_3_C_CX")
        self.assertEqual(plots.C.CX[task2]["1"]["plots"][0]["plot_str"], "Task2_1_C_CX")

        # test metrics
        plots = self.api.events.get_multi_task_plots(
            tasks=[task1, task2], metrics=[{"metric": "A"}]
        ).plots
        self.assertEqual(len(plots), 1)
        self.assertEqual(len(plots.A), 2)

    def test_task_plots(self):
        task = self._temp_task()
        event = self._create_task_event("plot", task, 0)
        event["metric"] = "roc"
        event.update(
            {
                "plot_str": json.dumps(
                    {
                        "data": [
                            {
                                "x": [0, 1, 2, 3, 4, 5, 6, 7, 8],
                                "y": [0, 1, 2, 3, 4, 5, 6, 7, 8],
                                "text": [
                                    "Th=0.1",
                                    "Th=0.2",
                                    "Th=0.3",
                                    "Th=0.4",
                                    "Th=0.5",
                                    "Th=0.6",
                                    "Th=0.7",
                                    "Th=0.8",
                                ],
                                "name": "class1",
                            },
                            {
                                "x": [0, 1, 2, 3, 4, 5, 6, 7, 8],
                                "y": [2.0, 3.0, 5.0, 8.2, 6.4, 7.5, 9.2, 8.1, 10.0],
                                "text": [
                                    "Th=0.1",
                                    "Th=0.2",
                                    "Th=0.3",
                                    "Th=0.4",
                                    "Th=0.5",
                                    "Th=0.6",
                                    "Th=0.7",
                                    "Th=0.8",
                                ],
                                "name": "class2",
                            },
                        ],
                        "layout": {
                            "title": "ROC for iter 0",
                            "xaxis": {"title": "my x axis"},
                            "yaxis": {"title": "my y axis"},
                        },
                    }
                )
            }
        )
        self.send(event)

        event1 = self._create_task_event("plot", task, 100)
        event1["metric"] = "confusion"
        event1.update(
            {
                "plot_str": json.dumps(
                    {
                        "data": [
                            {
                                "y": [
                                    "lying",
                                    "sitting",
                                    "standing",
                                    "people",
                                    "backgroun",
                                ],
                                "x": [
                                    "lying",
                                    "sitting",
                                    "standing",
                                    "people",
                                    "backgroun",
                                ],
                                "z": [
                                    [758, 163, 0, 0, 23],
                                    [63, 858, 3, 0, 0],
                                    [0, 50, 188, 21, 35],
                                    [0, 22, 8, 40, 4],
                                    [12, 91, 26, 29, 368],
                                ],
                                "type": "heatmap",
                            }
                        ],
                        "layout": {
                            "title": "Confusion Matrix for iter 100",
                            "xaxis": {"title": "Predicted value"},
                            "yaxis": {"title": "Real value"},
                        },
                    }
                )
            }
        )
        self.send(event1)

        plots = self.api.events.get_task_plots(task=task).plots
        self.assertEqual(
            {e["plot_str"] for e in (event, event1)}, {p.plot_str for p in plots}
        )

        self.api.tasks.reset(task=task)
        plots = self.api.events.get_task_plots(task=task).plots
        self.assertEqual(len(plots), 0)

    @unittest.skip("this test will run only if 'validate_plot_str' is set to true")
    def test_plots_validation(self):
        valid_plot_str = json.dumps({"data": []})
        invalid_plot_str = "Not a valid json"
        task = self._temp_task()

        event = self._create_task_event(
            "plot", task, 0, metric="test1", plot_str=valid_plot_str
        )
        event1 = self._create_task_event(
            "plot", task, 100, metric="test2", plot_str=invalid_plot_str
        )
        self.send_batch([event, event1])
        res = self.api.events.get_task_plots(task=task).plots
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].metric, "test1")

        event = self._create_task_event(
            "plot",
            task,
            0,
            metric="test1",
            plot_str=valid_plot_str,
            skip_validation=True,
        )
        event1 = self._create_task_event(
            "plot",
            task,
            100,
            metric="test2",
            plot_str=invalid_plot_str,
            skip_validation=True,
        )
        self.send_batch([event, event1])
        res = self.api.events.get_task_plots(task=task).plots
        self.assertEqual(len(res), 2)
        self.assertEqual(set(r.metric for r in res), {"test1", "test2"})

    def send_batch(self, events):
        _, data = self.api.send_batch("events.add_batch", events)
        return data

    def send(self, event):
        _, data = self.api.send("events.add", event)
        return data


if __name__ == "__main__":
    unittest.main()
