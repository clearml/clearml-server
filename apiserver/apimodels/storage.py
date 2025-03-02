from jsonmodels.fields import StringField, BoolField, ListField, EmbeddedField
from jsonmodels.models import Base
from jsonmodels.validators import Enum


class AWSBucketSettings(Base):
    bucket = StringField()
    subdir = StringField()
    host = StringField()
    key = StringField()
    secret = StringField()
    token = StringField()
    multipart = BoolField(default=True)
    acl = StringField()
    secure = BoolField(default=True)
    region = StringField()
    verify = BoolField(default=True)
    use_credentials_chain = BoolField(default=False)


class AWSSettings(Base):
    key = StringField()
    secret = StringField()
    region = StringField()
    token = StringField()
    use_credentials_chain = BoolField(default=False)
    buckets = ListField(items_types=[AWSBucketSettings])


class GoogleBucketSettings(Base):
    bucket = StringField()
    subdir = StringField()
    project = StringField()
    credentials_json = StringField()


class GoogleSettings(Base):
    project = StringField()
    credentials_json = StringField()
    buckets = ListField(items_types=[GoogleBucketSettings])


class AzureContainerSettings(Base):
    account_name = StringField()
    account_key = StringField()
    container_name = StringField()


class AzureSettings(Base):
    containers = ListField(items_types=[AzureContainerSettings])


class SetSettingsRequest(Base):
    aws = EmbeddedField(AWSSettings)
    google = EmbeddedField(GoogleSettings)
    azure = EmbeddedField(AzureSettings)


class ResetSettingsRequest(Base):
    keys = ListField([str], item_validators=[Enum("aws", "google", "azure")])
