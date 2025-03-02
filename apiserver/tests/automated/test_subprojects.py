from time import sleep
from typing import Sequence, Optional, Tuple

from boltons.iterutils import first

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model import EntityVisibility
from apiserver.database.utils import id as db_id
from apiserver.tests.api_client import APIClient
from apiserver.tests.automated import TestService


class TestSubProjects(TestService):
    def test_dataset_stats(self):
        project = self._temp_project(name="Dataset test", system_tags=["dataset"])
        res = self.api.organization.get_entities_count(
            datasets={"system_tags": ["dataset"]}, allow_public=False,
        )
        self.assertEqual(res.datasets, 1)

        task = self._temp_task(project=project)
        data = self.api.projects.get_all_ex(
            id=[project], include_dataset_stats=True
        ).projects[0]
        self.assertIsNone(data.dataset_stats)

        self.api.tasks.edit(
            task=task, runtime={"ds_file_count": 2, "ds_total_size": 1000}
        )
        data = self.api.projects.get_all_ex(
            id=[project], include_dataset_stats=True
        ).projects[0]
        self.assertEqual(data.dataset_stats, {"file_count": 2, "total_size": 1000})

    def test_query_children_system_tags(self):
        test_root_name = "TestQueryChildrenTags"
        test_root = self._temp_project(name=test_root_name)
        project1 = self._temp_project(name=f"{test_root_name}/project1")
        project2 = self._temp_project(name=f"{test_root_name}/project2")
        self._temp_report(name="test report", project=project1)
        self._temp_report(name="test report", project=project2, tags=["test1", "test2"])
        self._temp_report(name="test report", project=project2, tags=["test1"])

        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 2)

        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags=["test1", "test2"],
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.basename, "project2")
        self.assertEqual(p.stats.active.total_tasks, 2)

        # new filter
        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags_filter={"any": {"include": ["test1", "test2"]}},
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.basename, "project2")
        self.assertEqual(p.stats.active.total_tasks, 2)

        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags=["__$all", "test1", "test2"],
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.basename, "project2")
        self.assertEqual(p.stats.active.total_tasks, 1)

        # new filter
        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags_filter={"all": {"include": ["test1", "test2"]}},
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.basename, "project2")
        self.assertEqual(p.stats.active.total_tasks, 1)

        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags=["-test1", "-test2"],
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.basename, "project1")
        self.assertEqual(p.stats.active.total_tasks, 1)

        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags=["__$any", "__$not", "test1", "test2"],
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 2)
        for p in projects:
            self.assertEqual(p.stats.active.total_tasks, 1)

        # new filter
        projects = self.api.projects.get_all_ex(
            parent=[test_root],
            children_type="report",
            children_tags_filter={"all": {"exclude": ["test1", "test2"]}},
            shallow_search=True,
            include_stats=True,
            check_own_contents=True,
        ).projects
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.basename, "project1")
        self.assertEqual(p.stats.active.total_tasks, 1)

    def test_query_children(self):
        test_root_name = "TestQueryChildren"
        test_root = self._temp_project(name=test_root_name)
        dataset_tags = ["hello", "world"]
        dataset_project = self._temp_project(
            name=f"{test_root_name}/Project1/Dataset",
            system_tags=["dataset"],
            tags=dataset_tags,
        )
        self._temp_task(
            name="dataset task",
            type="data_processing",
            system_tags=["dataset"],
            project=dataset_project,
        )
        self._temp_task(name="regular task", project=dataset_project)
        pipeline_project = self._temp_project(
            name=f"{test_root_name}/Project2/Pipeline", system_tags=["pipeline"]
        )
        self._temp_task(
            name="pipeline task",
            type="controller",
            system_tags=["pipeline"],
            project=pipeline_project,
        )
        self._temp_task(name="regular task", project=pipeline_project)
        report_project = self._temp_project(name=f"{test_root_name}/Project3")
        self._temp_report(name="test report", project=report_project)
        self._temp_task(name="regular task", project=report_project)

        projects = self.api.projects.get_all_ex(
            parent=[test_root], shallow_search=True, include_stats=True
        ).projects
        self.assertEqual(
            {p.basename for p in projects}, {f"Project{idx+1}" for idx in range(3)}
        )
        for p in projects:
            self.assertEqual(
                p.stats.active.total_tasks,
                2 if p.basename in ("Project1", "Project2") else 1,
            )

        for i, type_ in enumerate(("dataset", "pipeline", "report")):
            projects = self.api.projects.get_all_ex(
                parent=[test_root],
                children_type=type_,
                shallow_search=True,
                include_stats=True,
                check_own_contents=True,
            ).projects
            self.assertEqual({p.basename for p in projects}, {f"Project{i+1}"})
            p = projects[0]
            if type_ in ("dataset",):
                self.assertEqual(p.own_datasets, 1)
                self.assertIsNone(p.get("own_tasks"))
                self.assertEqual(p.stats.datasets.count, 1)
                self.assertEqual(p.stats.datasets.tags, dataset_tags)
            else:
                self.assertEqual(p.own_tasks, 0)
                self.assertIsNone(p.get("own_datasets"))
                self.assertEqual(p.stats.active.total_tasks, 1)

    def test_project_aggregations(self):
        """This test requires user with user_auth_only... credentials in db"""
        user2_client = APIClient(
            api_key=config.get("apiclient.user_auth_only"),
            secret_key=config.get("apiclient.user_auth_only_secret"),
            base_url=f"http://localhost:8008/v2.13",
        )

        basename = "Pr1"
        child = self._temp_project(name=f"Aggregation/{basename}", client=user2_client)
        project = self.api.projects.get_all_ex(name="^Aggregation$").projects[0].id
        child_project = self.api.projects.get_all_ex(id=[child]).projects[0]
        self.assertEqual(child_project.parent.id, project)
        self.assertEqual(child_project.basename, basename)
        user = self.api.users.get_current_user().user.id

        # test aggregations on project with empty subprojects
        res = self.api.users.get_all_ex(active_in_projects=[project])
        self.assertEqual(res.users, [])
        res = self.api.projects.get_all_ex(id=[project], active_users=[user])
        self.assertEqual(res.projects, [])
        res = self.api.models.get_frameworks(projects=[project])
        self.assertEqual(res.frameworks, [])
        res = self.api.tasks.get_types(projects=[project])
        self.assertEqual(res.types, [])
        res = self.api.projects.get_task_parents(projects=[project])
        self.assertEqual(res.parents, [])
        res = self.api.projects.get_user_names(projects=[project])
        self.assertEqual(res.users, [])
        res = self.api.organization.get_entities_count(
            projects={"id": [project]}, active_users=[user]
        )
        self.assertEqual(res.projects, 0)

        # test aggregations with non-empty subprojects
        task1 = self._temp_task(project=child)
        self._temp_task(project=child, parent=task1)
        user2_task = self._temp_task(project=child, client=user2_client)
        framework = "Test framework"
        self._temp_model(project=child, framework=framework)
        res = self.api.users.get_all_ex(active_in_projects=[project])
        self._assert_ids(res.users, [user])
        res = self.api.projects.get_all_ex(id=[project], include_stats=True)
        self._assert_ids(res.projects, [project])
        self.assertEqual(res.projects[0].stats.active.total_tasks, 3)
        res = self.api.projects.get_all_ex(
            id=[project], active_users=[user], include_stats=True
        )
        self._assert_ids(res.projects, [project])
        self.assertEqual(res.projects[0].stats.active.total_tasks, 2)
        res = self.api.projects.get_task_parents(projects=[project])
        self._assert_ids(res.parents, [task1])
        res = self.api.projects.get_user_names(projects=[project])
        self.assertEqual(res.users, [{"id": "Test1", "name": "Test User"}])
        res = self.api.models.get_frameworks(projects=[project])
        self.assertEqual(res.frameworks, [framework])
        res = self.api.tasks.get_types(projects=[project])
        self.assertEqual(res.types, ["testing"])
        res = self.api.organization.get_entities_count(
            projects={"id": [project]}, active_users=[user]
        )
        self.assertEqual(res.projects, 1)

    def _assert_ids(self, actual: Sequence[dict], expected: Sequence[str]):
        self.assertEqual([a["id"] for a in actual], expected)

    def test_project_operations(self):
        # create
        with self.api.raises(errors.bad_request.InvalidProjectName):
            self._temp_project(name="/")
        project1 = self._temp_project(name="Root1/Pr1")
        project1_child = self._temp_project(name="Root1/Pr1/Pr2")
        with self.api.raises(errors.bad_request.ExpectedUniqueData):
            self._temp_project(name="Root1/Pr1/Pr2")

        # update
        with self.api.raises(errors.bad_request.CannotUpdateProjectLocation):
            self.api.projects.update(project=project1, name="Root2/Pr2")
        new_basename = "Pr2"
        res = self.api.projects.update(project=project1, name=f"Root1/{new_basename}")
        self.assertEqual(res.updated, 1)
        res = self.api.projects.get_by_id(project=project1)
        self.assertEqual(res.project.basename, new_basename)
        res = self.api.projects.get_by_id(project=project1_child)
        self.assertEqual(res.project.name, "Root1/Pr2/Pr2")

        # move
        res = self.api.projects.move(project=project1, new_location="Root2")
        self.assertEqual(res.moved, 2)
        res = self.api.projects.get_by_id(project=project1_child)
        self.assertEqual(res.project.name, "Root2/Pr2/Pr2")
        self.assertEqual(res.project.basename, "Pr2")

        # merge
        project_with_task, (active, archived) = self._temp_project_with_tasks(
            "Root1/Pr3/Pr4"
        )
        project1_parent = self._getProjectParent(project1)
        self._assertTags(project1_parent, tags=[], system_tags=[])
        self._assertTags(project1_parent, tags=[], system_tags=[])
        project_with_task_parent = self._getProjectParent(project_with_task)
        self._assertTags(project_with_task_parent)
        # self._assertTags(project_id=None)

        merge_source = self.api.projects.get_by_id(
            project=project_with_task
        ).project.parent
        res = self.api.projects.merge(
            project=merge_source, destination_project=project1
        )
        self.assertEqual(res.moved_entities, 0)
        self.assertEqual(res.moved_projects, 1)
        res = self.api.projects.get_by_id(project=project_with_task)
        self.assertEqual(res.project.name, "Root2/Pr2/Pr4")
        self.assertEqual(res.project.basename, "Pr4")
        with self.api.raises(errors.bad_request.InvalidProjectId):
            self.api.projects.get_by_id(project=merge_source)

        self._assertTags(project1_parent)
        self._assertTags(project1)
        self._assertTags(project_with_task_parent, tags=[], system_tags=[])
        # self._assertTags(project_id=None)

        # delete
        with self.api.raises(errors.bad_request.ProjectHasTasks):
            self.api.projects.delete(project=project1)
        res = self.api.projects.delete(project=project1, force=True)
        self.assertEqual(res.deleted, 3)
        self.assertEqual(res.disassociated_tasks, 2)
        res = self.api.tasks.get_by_id(task=active).task
        self.assertIsNone(res.get("project"))
        for p_id in (project1, project1_child, project_with_task):
            with self.api.raises(errors.bad_request.InvalidProjectId):
                self.api.projects.get_by_id(project=p_id)

        self._assertTags(project1_parent, tags=[], system_tags=[])
        # self._assertTags(project_id=None, tags=[], system_tags=[])

    def _getProjectParent(self, project_id: str):
        return self.api.projects.get_all_ex(id=[project_id]).projects[0].parent.id

    def _assertTags(
        self,
        project_id: Optional[str],
        tags: Sequence[str] = ("test",),
        system_tags: Sequence[str] = (EntityVisibility.archived.value,),
    ):
        if project_id:
            res = self.api.projects.get_task_tags(
                projects=[project_id], include_system=True
            )
        else:
            res = self.api.organization.get_tags(include_system=True)

        self.assertEqual(set(res.tags), set(tags))
        self.assertEqual(set(res.system_tags), set(system_tags))

    def test_get_all_search_options(self):
        project1 = self._temp_project(name="project1")
        project2 = self._temp_project(name="project1/project2")
        self._temp_project(name="project3")

        # local search finds only at the specified level
        res = self.api.projects.get_all_ex(
            name="project1", shallow_search=True
        ).projects
        self.assertEqual([p.id for p in res], [project1])
        res = self.api.projects.get_all_ex(name="project1", parent=[project1]).projects
        self.assertEqual([p.id for p in res], [project2])
        # basename search
        res = self.api.projects.get_all_ex(
            basename="project2", shallow_search=True
        ).projects
        self.assertEqual(res, [])

        # global search finds all or below the specified level
        res = self.api.projects.get_all_ex(name="project1").projects
        self.assertEqual(set(p.id for p in res), {project1, project2})
        project4 = self._temp_project(name="project1/project2/project1")
        res = self.api.projects.get_all_ex(name="project1", parent=[project2]).projects
        self.assertEqual([p.id for p in res], [project4])
        # basename search
        res = self.api.projects.get_all_ex(basename="project2").projects
        self.assertEqual([p.id for p in res], [project2])
        self.api.projects.delete(project=project1, force=True)

    def test_include_subprojects(self):
        project1, _ = self._temp_project_with_tasks(name="project1x")
        project2, _ = self._temp_project_with_tasks(name="project1x/project22")
        self._temp_model(project=project1)
        self._temp_model(project=project2)

        # tasks
        res = self.api.tasks.get_all_ex(project=project1).tasks
        self.assertEqual(len(res), 2)
        res = self.api.tasks.get_all(project=project1).tasks
        self.assertEqual(len(res), 2)
        res = self.api.tasks.get_all_ex(
            project=project1, include_subprojects=True
        ).tasks
        self.assertEqual(len(res), 4)
        res = self.api.tasks.get_all(project=project1, include_subprojects=True).tasks
        self.assertEqual(len(res), 4)

        # models
        res = self.api.models.get_all_ex(project=project1).models
        self.assertEqual(len(res), 1)
        res = self.api.models.get_all(project=project1).models
        self.assertEqual(len(res), 1)
        res = self.api.models.get_all_ex(
            project=project1, include_subprojects=True
        ).models
        self.assertEqual(len(res), 2)
        res = self.api.models.get_all(project=project1, include_subprojects=True).models
        self.assertEqual(len(res), 2)

    def test_get_all_with_check_own_contents(self):
        project1, _ = self._temp_project_with_tasks(name="project1x")
        project2 = self._temp_project(name="project2x")
        self._temp_project_with_tasks(name="project2x/project22")
        self._temp_model(project=project1)

        res = self.api.projects.get_all_ex(
            id=[project1, project2], check_own_contents=True
        ).projects
        res1 = next(p for p in res if p.id == project1)
        self.assertEqual(res1.own_tasks, 1)
        self.assertEqual(res1.own_models, 1)

        res2 = next(p for p in res if p.id == project2)
        self.assertEqual(res2.own_tasks, 0)
        self.assertEqual(res2.own_models, 0)

    def test_public_names_clash(self):
        # cannot create a project with a name that match public existing project
        with self.api.raises(errors.bad_request.PublicProjectExists):
            project = self._temp_project(name="ClearML Examples")

        # cannot create a subproject under a public project
        with self.api.raises(errors.bad_request.PublicProjectExists):
            project = self._temp_project(name="ClearML Examples/my project")

    def test_get_all_with_stats(self):
        project4, _ = self._temp_project_with_tasks(name="project1/project3/project4")
        project5, _ = self._temp_project_with_tasks(name="project1/project3/project5")
        project2 = self._temp_project(name="project2")
        res = self.api.projects.get_all(shallow_search=True).projects
        self.assertTrue(any(p for p in res if p.id == project2))
        self.assertFalse(any(p for p in res if p.id in [project4, project5]))

        project1 = first(p.id for p in res if p.name == "project1")
        res = self.api.projects.get_all_ex(
            id=[project1, project2], include_stats=True
        ).projects
        self.assertEqual(set(p.id for p in res), {project1, project2})
        res1 = next(p for p in res if p.id == project1)
        self.assertEqual(res1.stats["active"]["status_count"]["created"], 0)
        self.assertEqual(res1.stats["active"]["status_count"]["stopped"], 2)
        self.assertEqual(res1.stats["active"]["status_count"]["in_progress"], 0)
        self.assertEqual(res1.stats["active"]["total_runtime"], 2)
        self.assertEqual(res1.stats["active"]["completed_tasks_24h"], 2)
        self.assertEqual(res1.stats["active"]["total_tasks"], 2)
        self.assertEqual(
            {sp.name for sp in res1.sub_projects},
            {
                "project1/project3",
                "project1/project3/project4",
                "project1/project3/project5",
            },
        )
        res2 = next(p for p in res if p.id == project2)
        self.assertEqual(res2.stats["active"]["status_count"]["created"], 0)
        self.assertEqual(res2.stats["active"]["status_count"]["stopped"], 0)
        self.assertEqual(res2.stats["active"]["status_count"]["in_progress"], 0)
        self.assertEqual(res2.stats["active"]["status_count"]["completed"], 0)
        self.assertEqual(res2.stats["active"]["total_runtime"], 0)
        self.assertEqual(res2.stats["active"]["total_tasks"], 0)
        self.assertEqual(res2.sub_projects, [])

    def _run_tasks(self, *tasks):
        """Imitate 1 second of running"""
        for task_id in tasks:
            self.api.tasks.started(task=task_id)
        sleep(1)
        for task_id in tasks:
            self.api.tasks.stopped(task=task_id)

    def _temp_project_with_tasks(self, name) -> Tuple[str, Tuple[str, str]]:
        pr_id = self._temp_project(name=name)
        task_active = self._temp_task(project=pr_id)
        task_archived = self._temp_task(
            project=pr_id, system_tags=[EntityVisibility.archived.value], tags=["test"]
        )
        self._run_tasks(task_active, task_archived)
        return pr_id, (task_active, task_archived)

    delete_params = dict(can_fail=True, force=True)

    def _temp_project(self, name, client=None, **kwargs):
        return self.create_temp(
            "projects",
            delete_params=self.delete_params,
            name=name,
            description="",
            client=client,
            **kwargs,
        )

    def _temp_report(self, name, **kwargs):
        return self.create_temp(
            "reports",
            name=name,
            object_name="task",
            delete_params=self.delete_params,
            **kwargs,
        )

    def _temp_task(self, client=None, name=None, type=None, **kwargs):
        return self.create_temp(
            "tasks",
            delete_params=self.delete_params,
            type=type or "testing",
            name=name or db_id(),
            client=client,
            **kwargs,
        )

    def _temp_model(self, **kwargs):
        return self.create_temp(
            service="models",
            delete_params=self.delete_params,
            name="test",
            uri="file:///a",
            labels={},
            **kwargs,
        )
