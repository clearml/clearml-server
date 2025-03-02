from mongoengine import (
    Document,
    EmbeddedDocument,
    StringField,
    DateTimeField,
    EmbeddedDocumentListField,
    EmbeddedDocumentField,
    BooleanField,
)

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin
from apiserver.database.model.base import ProperDictMixin

class AWSBucketSettings(EmbeddedDocument, ProperDictMixin):
    bucket = StringField()
    subdir = StringField()
    host = StringField()
    key = StringField()
    secret = StringField()
    token = StringField()
    multipart = BooleanField()
    acl = StringField()
    secure = BooleanField()
    region = StringField()
    verify = BooleanField()
    use_credentials_chain = BooleanField()


class AWSSettings(EmbeddedDocument, DbModelMixin):
    key = StringField()
    secret = StringField()
    region = StringField()
    token = StringField()
    use_credentials_chain = BooleanField()
    buckets = EmbeddedDocumentListField(AWSBucketSettings)


class GoogleBucketSettings(EmbeddedDocument, ProperDictMixin):
    bucket = StringField()
    subdir = StringField()
    project = StringField()
    credentials_json = StringField()


class GoogleStorageSettings(EmbeddedDocument, DbModelMixin):
    project = StringField()
    credentials_json = StringField()
    buckets = EmbeddedDocumentListField(GoogleBucketSettings)


class AzureStorageContainerSettings(EmbeddedDocument, ProperDictMixin):
    account_name = StringField(required=True)
    account_key = StringField(required=True)
    container_name = StringField()


class AzureStorageSettings(EmbeddedDocument, DbModelMixin):
    containers = EmbeddedDocumentListField(AzureStorageContainerSettings)


class StorageSettings(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": [
            "company"
        ],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True, unique=True)
    last_update = DateTimeField()
    aws: AWSSettings = EmbeddedDocumentField(AWSSettings)
    google: GoogleStorageSettings = EmbeddedDocumentField(GoogleStorageSettings)
    azure: AzureStorageSettings = EmbeddedDocumentField(AzureStorageSettings)
