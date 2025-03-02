from typing import Tuple

from apiserver.apierrors import errors
from apiserver.tests.automated import TestService


class TestPipelines(TestService):
    task_hyperparams = {
        "properties":
            {
                "version": {
                    "section": "properties",
                    "name": "version",
                    "type": "str",
                    "value": "3.2"
                }
            }
    }

    def test_controller_operations(self):
        task_name = "pipelines test"
        project, task = self._temp_project_and_task(name=task_name)
        steps = [
            self.api.tasks.create(
                name=f"Pipeline step {i}",
                project=project,
                type="training",
                system_tags=["pipeline"],
                parent=task
            ).id
            for i in range(2)
        ]
        ids = [task, *steps]
        res = self.api.tasks.get_all_ex(id=ids, search_hidden=True)
        self.assertEqual(len(res.tasks), len(ids))

        # stop
        partial_ids = [task, steps[0]]
        self.api.tasks.enqueue_many(ids=partial_ids)
        res = self.api.tasks.get_all_ex(id=partial_ids, search_hidden=True)
        self.assertTrue(t.stats == "in_progress" for t in res.tasks)
        self.api.tasks.stop(task=task, include_pipeline_steps=True)
        res = self.api.tasks.get_all_ex(id=ids, search_hidden=True)
        self.assertTrue(t.stats == "created" for t in res.tasks)

        # archive/unarchive
        self.api.tasks.archive(tasks=[task], include_pipeline_steps=True)
        res = self.api.tasks.get_all_ex(id=ids, search_hidden=True, system_tags=["-archived"])
        self.assertEqual(len(res.tasks), 0)
        self.api.tasks.unarchive_many(ids=[task], include_pipeline_steps=True)
        res = self.api.tasks.get_all_ex(id=ids, search_hidden=True, system_tags=["-archived"])
        self.assertEqual(len(res.tasks), len(ids))

        # delete
        self.api.tasks.delete(task=task, force=True, include_pipeline_steps=True)
        res = self.api.tasks.get_all_ex(id=ids, search_hidden=True)
        self.assertEqual(len(res.tasks), 0)

    def test_delete_runs(self):
        queue = self.api.queues.get_default().id
        task_name = "pipelines test"
        project, task = self._temp_project_and_task(name=task_name)
        args = [{"name": "hello", "value": "test"}]
        pipeline_tasks = [
            self.api.pipelines.start_pipeline(
                task=task, queue=queue, args=args
            ).pipeline
            for _ in range(2)
        ]
        tasks = self.api.tasks.get_all_ex(project=project).tasks
        self.assertEqual({task, *pipeline_tasks}, {t.id for t in tasks})

        # cannot delete all runs
        with self.api.raises(errors.bad_request.CannotRemoveAllRuns):
            self.api.pipelines.delete_runs(project=project, ids=[task, *pipeline_tasks])

        # successful deletion
        res = self.api.pipelines.delete_runs(project=project, ids=pipeline_tasks)
        self.assertEqual({r.id for r in res.succeeded}, set(pipeline_tasks))
        tasks = self.api.tasks.get_all_ex(project=project).tasks
        self.assertEqual([task], [t.id for t in tasks])

    def test_start_pipeline(self):
        queue = self.api.queues.get_default().id
        task_name = "pipelines test"
        project, task = self._temp_project_and_task(name=task_name)
        args = [{"name": "hello", "value": "test"}]

        res = self.api.pipelines.start_pipeline(task=task, queue=queue, args=args)
        pipeline_task = res.pipeline
        self.assertTrue(res.enqueued)
        pipeline = self.api.tasks.get_all_ex(id=[pipeline_task]).tasks[0]
        self.assertTrue(pipeline.name.startswith(task_name))
        self.assertEqual(pipeline.status, "queued")
        self.assertEqual(pipeline.project.id, project)
        self.assertEqual(
            pipeline.hyperparams,
            {
                "Args": {
                    a["name"]: {
                        "section": "Args",
                        "name": a["name"],
                        "value": a["value"],
                    }
                    for a in args
                },
                **self.task_hyperparams,
            },
        )

        # watched queue
        queue = self._temp_queue("test pipelines")
        project, task = self._temp_project_and_task(name="pipelines test1")
        res = self.api.pipelines.start_pipeline(
            task=task, queue=queue, verify_watched_queue=True
        )
        self.assertEqual(res.queue_watched, False)

        self.api.workers.register(worker="test pipelines", queues=[queue])
        project, task = self._temp_project_and_task(name="pipelines test2")
        res = self.api.pipelines.start_pipeline(
            task=task, queue=queue, verify_watched_queue=True
        )
        self.assertEqual(res.queue_watched, True)

    def _temp_project_and_task(self, name) -> Tuple[str, str]:
        project = self.create_temp(
            "projects",
            name=name,
            description="test",
            delete_params=dict(force=True, delete_contents=True),
        )

        return (
            project,
            self.create_temp(
                "tasks",
                name=name,
                type="controller",
                project=project,
                system_tags=["pipeline"],
                hyperparams=self.task_hyperparams,
            ),
        )

    def _temp_queue(self, queue_name, **kwargs):
        return self.create_temp("queues", name=queue_name, **kwargs)
