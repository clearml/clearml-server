from enum import auto
from typing import Sequence

from jsonmodels import fields, models
from jsonmodels.validators import Length

from apiserver.apimodels import DictField, ActualEnumField, ScalarField
from apiserver.utilities.stringenum import StringEnum


class Filter(models.Base):
    tags = fields.ListField([str])
    system_tags = fields.ListField([str])


class TagsRequest(models.Base):
    include_system = fields.BoolField(default=False)
    filter = fields.EmbeddedField(Filter)


class EntitiesCountRequest(models.Base):
    projects = DictField()
    tasks = DictField()
    models = DictField()
    pipelines = DictField()
    datasets = DictField()
    reports = DictField()
    active_users = fields.ListField(str)
    search_hidden = fields.BoolField(default=False)
    allow_public = fields.BoolField(default=True)
    limit = fields.IntField()


class EntityType(StringEnum):
    task = auto()
    model = auto()


class ValueMapping(models.Base):
    key = ScalarField(nullable=True)
    value = ScalarField(nullable=True)


class FieldMapping(models.Base):
    field = fields.StringField(required=True)
    name = fields.StringField()
    values: Sequence[ValueMapping] = fields.ListField(items_types=[ValueMapping])


class PrepareDownloadForGetAllRequest(models.Base):
    entity_type = ActualEnumField(EntityType)
    allow_public = fields.BoolField(default=True)
    search_hidden = fields.BoolField(default=False)
    only_fields = fields.ListField(
        items_types=[str], validators=[Length(1)], required=True
    )
    field_mappings: Sequence[FieldMapping] = fields.ListField(
        items_types=[FieldMapping], validators=[Length(1)], required=True
    )


class DownloadForGetAllRequest(models.Base):
    prepare_id = fields.StringField(required=True)
