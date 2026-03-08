import os
from abc import ABC, ABCMeta, abstractmethod
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
from itertools import chain
from pathlib import Path
from time import sleep
from typing import Sequence, Optional, Tuple, Mapping, TypeVar, Hashable, Generic
from urllib.parse import urlparse

import boto3
import requests
from azure.storage.blob import ContainerClient, PartialBatchErrorException
from boltons.iterutils import bucketize, chunked_iter
from furl import furl
from google.cloud import storage as google_storage
from mongoengine import Q
from mypy_boto3_s3.service_resource import Bucket as AWSBucket

from apiserver.bll.auth import AuthBLL
from apiserver.bll.storage import StorageBLL
from apiserver.config.info import get_default_company
from apiserver.config_repo import config
from apiserver.database import db
from apiserver.database.model.url_to_delete import UrlToDelete, StorageType, DeletionStatus

log = config.logger(f"JOB-{Path(__file__).name}")
conf = config.get("services.async_urls_delete")
max_retries = conf.get("max_retries", 3)
retry_timeout = timedelta(seconds=conf.get("retry_timeout_sec", 60))
storage_bll = StorageBLL()


def mark_retry_failed(ids: Sequence[str], reason: str):
    UrlToDelete.objects(id__in=ids).update(
        last_failure_time=datetime.utcnow(),
        last_failure_reason=reason,
        inc__retry_count=1,
    )
    UrlToDelete.objects(id__in=ids, retry_count__gte=max_retries).update(
        status=DeletionStatus.failed
    )


def mark_failed(query: Q, reason: str):
    UrlToDelete.objects(query).update(
        status=DeletionStatus.failed,
        last_failure_time=datetime.utcnow(),
        last_failure_reason=reason,
    )


def scheme_prefix(scheme: str) -> str:
    return str(furl(scheme=scheme, netloc=""))


T = TypeVar("T", bound=Hashable)


class Storage(Generic[T], metaclass=ABCMeta):
    class Client(ABC):
        @property
        @abstractmethod
        def chunk_size(self) -> int:
            pass

        def get_path(self, url: UrlToDelete) -> str:
            pass

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    def group_urls(
        self, urls: Sequence[UrlToDelete]
    ) -> Mapping[T, Sequence[UrlToDelete]]:
        pass

    def get_client(self, base: T, urls: Sequence[UrlToDelete]) -> Client:
        pass


def delete_urls(urls_query: Q, storage: Storage):
    to_delete = list(UrlToDelete.objects(urls_query).order_by("url").limit(10000))
    if not to_delete:
        return

    grouped_urls = storage.group_urls(to_delete)
    for base, urls in grouped_urls.items():
        if not base:
            msg = f"Invalid {storage.name} url or missing {storage.name} configuration for account"
            mark_failed(
                Q(id__in=[url.id for url in urls]), msg,
            )
            log.warning(
                f"Failed to delete {len(urls)} files from {storage.name} due to: {msg}"
            )
            continue

        try:
            client = storage.get_client(base, urls)
        except Exception as ex:
            failed = [url.id for url in urls]
            mark_retry_failed(failed, reason=str(ex))
            log.warning(
                f"Failed to delete {len(failed)} files from {storage.name} due to: {str(ex)}"
            )
            continue

        for chunk in chunked_iter(urls, client.chunk_size):
            paths = []
            path_to_id_mapping = defaultdict(list)
            ids_to_delete = set()
            for url in chunk:
                try:
                    path = client.get_path(url)
                except Exception as ex:
                    err = str(ex)
                    mark_failed(Q(id=url.id), err)
                    log.warning(f"Error getting path for {url.url}: {err}")
                    continue

                paths.append(path)
                path_to_id_mapping[path].append(url.id)
                ids_to_delete.add(url.id)

            if not paths:
                continue

            try:
                deleted_paths, errors = client.delete_many(paths)
            except Exception as ex:
                mark_retry_failed([url.id for url in urls], str(ex))
                log.warning(
                    f"Error deleting {len(paths)} files from {storage.name}: {str(ex)}"
                )
                continue

            failed_ids = set()
            for reason, err_paths in errors.items():
                error_ids = set(
                    chain.from_iterable(
                        path_to_id_mapping.get(p, []) for p in err_paths
                    )
                )
                mark_retry_failed(list(error_ids), reason)
                log.warning(
                    f"Failed to delete {len(error_ids)} files from {storage.name} storage due to: {reason}"
                )
                failed_ids.update(error_ids)

            deleted_ids = set(
                chain.from_iterable(
                    path_to_id_mapping.get(p, []) for p in deleted_paths
                )
            )
            if deleted_ids:
                UrlToDelete.objects(id__in=list(deleted_ids)).delete()
                log.info(
                    f"{len(deleted_ids)} files deleted from {storage.name} storage"
                )

            missing_ids = ids_to_delete - deleted_ids - failed_ids
            if missing_ids:
                mark_retry_failed(list(missing_ids), "Not succeeded")


