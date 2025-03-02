import json
import os
import tempfile
from copy import copy
from datetime import datetime
from typing import Optional, Sequence

import attr
from boltons.cacheutils import cachedproperty
from clearml.backend_config.bucket_config import (
    S3BucketConfigurations,
    AzureContainerConfigurations,
    GSBucketConfigurations,
    AzureContainerConfig,
    GSBucketConfig,
    S3BucketConfig,
)

from apiserver.apierrors import errors
from apiserver.apimodels.storage import SetSettingsRequest
from apiserver.config_repo import config
from apiserver.database.model.storage_settings import (
    StorageSettings,
    GoogleBucketSettings,
    AWSSettings,
    AzureStorageSettings,
    GoogleStorageSettings,
)
from apiserver.database.utils import id as db_id

log = config.logger(__file__)


class StorageBLL:
    default_aws_configs: S3BucketConfigurations = None
    conf = config.get("services.storage_credentials")

    @cachedproperty
    def _default_aws_configs(self) -> S3BucketConfigurations:
        return S3BucketConfigurations.from_config(self.conf.get("aws.s3"))

    @cachedproperty
    def _default_azure_configs(self) -> AzureContainerConfigurations:
        return AzureContainerConfigurations.from_config(self.conf.get("azure.storage"))

    @cachedproperty
    def _default_gs_configs(self) -> GSBucketConfigurations:
        return GSBucketConfigurations.from_config(self.conf.get("google.storage"))

    def get_azure_settings_for_company(
        self,
        company_id: str,
        db_settings: StorageSettings = None,
        query_db: bool = True,
    ) -> AzureContainerConfigurations:
        if not db_settings and query_db:
            db_settings = (
                StorageSettings.objects(company=company_id).only("azure").first()
            )

        if not db_settings or not db_settings.azure:
            return copy(self._default_azure_configs)

        azure = db_settings.azure
        return AzureContainerConfigurations(
            container_configs=[
                AzureContainerConfig(**entry.to_proper_dict())
                for entry in (azure.containers or [])
            ]
        )

    def get_gs_settings_for_company(
        self,
        company_id: str,
        db_settings: StorageSettings = None,
        query_db: bool = True,
        json_string: bool = False,
    ) -> GSBucketConfigurations:
        if not db_settings and query_db:
            db_settings = (
                StorageSettings.objects(company=company_id).only("google").first()
            )

        if not db_settings or not db_settings.google:
            if not json_string:
                return copy(self._default_gs_configs)

            if self._default_gs_configs._buckets:
                buckets = [
                    attr.evolve(
                        b,
                        credentials_json=self._assure_json_string(b.credentials_json),
                    )
                    for b in self._default_gs_configs._buckets
                ]
            else:
                buckets = self._default_gs_configs._buckets

            return GSBucketConfigurations(
                buckets=buckets,
                default_project=self._default_gs_configs._default_project,
                default_credentials=self._assure_json_string(
                    self._default_gs_configs._default_credentials
                ),
            )

        def get_bucket_config(bc: GoogleBucketSettings) -> GSBucketConfig:
            data = bc.to_proper_dict()
            if not json_string and bc.credentials_json:
                data["credentials_json"] = self._assure_json_file(bc.credentials_json)
            return GSBucketConfig(**data)

        google = db_settings.google
        buckets_configs = [get_bucket_config(b) for b in (google.buckets or [])]
        return GSBucketConfigurations(
            buckets=buckets_configs,
            default_project=google.project,
            default_credentials=google.credentials_json
            if json_string
            else self._assure_json_file(google.credentials_json),
        )

    def get_aws_settings_for_company(
        self,
        company_id: str,
        db_settings: StorageSettings = None,
        query_db: bool = True,
    ) -> S3BucketConfigurations:
        if not db_settings and query_db:
            db_settings = (
                StorageSettings.objects(company=company_id).only("aws").first()
            )
        if not db_settings or not db_settings.aws:
            return copy(self._default_aws_configs)

        aws = db_settings.aws
        buckets_configs = S3BucketConfig.from_list(
            [b.to_proper_dict() for b in (aws.buckets or [])]
        )
        return S3BucketConfigurations(
            buckets=buckets_configs,
            default_key=aws.key,
            default_secret=aws.secret,
            default_region=aws.region,
            default_use_credentials_chain=aws.use_credentials_chain,
            default_token=aws.token,
            default_extra_args={},
        )

    def _assure_json_file(self, name_or_content: str) -> str:
        if not name_or_content:
            return name_or_content

        if name_or_content.endswith(".json") or os.path.exists(name_or_content):
            return name_or_content

        try:
            json.loads(name_or_content)
        except Exception:
            return name_or_content

        with tempfile.NamedTemporaryFile(
            mode="wt", delete=False, suffix=".json"
        ) as tmp:
            tmp.write(name_or_content)

        return tmp.name

    def _assure_json_string(self, name_or_content: str) -> Optional[str]:
        if not name_or_content:
            return name_or_content

        try:
            json.loads(name_or_content)
            return name_or_content
        except Exception:
            pass

        try:
            with open(name_or_content) as fp:
                return fp.read()
        except Exception:
            return None

    def get_company_settings(self, company_id: str) -> dict:
        db_settings = StorageSettings.objects(company=company_id).first()
        aws = self.get_aws_settings_for_company(company_id, db_settings, query_db=False)
        aws_dict = {
            "key": aws._default_key,
            "secret": aws._default_secret,
            "token": aws._default_token,
            "region": aws._default_region,
            "use_credentials_chain": aws._default_use_credentials_chain,
            "buckets": [attr.asdict(b) for b in aws._buckets],
        }

        gs = self.get_gs_settings_for_company(
            company_id, db_settings, query_db=False, json_string=True
        )
        gs_dict = {
            "project": gs._default_project,
            "credentials_json": gs._default_credentials,
            "buckets": [attr.asdict(b) for b in gs._buckets],
        }

        azure = self.get_azure_settings_for_company(company_id, db_settings)
        azure_dict = {
            "containers": [attr.asdict(ac) for ac in azure._container_configs],
        }

        return {
            "aws": aws_dict,
            "google": gs_dict,
            "azure": azure_dict,
            "last_update": db_settings.last_update if db_settings else None,
        }

    def set_company_settings(
        self, company_id: str, settings: SetSettingsRequest
    ) -> int:
        update_dict = {}
        if settings.aws:
            update_dict["aws"] = {
                **{
                    k: v
                    for k, v in settings.aws.to_struct().items()
                    if k in AWSSettings.get_fields()
                }
            }

        if settings.azure:
            update_dict["azure"] = {
                **{
                    k: v
                    for k, v in settings.azure.to_struct().items()
                    if k in AzureStorageSettings.get_fields()
                }
            }

        if settings.google:
            update_dict["google"] = {
                **{
                    k: v
                    for k, v in settings.google.to_struct().items()
                    if k in GoogleStorageSettings.get_fields()
                }
            }
            cred_json = update_dict["google"].get("credentials_json")
            if cred_json:
                try:
                    json.loads(cred_json)
                except Exception as ex:
                    raise errors.bad_request.ValidationError(
                        f"Invalid json credentials: {str(ex)}"
                    )

        if not update_dict:
            raise errors.bad_request.ValidationError("No settings were provided")

        settings = StorageSettings.objects(company=company_id).only("id").first()
        settings_id = settings.id if settings else db_id()
        return StorageSettings.objects(id=settings_id).update(
            upsert=True,
            id=settings_id,
            company=company_id,
            last_update=datetime.utcnow(),
            **update_dict,
        )

    def reset_company_settings(self, company_id: str, keys: Sequence[str]) -> int:
        return StorageSettings.objects(company=company_id).update(
            last_update=datetime.utcnow(), **{f"unset__{k}": 1 for k in keys}
        )
