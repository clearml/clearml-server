from datetime import datetime, timedelta

from apiserver.tests.automated import TestService


class TestTasksFiltering(TestService):
    def setUp(self, **kwargs):
        super().setUp(version="2.13")

    def test_hyperparam_values(self):
        project = self.temp_project()
        param1 = ("Se$tion1", "pa__ram1", True)
        param2 = ("Section2", "param2", False)
        task_count = 5
        for (section, name, unique_value) in (param1, param2):
            for idx in range(task_count):
                t = self.temp_task(project=project)
                self.api.tasks.edit_hyper_params(
                    task=t,
                    hyperparams=[
                        {
                            "section": section,
                            "name": name,
                            "type": "str",
                            "value": str(idx) if unique_value else "Constant",
                        }
                    ],
                )
        res = self.api.projects.get_hyperparam_values(
            projects=[project], section=param1[0], name=param1[1]
        )
        self.assertEqual(res.total, task_count)
        self.assertEqual(res["values"], [str(i) for i in range(task_count)])
        res = self.api.projects.get_hyperparam_values(
            projects=[project], section=param2[0], name=param2[1]
        )
        self.assertEqual(res.total, 1)
        self.assertEqual(res["values"], ["Constant"])
        res = self.api.projects.get_hyperparam_values(
            projects=[project], section="missing", name="missing"
        )
        self.assertEqual(res.total, 0)
        self.assertEqual(res["values"], [])

        # search pattern
        res = self.api.projects.get_hyperparam_values(
            projects=[project], section=param1[0], name=param1[1], pattern="^1"
        )
        self.assertEqual(res.total, 1)
        self.assertEqual(res["values"], ["1"])

        res = self.api.projects.get_hyperparam_values(
            projects=[project], section=param1[0], name=param1[1], pattern="11"
        )
        self.assertEqual(res.total, 0)

    def test_datetime_queries(self):
        tasks = [self.temp_task() for _ in range(5)]
        now = datetime.utcnow()
        for task in tasks:
            self.api.tasks.ping(task=task)

        # date time syntax
        res = self.api.tasks.get_all_ex(last_update=f">={now.isoformat()}").tasks
        self.assertTrue(set(tasks).issubset({t.id for t in res}))
        res = self.api.tasks.get_all_ex(
            last_update=[
                f">={(now - timedelta(seconds=60)).isoformat()}",
                f"<={now.isoformat()}",
            ]
        ).tasks
        self.assertFalse(set(tasks).issubset({t.id for t in res}))

        # _any_/_all_ queries
        res = self.api.tasks.get_all_ex(
            **{"_any_": {"datetime": f">={now.isoformat()}", "fields": ["last_update", "status_changed"]}}
        ).tasks
        self.assertTrue(set(tasks).issubset({t.id for t in res}))
        res = self.api.tasks.get_all_ex(
            **{"_all_": {"datetime": f">={now.isoformat()}", "fields": ["last_update", "status_changed"]}}
        ).tasks
        self.assertFalse(set(tasks).issubset({t.id for t in res}))

        # simplified range syntax
        res = self.api.tasks.get_all_ex(last_update=[now.isoformat(), None]).tasks
        self.assertTrue(set(tasks).issubset({t.id for t in res}))

        res = self.api.tasks.get_all_ex(
            last_update=[(now - timedelta(seconds=60)).isoformat(), now.isoformat()]
        ).tasks
        self.assertFalse(set(tasks).issubset({t.id for t in res}))

        res = self.api.tasks.get_all_ex(
            **{"_any_": {"datetime": [now.isoformat(), None], "fields": ["last_update", "status_changed"]}}
        ).tasks
        self.assertTrue(set(tasks).issubset({t.id for t in res}))
        res = self.api.tasks.get_all_ex(
            **{"_all_": {"datetime": [now.isoformat(), None], "fields": ["last_update", "status_changed"]}}
        ).tasks
        self.assertFalse(set(tasks).issubset({t.id for t in res}))

    def test_range_queries(self):
        tasks = [self.temp_task() for _ in range(5)]
        now = datetime.utcnow()
        for task in tasks:
            self.api.tasks.started(task=task)

        res = self.api.tasks.get_all_ex(started=[now.isoformat(), None]).tasks
        self.assertTrue(set(tasks).issubset({t.id for t in res}))

        res = self.api.tasks.get_all_ex(
            started=[(now - timedelta(seconds=60)).isoformat(), now.isoformat()]
        ).tasks
        self.assertFalse(set(tasks).issubset({t.id for t in res}))

    def temp_project(self, **kwargs) -> str:
        self.update_missing(
            kwargs,
            name="Test tasks filtering",
            description="test",
            delete_params=dict(force=True),
        )
        return self.create_temp("projects", **kwargs)

    def temp_task(self, **kwargs) -> str:
        self.update_missing(
            kwargs,
            type="testing",
            name="test tasks filtering",
            delete_params=dict(force=True),
        )
        return self.create_temp("tasks", **kwargs)
