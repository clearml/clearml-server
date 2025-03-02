from mongoengine import Document, StringField, DynamicField, DateTimeField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin
from apiserver.database.model.base import GetMixin
from apiserver.database.model.company import Company


class User(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
    }
    get_all_query_options = GetMixin.QueryParameterOptions(list_fields=("id",))

    id = StringField(primary_key=True)
    company = StringField(required=True, reference_field=Company)
    name = StringField(required=True, user_set_allowed=True)
    family_name = StringField(user_set_allowed=True)
    given_name = StringField(user_set_allowed=True)
    avatar = StringField()
    preferences = DynamicField(default="", exclude_by_default=True)
    created_in_version = StringField()
    created = DateTimeField()
