from os import getenv

from boltons.iterutils import first
from furl import furl
from jsonmodels import models
from jsonmodels.errors import ValidationError
from jsonmodels.fields import StringField
from mongoengine import register_connection
from mongoengine.connection import get_connection, disconnect

from apiserver.config_repo import config
from .defs import Database
from .utils import get_items

log = config.logger("database")

strict = config.get("apiserver.mongo.strict", True)

OVERRIDE_HOST_ENV_KEY = (
    "CLEARML_MONGODB_SERVICE_HOST",
    "TRAINS_MONGODB_SERVICE_HOST",
    "MONGODB_SERVICE_HOST",
    "MONGODB_SERVICE_SERVICE_HOST",
)
OVERRIDE_PORT_ENV_KEY = (
    "CLEARML_MONGODB_SERVICE_PORT",
    "TRAINS_MONGODB_SERVICE_PORT",
    "MONGODB_SERVICE_PORT",
)

OVERRIDE_CONNECTION_STRING_ENV_KEY = "CLEARML_MONGODB_SERVICE_CONNECTION_STRING"
OVERRIDE_USERNAME_ENV_KEY = "CLEARML_MONGODB_SERVICE_USERNAME"
OVERRIDE_PASSWORD_ENV_KEY = "CLEARML_MONGODB_SERVICE_PASSWORD"
OVERRIDE_QUERY_ENV_KEY = "CLEARML_MONGODB_SERVICE_QUERY"


class DatabaseEntry(models.Base):
    host = StringField(required=True)
    alias = StringField()
    name = StringField()
    db = StringField()


class DatabaseFactory:
    _entries = []

    @classmethod
    def _create_db_entry(cls, alias: str, settings: dict) -> DatabaseEntry:
        return DatabaseEntry(alias=alias, **settings)

    @classmethod
    def initialize(cls):
        db_entries = config.get("hosts.mongo", {})
        missing = []
        log.info("Initializing database connections")

        override_connection_string = getenv(OVERRIDE_CONNECTION_STRING_ENV_KEY)
        override_hostname = first(map(getenv, OVERRIDE_HOST_ENV_KEY), None)
        override_port = first(map(getenv, OVERRIDE_PORT_ENV_KEY), None)
        override_username = getenv(OVERRIDE_USERNAME_ENV_KEY)
        override_password = getenv(OVERRIDE_PASSWORD_ENV_KEY)
        override_query = getenv(OVERRIDE_QUERY_ENV_KEY)

        if override_connection_string:
            log.info(f"Using override mongodb connection string template {override_connection_string}")
        else:
            if override_hostname:
                log.info(f"Using override mongodb host {override_hostname}")
            if override_port:
                log.info(f"Using override mongodb port {override_port}")
            if override_username:
                log.info(f"Using override mongodb username {override_username}")
            if override_password:
                log.info(f"Using override mongodb password ******")
            if override_query:
                log.info(f"Using override mongodb query {override_query}")

        for key, alias in get_items(Database).items():
            if key not in db_entries:
                missing.append(key)
                continue

            settings = {**db_entries.get(key)}
            if not any(field in settings for field in ("name", "db")):
                settings["name"] = key
            entry = cls._create_db_entry(alias=alias, settings=settings)

            if override_connection_string:
                con_str = override_connection_string
                log.info(f"Using override mongodb connection string for {alias}: {con_str}")
                entry.host = con_str
            else:
                if override_hostname:
                    entry.host = furl(entry.host).set(host=override_hostname).url
                if override_port:
                    entry.host = furl(entry.host).set(port=override_port).url
                if override_username:
                    entry.host = furl(entry.host).set(username=override_username).url
                if override_password:
                    entry.host = furl(entry.host).set(password=override_password).url
                if override_query:
                    entry.host = furl(entry.host).set(query=override_query).url

            try:
                entry.validate()
                log.info(
                    "Registering connection to %(alias)s (%(host)s)" % entry.to_struct()
                )
                register_connection(**entry.to_struct())

                cls._entries.append(entry)
            except ValidationError as ex:
                raise Exception("Invalid database entry `%s`: %s" % (key, ex.args[0]))
        if missing:
            raise ValueError(
                "Missing database configuration for %s" % ", ".join(missing)
            )

    @classmethod
    def get_entries(cls):
        return cls._entries

    @classmethod
    def get_hosts(cls):
        return [entry.host for entry in cls.get_entries()]

    @classmethod
    def get_aliases(cls):
        return [entry.alias for entry in cls.get_entries()]

    @classmethod
    def reconnect(cls):
        for entry in cls.get_entries():
            # there is bug in the current implementation that prevents
            # reconnection from work so workaround this
            # get_connection(entry.alias, reconnect=True)
            disconnect(entry.alias)
            register_connection(**entry.to_struct())
            get_connection(entry.alias)


db = DatabaseFactory()