class FileserverStorage(Storage):
    class Client(Storage.Client):
        timeout = conf.get("fileserver.timeout_sec", 300)

        def __init__(self, session: requests.Session, host: str):
            self.session = session
            self.delete_url = furl(host).add(path="delete_many").url

        @property
        def chunk_size(self) -> int:
            return 10000

        def get_path(self, url: UrlToDelete) -> str:
            path = url.url.strip("/")
            if not path:
                raise ValueError("Empty path")

            return path

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            res = self.session.post(
                url=self.delete_url, json={"files": list(paths)}, timeout=self.timeout
            )
            res.raise_for_status()
            res_data = res.json()
            return list(res_data.get("deleted", {})), res_data.get("errors", {})

    token_expiration_sec = conf.get("fileserver.token_expiration_sec", 600)

    def __init__(self, company: str, fileserver_host: str = None):
        fileserver_host = fileserver_host or config.get("hosts.fileserver", None)
        self.host = fileserver_host.rstrip("/")
        if not self.host:
            log.warning("Fileserver host not configured")

        def _parse_url_prefix(prefix) -> Tuple[str, str]:
            url = furl(prefix)
            host = f"{url.scheme}://{url.netloc}" if url.scheme else None
            return host, str(url.path).rstrip("/")

        url_prefixes = [
            _parse_url_prefix(p) for p in conf.get("fileserver.url_prefixes", [])
        ]
        if not any(self.host == host for host, _ in url_prefixes):
            url_prefixes.append((self.host, ""))
        self.url_prefixes = url_prefixes

        self.company = company

    @property
    def name(self) -> str:
        return "Fileserver"

    def _resolve_base_url(self, url: UrlToDelete) -> Optional[str]:
        """
        For the url return the base_url containing schema, optional host and bucket name
        """
        if not url.url:
            return None

        try:
            parsed = furl(url.url)
            url_host = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else None
            url_path = str(parsed.path)
        except Exception:
            return None

        for host, path_prefix in self.url_prefixes:
            if host and url_host != host:
                continue
            if path_prefix and not url_path.startswith(path_prefix + "/"):
                continue
            url.url = url_path[len(path_prefix or "") :]
            return self.host

    def group_urls(
        self, urls: Sequence[UrlToDelete]
    ) -> Mapping[str, Sequence[UrlToDelete]]:
        return bucketize(urls, key=self._resolve_base_url)

    def get_client(self, base: str, urls: Sequence[UrlToDelete]) -> Client:
        host = base
        token = AuthBLL.get_token_for_user(
            user_id="__apiserver__",
            company_id=get_default_company(),
            expiration_sec=self.token_expiration_sec,
        ).token
        session = requests.session()
        session.headers.update({"Authorization": "Bearer {}".format(token)})
        res = session.get(url=host, timeout=self.Client.timeout)
        res.raise_for_status()

        return self.Client(session, host)


