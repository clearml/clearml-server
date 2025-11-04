from enum import StrEnum

from mongoengine import StringField, DateTimeField, IntField, EnumField

from apiserver.database import Database, strict
from apiserver.database.model import AttributedDocument


class StorageType(StrEnum):
    fileserver = "fileserver"
    s3 = "s3"
    azure = "azure"
    gs = "gs"
    unknown = "unknown"


class FileType(StrEnum):
    file = "file"
    folder = "folder"


class DeletionStatus(StrEnum):
    created = "created"
    retrying = "retrying"
    failed = "failed"


class UrlToDelete(AttributedDocument):
    _field_collation_overrides = {
        "url": AttributedDocument._numeric_locale,
    }

    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": [
            ("company", "user", "task"),
            ("company", "storage_type", "url"),
            ("status", "retry_count", "storage_type"),
        ],
    }

    id = StringField(primary_key=True)
    url = StringField(required=True, unique_with="company")
    task = StringField(required=True)
    created = DateTimeField(required=True)
    storage_type = EnumField(StorageType, default=StorageType.unknown)
    type = EnumField(FileType, default=FileType.file)
    retry_count = IntField(default=0)
    last_failure_time = DateTimeField()
    last_failure_reason = StringField()
    status = EnumField(DeletionStatus, default=DeletionStatus.created)
