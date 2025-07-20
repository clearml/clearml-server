from jsonmodels.fields import StringField, BoolField
from jsonmodels.models import Base

from apiserver.apimodels import DictField


class UserRequest(Base):
    user = StringField(required=True)


class CreateRequest(Base):
    id = StringField(required=True)
    name = StringField(required=True)
    company = StringField(required=True)
    family_name = StringField()
    given_name = StringField()
    avatar = StringField()


class SetPreferencesRequest(Base):
    preferences = DictField(required=True)
    return_updated = BoolField(default=True)
