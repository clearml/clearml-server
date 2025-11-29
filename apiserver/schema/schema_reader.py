"""
Objects representing schema entities
"""
import json
import re
from operator import attrgetter
from pathlib import Path
from typing import Mapping, Sequence

import attr
from boltons.dictutils import subdict
from pyhocon import ConfigFactory

from apiserver.config_repo import config
from apiserver.utilities.partial_version import PartialVersion


log = config.logger(__file__)

ALL_ROLES = "*"


class EndpointSchema:
    REQUEST_KEY = "request"
    RESPONSE_KEY = "response"
    BATCH_REQUEST_KEY = "batch_request"
    DEFINITIONS_KEY = "definitions"

    def __init__(
        self,
        service_name: str,
        action_name: str,
        version: PartialVersion,
        schema: dict,
        definitions: dict = None,
    ):
        """
        Class for interacting with the schema of a single endpoint
        :param service_name: name of containing service
        :param action_name: name of action
        :param version: endpoint version
        :param schema: endpoint schema
        :param definitions: service definitions
        """
        self.service_name = service_name
        self.action_name = action_name
        self.full_name = f"{service_name}.{action_name}"
        self.version = version
        self.definitions = definitions
        self.request_schema = None
        self.batch_request_schema = None
        if self.REQUEST_KEY in schema:
            self.request_schema = {
                **schema[self.REQUEST_KEY],
                self.DEFINITIONS_KEY: self.definitions,
            }
        elif self.BATCH_REQUEST_KEY in schema:
            self.batch_request_schema = {
                **schema[self.BATCH_REQUEST_KEY],
                self.DEFINITIONS_KEY: self.definitions,
            }
        else:
            raise RuntimeError(
                f"endpoint {self.full_name} version {self.version} "
                "has no request or batch_request schema",
                schema,
            )
        self.response_schema = {
            **schema[self.RESPONSE_KEY],
            "definitions": self.definitions,
        }


class EndpointVersionsGroup:

    endpoints: Sequence[EndpointSchema]
    allow_roles: Sequence[str]
    internal: bool
    authorize: bool

    def __repr__(self):
        return (
            f"{type(self).__name__}<{self.full_name}, "
            f"versions={tuple(e.version for e in self.endpoints)}>"
        )

    def __init__(
        self,
        service_name: str,
        action_name: str,
        conf: dict,
        definitions: dict = None,
        defaults: dict = None,
    ):
        """
        Represents multiple implementations of a single endpoint, discriminated by API version
        :param service_name: name of containing service
        :param action_name: name of action
        :param conf: mapping between minimum version to endpoint schema
        :param definitions: service definitions
        :param defaults: service defaults
        """
        self.service_name = service_name
        self.action_name = action_name
        self.full_name = f"{service_name}.{action_name}"
        self.definitions = definitions or {}
        self.defaults = defaults or {}
        self.internal = self._pop_attr_with_default(conf, "internal")
        self.allow_roles = self._pop_attr_with_default(conf, "allow_roles")
        self.authorize = self._pop_attr_with_default(conf, "authorize")
        self.community_only = self._pop_attr_with_default(conf, "community_only")

        def parse_version(version):
            if not re.match(r"^\d+\.\d+$", version):
                raise ValueError(
                    f"Encountered unrecognized key {version!r} in {self.service_name}.{self.action_name}"
                )
            return PartialVersion(version)

        self.endpoints = sorted(
            (
                EndpointSchema(
                    service_name=self.service_name,
                    action_name=self.action_name,
                    version=parse_version(version),
                    schema=endpoint_conf,
                    definitions=self.definitions,
                )
                for version, endpoint_conf in conf.items()
            ),
            key=attrgetter("version"),
        )

    def allows(self, role):
        return ALL_ROLES in self.allow_roles or role in self.allow_roles

    def _pop_attr_with_default(self, conf, attr):
        return conf.pop(attr, self.defaults[attr])

    def get_for_version(self, min_version: PartialVersion):
        """
        Return endpoint schema for version
        """
        if not self.endpoints:
            raise ValueError(f"endpoint group {self} has no versions")
        for endpoint in self.endpoints:
            if min_version <= endpoint.version:
                return endpoint
        raise ValueError(
            f"min_version {min_version} is higher than highest version in group {self}"
        )


class Service:

    endpoint_groups: Mapping[str, EndpointVersionsGroup]

    def __init__(self, name: str, conf: dict, api_defaults: dict):
        """
        Represents schema of one service
        :param name: name of service
        :param conf: service configuration, containing endpoint groups and other details
        :param api_defaults: API-wide endpoint attributes default values
        """
        self.name = name
        conf = subdict(conf, drop=("_description", "_references"))
        self.defaults = {**api_defaults, **conf.pop("_default", {})}
        self.definitions = conf.pop("_definitions", None)
        self.endpoint_groups: Mapping[str, EndpointVersionsGroup] = {
            endpoint_name: EndpointVersionsGroup(
                service_name=self.name,
                action_name=endpoint_name,
                conf=endpoint_conf,
                defaults=self.defaults,
                definitions=self.definitions,
            )
            for endpoint_name, endpoint_conf in conf.items()
        }


class Schema:
    services: Mapping[str, Service]

    def __init__(self, services: dict, api_defaults: dict):
        """
        Represents the entire API schema
        :param services: services schema
        :param api_defaults: default values of service configuration
        """
        self.api_defaults = api_defaults
        self.services = {
            name: Service(name, conf, api_defaults=self.api_defaults)
            for name, conf in services.items()
        }


@attr.s()
class SchemaReader:
    root = Path(__file__).parent / "services"
    cache_path: Path = None

    def __attrs_post_init__(self):
        if not self.cache_path:
            self.cache_path = self.root / "_cache.json"

    @staticmethod
    def mod_time(path):
        """
        return file modification time
        """
        return path.stat().st_mtime

    @staticmethod
    def read_file(path):
        return ConfigFactory.parse_file(path).as_plain_ordered_dict()

    def get_schema(self):
        """
        Parse the API schema to schema object.
        Load from config files and write to cache file if possible.
        """
        services = [
            service
            for service in self.root.glob("*.conf")
            if not service.name.startswith("_")
        ]

        current_services_names = {path.stem for path in services}

        try:
            if self.mod_time(self.cache_path) >= max(map(self.mod_time, services)):
                log.info("loading schema from cache")
                result = json.loads(self.cache_path.read_text())
                cached_services_names = set(result.pop("services_names", []))
                if cached_services_names == current_services_names:
                    return Schema(**result)
                else:
                    log.info(
                        f"found services files changed: "
                        f"added: {list(current_services_names - cached_services_names)}, "
                        f"removed: {list(cached_services_names - current_services_names)}"
                    )
        except (IOError, KeyError, TypeError, ValueError, AttributeError) as ex:
            log.warning(f"failed loading cache: {ex}")

        log.info("regenerating schema cache")
        services = {path.stem: self.read_file(path) for path in services}
        api_defaults = self.read_file(self.root / "_api_defaults.conf")

        try:
            self.cache_path.write_text(
                json.dumps(
                    dict(
                        services_names=list(current_services_names),
                        services=services,
                        api_defaults=api_defaults,
                    )
                )
            )
        except IOError:
            log.exception(f"failed cache file to {self.cache_path}")

        return Schema(services, api_defaults)
