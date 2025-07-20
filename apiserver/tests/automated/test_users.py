from typing import Sequence
from uuid import uuid4

from apiserver.config_repo import config
from apiserver.tests.automated import TestService

log = config.logger(__file__)


class TestUsersService(TestService):
    def setUp(self, version="2.8"):
        super(TestUsersService, self).setUp(version=version)
        self.company = self.api.users.get_current_user().user.company.id

    def new_user(self):
        user_name = uuid4().hex
        user_id = self.api.auth.create_user(
            company=self.company, name=user_name, email="{0}@{0}.com".format(user_name)
        ).id
        self.defer(self.api.users.delete, user=user_id)
        return user_id

    def test_active_users(self):
        user_1 = self.new_user()
        user_2 = self.new_user()
        user_3 = self.new_user()

        model = (
            self.api.impersonate(user_2)
            .models.create(name="test", uri="file:///a", labels={})
            .id
        )
        self.defer(self.api.models.delete, model=model)
        project = self.create_temp("projects", name="users test", description="")
        task = (
            self.api.impersonate(user_3)
            .tasks.create(
                name="test", type="testing", project=project
            )
            .id
        )
        self.defer(self.api.tasks.delete, task=task, move_to_trash=False)

        user_ids = [user_1, user_2, user_3]
        # no projects filtering
        users = self.api.users.get_all_ex(id=user_ids).users
        self._assertUsers((user_1, user_2, user_3), users)

        # all projects
        users = self.api.users.get_all_ex(id=user_ids, active_in_projects=[]).users
        self._assertUsers((user_2, user_3), users)

        # specific project
        users = self.api.users.get_all_ex(id=user_ids, active_in_projects=[project]).users
        self._assertUsers((user_3,), users)

    def _assertUsers(self, expected: Sequence, users: Sequence):
        self.assertEqual(set(expected), set(u.id for u in users))

    def test_no_preferences(self):
        user = self.new_user()
        assert self.api.impersonate(user).users.get_preferences().preferences == {}

    def _test_update(self, user, tests):
        """
        Check that all for each (updates, expected_result) pair, ``updates`` yield ``result``.
        """
        new_user_client = self.api.impersonate(user)
        for update, expected in tests:
            new_user_client.users.set_preferences(user=user, preferences=update)
            preferences = new_user_client.users.get_preferences(user=user).preferences
            self.assertEqual(preferences, expected)

    def test_nested_update(self):
        tests = [
            ({"a": 0}, {"a": 0}),
            ({"b": 1}, {"a": 0, "b": 1}),
            ({"section.a": 2}, {"a": 0, "b": 1, "section": {"a": 2}}),
        ]
        self._test_update(self.new_user(), tests)

    def test_delete(self):
        tests = [
            ({"section": {"a": 0, "b": 1}},) * 2,
            ({"section": {"a": None}}, {"section": {"a": None}}),
            ({"section": None}, {"section": None}),
        ]
        self._test_update(self.new_user(), tests)