class AzureStorage(Storage):
    class Client(Storage.Client):
        def __init__(self, container: ContainerClient):
            self.container = container

        @property
        def chunk_size(self) -> int:
            return 256

        def get_path(self, url: UrlToDelete) -> str:
            parsed = furl(url.url)
            if (
                not parsed.path
                or not parsed.path.segments
                or len(parsed.path.segments) <= 1
            ):
                raise ValueError("No path found following container name")

            # noinspection PyTypeChecker
            return os.path.join(*parsed.path.segments[1:])

        @staticmethod
        def _path_from_request_url(request_url: str) -> str:
            try:
                return furl(request_url).path.segments[-1]
            except Exception:
                return request_url

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            try:
                res = self.container.delete_blobs(*paths)
            except PartialBatchErrorException as pex:
                deleted = []
                errors = defaultdict(list)
                for part in pex.parts:
                    if 300 >= part.status_code >= 200:
                        deleted.append(self._path_from_request_url(part.request.url))
                    else:
                        errors[part.reason].append(
                            self._path_from_request_url(part.request.url)
                        )
                return deleted, errors

            return [self._path_from_request_url(part.request.url) for part in res], {}

    def __init__(self, company: str):
        self.configs = storage_bll.get_azure_settings_for_company(company)
        self.scheme = "azure"

    @property
    def name(self) -> str:
        return "Azure"

    def _resolve_base_url(self, url: UrlToDelete) -> Optional[Tuple]:
        """
        For the url return the base_url containing schema, optional host and bucket name
        """
        try:
            parsed = urlparse(url.url)
            if parsed.scheme != self.scheme:
                return None

            azure_conf = self.configs.get_config_by_uri(url.url)
            if azure_conf is None:
                return None

            account_url = parsed.netloc
            return account_url, azure_conf.container_name
        except Exception as ex:
            log.warning(f"Error resolving base url for {url.url}: " + str(ex))
            return None

    def group_urls(
        self, urls: Sequence[UrlToDelete]
    ) -> Mapping[Tuple, Sequence[UrlToDelete]]:
        return bucketize(urls, key=self._resolve_base_url)

    def get_client(self, base: Tuple, urls: Sequence[UrlToDelete]) -> Client:
        account_url, container_name = base
        sample_url = urls[0].url
        cfg = self.configs.get_config_by_uri(sample_url)
        if not cfg or not cfg.account_name or not cfg.account_key:
            raise ValueError(
                f"Missing account name or key for Azure Blob Storage "
                f"account: {account_url}, container: {container_name}"
            )

        return self.Client(
            ContainerClient(
                account_url=account_url,
                container_name=cfg.container_name,
                credential={
                    "account_name": cfg.account_name,
                    "account_key": cfg.account_key,
                },
            )
        )


class AWSStorage(Storage):
    class Client(Storage.Client):
        def __init__(self, base_url: str, container: AWSBucket):
            self.container = container
            self.base_url = base_url

        @property
        def chunk_size(self) -> int:
            return 1000

        def get_path(self, url: UrlToDelete) -> str:
            """ Normalize remote path. Remove any prefix that is already handled by the container """
            path = url.url
            if path.startswith(self.base_url):
                path = path[len(self.base_url) :]
            path = path.lstrip("/")
            return path

        @staticmethod
        def _path_from_request_url(request_url: str) -> str:
            try:
                return furl(request_url).path.segments[-1]
            except Exception:
                return request_url

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            res = self.container.delete_objects(
                Delete={"Objects": [{"Key": p} for p in paths]}
            )
            errors = defaultdict(list)
            for err in res.get("Errors", []):
                msg = err.get("Message", "")
                errors[msg].append(err.get("Key"))

            return [d.get("Key") for d in res.get("Deleted", [])], errors

    def __init__(self, company: str):
        self.configs = storage_bll.get_aws_settings_for_company(company)
        self.scheme = "s3"

    @property
    def name(self) -> str:
        return "AWS"

    def _resolve_base_url(self, url: UrlToDelete) -> Optional[str]:
        """
        For the url return the base_url containing schema, optional host and bucket name
        """
        try:
            parsed = urlparse(url.url)
            if parsed.scheme != self.scheme:
                return None

            s3_conf = self.configs.get_config_by_uri(url.url)
            if s3_conf is None:
                return None

            s3_bucket = s3_conf.bucket
            if not s3_bucket:
                parts = Path(parsed.path.strip("/")).parts
                if parts:
                    s3_bucket = parts[0]
            return "/".join(filter(None, ("s3:/", s3_conf.host, s3_bucket)))
        except Exception as ex:
            log.warning(f"Error resolving base url for {url.url}: " + str(ex))
            return None

    def group_urls(
        self, urls: Sequence[UrlToDelete]
    ) -> Mapping[str, Sequence[UrlToDelete]]:
        return bucketize(urls, key=self._resolve_base_url)

    def get_client(self, base: str, urls: Sequence[UrlToDelete]) -> Client:
        sample_url = urls[0].url
        cfg = self.configs.get_config_by_uri(sample_url)
        boto_kwargs = {
            "endpoint_url": (("https://" if cfg.secure else "http://") + cfg.host)
            if cfg.host
            else None,
            "use_ssl": cfg.secure,
            "verify": cfg.verify,
            "region_name": cfg.region or None,
        }
        name = base[len(scheme_prefix(self.scheme)) :]
        bucket_name = name[len(cfg.host) + 1 :] if cfg.host else name
        if not cfg.use_credentials_chain:
            if not cfg.key or not cfg.secret:
                raise ValueError(
                    f"Missing key or secret for AWS S3 host: {cfg.host}, bucket: {str(bucket_name)}"
                )

            boto_kwargs["aws_access_key_id"] = cfg.key
            boto_kwargs["aws_secret_access_key"] = cfg.secret
            if cfg.token:
                boto_kwargs["aws_session_token"] = cfg.token

        return self.Client(
            base, boto3.resource("s3", **boto_kwargs).Bucket(bucket_name)
        )


