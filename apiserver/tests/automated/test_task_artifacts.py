from operator import itemgetter
from typing import Sequence

from apiserver.tests.automated import TestService


class TestTasksArtifacts(TestService):
    def setUp(self, **kwargs):
        super().setUp(version="2.10")

    def new_task(self, **kwargs) -> str:
        self.update_missing(
            kwargs,
            type="testing",
            name="test artifacts",
            delete_params=dict(force=True),
        )
        return self.create_temp("tasks", **kwargs)

    def test_artifacts_set_get(self):
        artifacts = [
            dict(key="a", type="str", uri="test1"),
            dict(key="b", type="int", uri="test2"),
        ]

        # test create/get and get_all
        task = self.new_task(execution={"artifacts": artifacts})
        res = self.api.tasks.get_by_id(task=task).task
        self._assertTaskArtifacts(artifacts, res)
        res = self.api.tasks.get_all_ex(id=[task]).tasks[0]
        self._assertTaskArtifacts(artifacts, res)

        # test edit
        artifacts = [
            dict(key="bb", type="str", uri="http://files.clear.ml/test1", mode="output"),
            dict(key="aa", type="int", uri="http://files.clear.ml/test2", mode="input"),
        ]
        self.api.tasks.edit(task=task, execution={"artifacts": artifacts})
        res = self.api.tasks.get_by_id(task=task).task
        self._assertTaskArtifacts(artifacts, res)

        # test clone
        task2 = self.api.tasks.clone(task=task).id
        res = self.api.tasks.get_by_id(task=task2).task
        self._assertTaskArtifacts([a for a in artifacts if a["mode"] != "output"], res)

        new_artifacts = [
            dict(key="x", type="str", uri="x_test", mode="input"),
            dict(key="y", type="int", uri="y_test", mode="input"),
            dict(key="z", type="int", uri="y_test", mode="input"),
        ]
        new_task = self.api.tasks.clone(
            task=task, execution_overrides={"artifacts": new_artifacts}
        ).id
        res = self.api.tasks.get_by_id(task=new_task).task
        self._assertTaskArtifacts(new_artifacts, res)

    def test_artifacts_edit_delete(self):
        artifacts = [
            dict(key="a", type="str", uri="test1", mode="input"),
            dict(key="b", type="int", uri="test2"),
            dict(key="c", type="int", uri="test3"),
        ]
        task = self.new_task(execution={"artifacts": artifacts})

        # test add_or_update
        edit = [
            dict(key="a", type="str", uri="hello", mode="input"),
            dict(key="c", type="int", uri="world"),
        ]
        res = self.api.tasks.add_or_update_artifacts(task=task, artifacts=edit)
        artifacts = self._update_source(artifacts, edit)
        res = self.api.tasks.get_all_ex(id=[task]).tasks[0]
        self._assertTaskArtifacts(artifacts, res)

        # test delete
        self.api.tasks.delete_artifacts(task=task, artifacts=[{"key": artifacts[-1]["key"]}])
        res = self.api.tasks.get_all_ex(id=[task]).tasks[0]
        self._assertTaskArtifacts(artifacts[0: len(artifacts) - 1], res)

        # test edit running task
        self.api.tasks.started(task=task)
        self.api.tasks.add_or_update_artifacts(task=task, artifacts=edit)
        res = self.api.tasks.get_all_ex(id=[task]).tasks[0]
        self._assertTaskArtifacts(artifacts, res)
        self.api.tasks.delete_artifacts(task=task, artifacts=[{"key": artifacts[-1]["key"]}])
        res = self.api.tasks.get_all_ex(id=[task]).tasks[0]
        self._assertTaskArtifacts(artifacts[0: len(artifacts) - 1], res)

        self.api.tasks.reset(task=task)
        res = self.api.tasks.get_all_ex(id=[task]).tasks[0]
        self._assertTaskArtifacts(artifacts[0: 1], res)

    def _update_source(self, source: Sequence[dict], update: Sequence[dict]):
        dict1 = {s["key"]: s for s in source}
        dict2 = {u["key"]: u for u in update}
        res = {
            k: v if k not in dict2 else dict2[k]
            for k, v in dict1.items()
        }
        res.update({k: v for k, v in dict2.items() if k not in res})
        return list(res.values())

    def _assertTaskArtifacts(self, artifacts: Sequence[dict], task):
        task_artifacts: dict = task.execution.artifacts
        self.assertEqual(len(artifacts), len(task_artifacts))

        for expected, actual in zip(
            sorted(artifacts, key=itemgetter("key", "type")), task_artifacts
        ):
            self.assertEqual(
                expected, {k: v for k, v in actual.items() if k in expected}
            )
