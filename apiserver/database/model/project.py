from mongoengine import StringField, DateTimeField, IntField, ListField

from apiserver.database import Database, strict
from apiserver.database.fields import StrippedStringField, SafeSortedListField
from apiserver.database.model import AttributedDocument
from apiserver.database.model.base import GetMixin


class Project(AttributedDocument):
    min_name_length = 3

    get_all_query_options = GetMixin.QueryParameterOptions(
        pattern_fields=("name", "basename", "description"),
        list_fields=("tags", "system_tags", "id", "parent", "path"),
        range_fields=("last_update",),
    )

    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": [
            "parent",
            "path",
            ("company", "name"),
            ("company", "basename"),
            ("company", "parent", "system_tags", "featured", "last_update"),
            {
                "name": "%s.project.main_text_index" % Database.backend,
                "fields": ["$name", "$id", "$description"],
                "default_language": "english",
                "weights": {"name": 10, "id": 10, "description": 10},
            },
        ],
    }

    id = StringField(primary_key=True)
    name = StrippedStringField(
        required=True,
        unique_with=AttributedDocument.company.name,
        min_length=min_name_length,
        sparse=True,
    )
    basename = StrippedStringField(required=True)
    description = StringField()
    created = DateTimeField(required=True)
    tags = SafeSortedListField(StringField(required=True))
    system_tags = SafeSortedListField(StringField(required=True))
    default_output_destination = StrippedStringField()
    last_update = DateTimeField()
    featured = IntField(default=9999)
    logo_url = StringField()
    logo_blob = StringField(exclude_by_default=True)
    company_origin = StringField(exclude_by_default=True)
    parent = StringField(reference_field="Project")
    path = ListField(StringField(required=True), exclude_by_default=True)