class GoogleCloudStorage(Storage):
    class Client(Storage.Client):
        def __init__(self, base_url: str, container: google_storage.Bucket):
            self.container = container
            self.base_url = base_url

        @property
        def chunk_size(self) -> int:
            return 100

        def get_path(self, url: UrlToDelete) -> str:
            """ Normalize remote path. Remove any prefix that is already handled by the container """
            path = url.url
            if path.startswith(self.base_url):
                path = path[len(self.base_url) :]
            path = path.lstrip("/")
            return path

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            not_found = set()

            def error_callback(blob: google_storage.Blob):
                not_found.add(blob.name)

            self.container.delete_blobs(
                [self.container.blob(p) for p in paths], on_error=error_callback,
            )
            errors = {"Not found": list(not_found)} if not_found else {}
            return list(set(paths) - not_found), errors

    def __init__(self, company: str):
        self.configs = storage_bll.get_gs_settings_for_company(company)
        self.scheme = "gs"

    @property
    def name(self) -> str:
        return "Google Storage"

    def _resolve_base_url(self, url: UrlToDelete) -> Optional[str]:
        """
        For the url return the base_url containing schema, optional host and bucket name
        """
        try:
            parsed = urlparse(url.url)
            if parsed.scheme != self.scheme:
                return None

            gs_conf = self.configs.get_config_by_uri(url.url)
            if gs_conf is None:
                return None

            return str(furl(scheme=parsed.scheme, netloc=gs_conf.bucket))
        except Exception as ex:
            log.warning(f"Error resolving base url for {url.url}: " + str(ex))
            return None

    def group_urls(
        self, urls: Sequence[UrlToDelete]
    ) -> Mapping[str, Sequence[UrlToDelete]]:
        return bucketize(urls, key=self._resolve_base_url)

    def get_client(self, base: str, urls: Sequence[UrlToDelete]) -> Client:
        sample_url = urls[0].url
        cfg = self.configs.get_config_by_uri(sample_url)
        if cfg.credentials_json:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                cfg.credentials_json
            )
        else:
            credentials = None

        bucket_name = base[len(scheme_prefix(self.scheme)) :]
        return self.Client(
            base,
            google_storage.Client(project=cfg.project, credentials=credentials).bucket(
                bucket_name
            ),
        )


def run_delete_loop(fileserver_host: str):
    storage_helpers = {
        StorageType.fileserver: partial(
            FileserverStorage, fileserver_host=fileserver_host
        ),
        StorageType.s3: AWSStorage,
        StorageType.azure: AzureStorage,
        StorageType.gs: GoogleCloudStorage,
    }
    while True:
        now = datetime.utcnow()
        urls_query = (
            Q(status__ne=DeletionStatus.failed)
            & Q(retry_count__lt=max_retries)
            & (
                Q(last_failure_time__exists=False)
                | Q(last_failure_time__lt=now - retry_timeout)
            )
        )

        url_to_delete: UrlToDelete = UrlToDelete.objects(
            urls_query & Q(storage_type__in=list(storage_helpers))
        ).order_by("retry_count").limit(1).first()
        if not url_to_delete:
            sleep(10)
            continue

        company = url_to_delete.company
        user = url_to_delete.user
        storage_type = url_to_delete.storage_type
        log.info(
            f"Deleting {storage_type} objects for company: {company}, user: {user}"
        )
        company_storage_urls_query = urls_query & Q(
            company=company, storage_type=storage_type,
        )
        delete_urls(
            urls_query=company_storage_urls_query,
            storage=storage_helpers[storage_type](company=company),
        )


def main():
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--fileserver-host", "-fh", help="Fileserver host address", type=str,
    )
    args = parser.parse_args()

    db.initialize()
    run_delete_loop(args.fileserver_host)


if __name__ == "__main__":
    main()
