from mongoengine import (
    StringField,
    EmbeddedDocument,
    EmbeddedDocumentListField,
    EmailField,
    DateTimeField,
    BooleanField,
)

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin
from apiserver.database.model.base import AuthDocument
from apiserver.database.utils import get_options


class Entities(object):
    company = "company"
    task = "task"
    user = "user"
    model = "model"


class Role(object):
    system = "system"
    """ Internal system component """
    root = "root"
    """ Root admin (person) """
    admin = "admin"
    """ Company administrator """
    superuser = "superuser"
    """ Company super user """
    user = "user"
    """ Company user """
    annotator = "annotator"
    """ Annotator with limited access"""
    guest = "guest"
    """ Guest user. Read Only."""

    @classmethod
    def get_system_roles(cls) -> set:
        return {cls.system, cls.root}

    @classmethod
    def get_company_roles(cls) -> set:
        return set(get_options(cls)) - cls.get_system_roles()


class Credentials(EmbeddedDocument):
    meta = {"strict": False}
    key = StringField(required=True)
    secret = StringField(required=True)
    label = StringField()
    created = DateTimeField()
    last_used = DateTimeField()
    last_used_from = StringField()


class User(DbModelMixin, AuthDocument):
    meta = {"db_alias": Database.auth, "strict": strict}

    id = StringField(primary_key=True)
    name = StringField()

    created = DateTimeField()
    """ User auth entry creation time """

    validated = DateTimeField()
    """ Last validation (login) time """

    role = StringField(required=True, choices=get_options(Role), default=Role.user)
    """ User role """

    company = StringField(required=True)
    """ Company this user belongs to """

    credentials = EmbeddedDocumentListField(Credentials, default=list)
    """ Credentials generated for this user """

    email = EmailField(unique=True, sparse=True)
    """ Email uniquely identifying the user """

    autocreated = BooleanField(default=False)
    """ Set to true if the user was auto created based on config settings"""
