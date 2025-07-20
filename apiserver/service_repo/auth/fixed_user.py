import hashlib
from functools import lru_cache
from typing import Sequence, Optional

import attr

from apiserver.config_repo import config
from apiserver.config.info import get_default_company
from apiserver.database.model.auth import Role


class FixedUsersError(Exception):
    pass


@attr.s(auto_attribs=True)
class FixedUser:
    username: str
    password: str
    name: str
    company: str = get_default_company()
    id: str = attr.field(init=False)
    email: str = attr.field(init=False)
    role: str = attr.field(init=False)

    is_guest: bool = False

    def __attrs_post_init__(self):
        self.id = hashlib.md5(f"{self.company}:{self.username}".encode()).hexdigest()
        self.email = f"{self.id}@example.com"
        self.role = Role.guest if self.is_guest else Role.user


    @classmethod
    def enabled(cls):
        return config.get("apiserver.auth.fixed_users.enabled", False)

    @classmethod
    def guest_enabled(cls):
        return cls.enabled() and config.get("services.auth.fixed_users.guest.enabled", False)

    @classmethod
    def pass_hashed(cls):
        return config.get("apiserver.auth.fixed_users.pass_hashed", False)

    @classmethod
    def validate(cls):
        if not cls.enabled():
            return
        users = cls.from_config()
        if len({user.username for user in users}) < len(users):
            raise FixedUsersError(
                "Duplicate user names found in fixed users configuration"
            )

    @classmethod
    # @lru_cache()
    def from_config(cls) -> Sequence["FixedUser"]:
        users = [
            cls(**user) for user in config.get("apiserver.auth.fixed_users.users", [])
        ]

        if cls.guest_enabled():
            users.insert(
                0,
                cls.get_guest_user()
            )

        return users

    @classmethod
    @lru_cache()
    def get_by_username(cls, username) -> "FixedUser":
        return next(
            (user for user in cls.from_config() if user.username == username), None
        )

    @classmethod
    @lru_cache()
    def is_guest_endpoint(cls, service, action):
        """
        Validate a potential guest user,
        This method will verify the user is indeed the guest user,
         and that the guest user may access the service/action using its username/password
        """
        return any(
            ep == ".".join((service, action))
            for ep in config.get("services.auth.fixed_users.guest.allow_endpoints", [])
        )

    @classmethod
    def get_guest_user(cls) -> Optional["FixedUser"]:
        if cls.guest_enabled():
            return cls(
                is_guest=True,
                username=config.get("services.auth.fixed_users.guest.username"),
                password=config.get("services.auth.fixed_users.guest.password"),
                name=config.get("services.auth.fixed_users.guest.name"),
                company=config.get("services.auth.fixed_users.guest.default_company"),
            )

    def __hash__(self):
        return hash(self.id)
