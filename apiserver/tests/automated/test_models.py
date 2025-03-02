import unittest

from apiserver.apierrors import errors
from apiserver.apierrors.errors.bad_request import InvalidModelId
from apiserver.tests.automated import TestService

MODEL_CANNOT_BE_UPDATED_CODES = (400, 203)
TASK_CANNOT_BE_UPDATED_CODES = (400, 110)
PUBLISHED = "published"
IN_PROGRESS = "in_progress"


class TestModelsService(TestService):
    def setUp(self, version="2.9"):
        super().setUp(version=version)

    def test_delete_model_for_task(self):
        # non published task
        task_id, model_id = self._create_task_and_model()
        task = self.api.tasks.get_by_id(task=task_id).task
        self.assertEqual(task.models.output[0].model, model_id)
        res = self.api.models.delete(model=model_id)
        self.assertTrue(res.deleted)
        with self.api.raises(errors.bad_request.InvalidModelId):
            self.api.models.get_by_id(model=model_id)
        task = self.api.tasks.get_by_id(task=task_id).task
        self.assertEqual(task.models.output, [])

        # published task
        task_id, model_id = self._create_task_and_model()
        self.api.tasks.stopped(task=task_id)
        self.api.tasks.publish(task=task_id, publish_model=False)
        with self.api.raises(errors.bad_request.ModelCreatingTaskExists):
            self.api.models.delete(model=model_id)
        res = self.api.models.delete(model=model_id, force=True)
        self.assertTrue(res.deleted)
        with self.api.raises(errors.bad_request.InvalidModelId):
            self.api.models.get_by_id(model=model_id)
        task = self.api.tasks.get_by_id(task=task_id).task
        self.assertEqual(task.models.output[0].model, f"__DELETED__{model_id}")

    def test_publish_output_model_running_task(self):
        task_id, model_id = self._create_task_and_model()
        self._assert_model_ready(model_id, False)

        with self._assert_update_task_failure():
            self.api.models.set_ready(model=model_id)

        self._assert_model_ready(model_id, False)
        self._assert_task_status(task_id, IN_PROGRESS)

    def test_publish_output_model_running_task_no_task_publish(self):
        task_id, model_id = self._create_task_and_model()
        self._assert_model_ready(model_id, False)

        res = self.api.models.set_ready(model=model_id, publish_task=False)
        assert res.updated == 1  # model updated
        assert res.get("published_task", None) is None

        self._assert_model_ready(model_id, True)
        self._assert_task_status(task_id, IN_PROGRESS)

    def test_publish_output_model_running_task_force_task_publish(self):
        task_id, model_id = self._create_task_and_model()
        self._assert_model_ready(model_id, False)

        self.api.models.set_ready(model=model_id, force_publish_task=True)

        self._assert_model_ready(model_id, True)
        self._assert_task_status(task_id, PUBLISHED)

    def test_publish_output_model_published_task(self):
        task_id, model_id = self._create_task_and_model()
        self._assert_model_ready(model_id, False)

        self.api.tasks.stopped(task=task_id)
        self.api.tasks.publish(task=task_id, publish_model=False)
        self._assert_model_ready(model_id, False)
        self._assert_task_status(task_id, PUBLISHED)

        res = self.api.models.set_ready(model=model_id)
        assert res.updated == 1  # model updated
        assert res.get("published_task", None) is None

        self._assert_model_ready(model_id, True)
        self._assert_task_status(task_id, PUBLISHED)

    def test_publish_output_model_stopped_task(self):
        task_id, model_id = self._create_task_and_model()
        self._assert_model_ready(model_id, False)

        self.api.tasks.stopped(task=task_id)

        res = self.api.models.set_ready(model=model_id)
        assert res.updated == 1  # model updated
        assert res.published_task.id == task_id
        assert res.published_task.data.updated == 1
        self._assert_model_ready(model_id, True)
        self._assert_task_status(task_id, PUBLISHED)

        # cannot publish already published model
        with self._assert_update_model_failure():
            self.api.models.set_ready(model=model_id)
        self._assert_model_ready(model_id, True)

    def test_publish_output_model_no_task(self):
        model_id = self.create_temp(
            service="models", name="test", uri="file:///a", labels={}, ready=False
        )
        self._assert_model_ready(model_id, False)

        res = self.api.models.set_ready(model=model_id)
        assert res.updated == 1  # model updated
        assert res.get("task", None) is None
        self._assert_model_ready(model_id, True)

    def test_publish_task_with_output_model(self):
        task_id, model_id = self._create_task_and_model()
        self._assert_model_ready(model_id, False)

        self.api.tasks.stopped(task=task_id)
        res = self.api.tasks.publish(task=task_id)
        assert res.updated == 1  # model updated
        self._assert_model_ready(model_id, True)
        self._assert_task_status(task_id, PUBLISHED)

    def test_publish_task_with_published_model(self):
        task_id, model_id = self._create_task_and_model()
        self.api.models.set_ready(model=model_id, publish_task=False)
        self._assert_model_ready(model_id, True)

        self.api.tasks.stopped(task=task_id)
        res = self.api.tasks.publish(task=task_id)
        assert res.updated == 1  # model updated
        self._assert_task_status(task_id, PUBLISHED)
        self._assert_model_ready(model_id, True)

    def test_publish_task_no_output_model(self):
        task_id = self.create_temp(service="tasks", type="testing", name="server-test")
        self.api.tasks.started(task=task_id)
        self.api.tasks.stopped(task=task_id)

        res = self.api.tasks.publish(task=task_id)
        assert res.updated == 1  # model updated
        self._assert_task_status(task_id, PUBLISHED)

    def test_get_models_stats(self):
        model1 = self._create_model(labels={"hello": 1, "world": 2})
        model2 = self._create_model(labels={"foo": 1})
        model3 = self._create_model()

        # no stats
        res = self.api.models.get_all_ex(id=[model1, model2, model3]).models
        self.assertEqual(len(res), 3)
        self.assertTrue(all("stats" not in m for m in res))

        # stats
        res = self.api.models.get_all_ex(
            id=[model1, model2, model3], include_stats=True
        ).models
        self.assertEqual(len(res), 3)
        stats = {m.id: m.stats.labels_count for m in res}
        self.assertEqual(stats[model1], 2)
        self.assertEqual(stats[model2], 1)
        self.assertEqual(stats[model3], 0)

    def test_update_model_iteration_with_task(self):
        task_id = self._create_task()
        model_id = self._create_model()
        self.api.models.update(
            model=model_id, task=task_id, iteration=1000, labels={"foo": 1}
        )

        self.assertEqual(
            self.api.tasks.get_by_id(task=task_id).task.last_iteration, 1000
        )

        self.api.models.update(model=model_id, task=task_id, iteration=500)

        self.assertEqual(
            self.api.tasks.get_by_id(task=task_id).task.last_iteration, 1000
        )

    def test_update_model_for_task_iteration(self):
        task_id = self._create_task()

        res = self.api.models.update_for_task(
            task=task_id, name="test model", uri="file:///b", iteration=999,
        )

        model_id = res.id

        self.defer(self.api.models.delete, can_fail=True, model=model_id, force=True)

        self.assertEqual(
            self.api.tasks.get_by_id(task=task_id).task.last_iteration, 999
        )

        self.api.models.update_for_task(task=task_id, uri="file:///c", iteration=1000)

        self.assertEqual(
            self.api.tasks.get_by_id(task=task_id).task.last_iteration, 1000
        )

        self.api.models.update_for_task(task=task_id, uri="file:///d", iteration=888)

        self.assertEqual(
            self.api.tasks.get_by_id(task=task_id).task.last_iteration, 1000
        )

    def test_get_frameworks(self):
        framework_1 = "Test framework 1"
        framework_2 = "Test framework 2"

        # create model on top level
        self._create_model(name="framework model test", framework=framework_1)

        # create model under a project as make it inherit its framework from the task
        project = self.create_temp("projects", name="Frameworks test", description="")
        task = self._create_task(project=project, execution=dict(framework=framework_2))
        self.api.models.update_for_task(
            task=task,
            name="framework output model test",
            uri="file:///b",
            iteration=999,
        )

        # get all frameworks
        res = self.api.models.get_frameworks()
        self.assertTrue({framework_1, framework_2}.issubset(set(res.frameworks)))

        # get frameworks under the project
        res = self.api.models.get_frameworks(projects=[project])
        self.assertEqual([framework_2], res.frameworks)

        # empty result
        self.api.tasks.delete(task=task, force=True)
        res = self.api.models.get_frameworks(projects=[project])
        self.assertEqual([], res.frameworks)

    @unittest.skip(
        """This test requires the following setting
        CLEARML__services__async_urls_delete__fileserver__url_prefixes=["https://files.allegro-master.hosted.allegro.ai"
        Check the test results in the logs of async_delete service
        """
    )
    def test_delete_many_with_files(self):
        models = [
            self._create_model(
                name=f"delete model test{idx}",
                uri=f"https://files.allegro-master.hosted.allegro.ai/models/test{idx}.txt"
            )
            for idx in range(2)
        ]
        self.api.models.delete_many(ids=models)


    def test_make_public(self):
        m1 = self._create_model(name="public model test")

        # model with company_origin not set to the current company cannot be converted to private
        with self.api.raises(InvalidModelId):
            self.api.models.make_private(ids=[m1])

        # public model can be retrieved but not updated
        res = self.api.models.make_public(ids=[m1])
        self.assertEqual(res.updated, 1)
        res = self.api.models.get_all(id=[m1])
        self.assertEqual([m.id for m in res.models], [m1])
        with self.api.raises(InvalidModelId):
            self.api.models.update(model=m1, name="public model test change 1")

        # task made private again and can be both retrieved and updated
        res = self.api.models.make_private(ids=[m1])
        self.assertEqual(res.updated, 1)
        res = self.api.models.get_all(id=[m1])
        self.assertEqual([m.id for m in res.models], [m1])
        self.api.models.update(model=m1, name="public model test change 2")

    def _assert_task_status(self, task_id, status):
        task = self.api.tasks.get_by_id(task=task_id).task
        assert task.status == status

    def _assert_model_ready(self, model_id, ready):
        model = self.api.models.get_by_id(model=model_id)["model"]
        assert model.ready == ready

    def _assert_update_model_failure(self):
        return self.api.raises(MODEL_CANNOT_BE_UPDATED_CODES)

    def _assert_update_task_failure(self):
        return self.api.raises(TASK_CANNOT_BE_UPDATED_CODES)

    def _create_model(self, **kwargs):
        return self.create_temp(
            service="models",
            delete_params=dict(can_fail=True, force=True),
            name=kwargs.pop("name", "test"),
            uri=kwargs.pop("uri", "file:///a"),
            labels=kwargs.pop("labels", {}),
            **kwargs,
        )

    def _create_task(self, **kwargs):
        task_id = self.create_temp(
            service="tasks",
            type=kwargs.pop("type", "testing"),
            name=kwargs.pop("name", "server-test"),
            **kwargs,
        )

        return task_id

    def _create_task_and_model(self):
        execution_model_id = self.create_temp(
            service="models",
            name="test",
            uri="https://files.trains-master.hosted.allegro.ai/a.jpg",
            labels={},
        )
        task_id = self.create_temp(
            service="tasks",
            type="testing",
            name="server-test",
            execution=dict(model=execution_model_id),
        )
        self.api.tasks.started(task=task_id)
        output_model_id = self.api.models.update_for_task(
            task=task_id, uri="file:///b"
        )["id"]

        return task_id, output_model_id
