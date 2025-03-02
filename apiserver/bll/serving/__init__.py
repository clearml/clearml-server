from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from operator import attrgetter
from time import time
from typing import Optional, Sequence, Union

import attr
from boltons.iterutils import chunked_iter, bucketize
from pyhocon import ConfigTree

from apiserver.apimodels.serving import (
    ServingContainerEntry,
    RegisterRequest,
    StatusReportRequest,
)
from apiserver.apimodels.workers import MachineStats
from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.redis_manager import redman
from .stats import ServingStats


log = config.logger(__file__)


class ServingBLL:
    def __init__(self, redis=None):
        self.conf = config.get("services.serving", ConfigTree())
        self.redis = redis or redman.connection("workers")

    @staticmethod
    def _get_url_key(company: str, url: str):
        return f"serving_url_{company}_{url}"

    @staticmethod
    def _get_container_key(company: str, container_id: str) -> str:
        """Build redis key from company and container_id"""
        return f"serving_container_{company}_{container_id}"

    def _save_serving_container_entry(self, entry: ServingContainerEntry):
        self.redis.setex(
            entry.key, timedelta(seconds=entry.register_timeout), entry.to_json()
        )

        url_key = self._get_url_key(entry.company_id, entry.endpoint_url)
        expiration = int(time()) + entry.register_timeout
        container_item = {entry.key: expiration}
        self.redis.zadd(url_key, container_item)
        # make sure that url set will not get stuck in redis
        # indefinitely in case no more containers report to it
        self.redis.expire(url_key, max(3600, entry.register_timeout))

    def _get_serving_container_entry(
        self, company_id: str, container_id: str
    ) -> Optional[ServingContainerEntry]:
        """
        Get a container entry for the provided container ID.
        """
        key = self._get_container_key(company_id, container_id)
        data = self.redis.get(key)
        if not data:
            return

        try:
            entry = ServingContainerEntry.from_json(data)
            return entry
        except Exception as e:
            msg = "Failed parsing container entry"
            log.exception(f"{msg}: {str(e)}")

    def register_serving_container(
        self,
        company_id: str,
        request: RegisterRequest,
        ip: str = "",
    ) -> ServingContainerEntry:
        """
        Register a serving container
        """
        now = datetime.now(timezone.utc)
        key = self._get_container_key(company_id, request.container_id)
        entry = ServingContainerEntry(
            **request.to_struct(),
            key=key,
            company_id=company_id,
            ip=ip,
            register_time=now,
            register_timeout=request.timeout,
            last_activity_time=now,
        )
        self._save_serving_container_entry(entry)
        return entry

    def unregister_serving_container(
        self,
        company_id: str,
        container_id: str,
    ) -> None:
        """
        Unregister a serving container
        """
        entry = self._get_serving_container_entry(company_id, container_id)
        if entry:
            url_key = self._get_url_key(entry.company_id, entry.endpoint_url)
            self.redis.zrem(url_key, entry.key)

        key = self._get_container_key(company_id, container_id)
        res = self.redis.delete(key)
        if res:
            return

        if not self.conf.get("container_auto_unregister", True):
            raise errors.bad_request.ContainerNotRegistered(container=container_id)

    def container_status_report(
        self,
        company_id: str,
        report: StatusReportRequest,
        ip: str = "",
    ) -> None:
        """
        Serving container status report
        """
        container_id = report.container_id
        now = datetime.now(timezone.utc)
        entry = self._get_serving_container_entry(company_id, container_id)
        if entry:
            ip = ip or entry.ip
            register_time = entry.register_time
            register_timeout = entry.register_timeout
        else:
            if not self.conf.get("container_auto_register", True):
                raise errors.bad_request.ContainerNotRegistered(container=container_id)
            ip = ip
            register_time = now
            register_timeout = int(
                self.conf.get("default_container_timeout_sec", 10 * 60)
            )

        key = self._get_container_key(company_id, container_id)
        entry = ServingContainerEntry(
            **report.to_struct(),
            key=key,
            company_id=company_id,
            ip=ip,
            register_time=register_time,
            register_timeout=register_timeout,
            last_activity_time=now,
        )
        self._save_serving_container_entry(entry)
        ServingStats.log_stats_to_es(entry)

    def _get_all(
        self,
        company_id: str,
    ) -> Sequence[ServingContainerEntry]:
        keys = list(self.redis.scan_iter(self._get_container_key(company_id, "*")))
        entries = []
        for keys in chunked_iter(keys, 1000):
            data = self.redis.mget(keys)
            if not data:
                continue
            for d in data:
                try:
                    entries.append(ServingContainerEntry.from_json(d))
                except Exception as ex:
                    log.error(f"Failed parsing container entry {str(ex)}")

        return entries

    @attr.s(auto_attribs=True)
    class Counter:
        class AggType(Enum):
            avg = auto()
            max = auto()
            total = auto()
            count = auto()

        name: str
        field: str
        agg_type: AggType
        float_precision: int = None

        _max: Union[int, float, datetime] = attr.field(init=False, default=None)
        _total: Union[int, float] = attr.field(init=False, default=0)
        _count: int = attr.field(init=False, default=0)

        def add(self, entry: ServingContainerEntry):
            value = getattr(entry, self.field, None)
            if value is None:
                return

            self._count += 1
            if self.agg_type == self.AggType.max:
                self._max = value if self._max is None else max(self._max, value)
            else:
                self._total += value

        def __call__(self):
            if self.agg_type == self.AggType.count:
                return self._count

            if self.agg_type == self.AggType.max:
                return self._max

            if self.agg_type == self.AggType.total:
                return self._total

            if not self._count:
                return None
            avg = self._total / self._count
            return (
                round(avg, self.float_precision) if self.float_precision else round(avg)
            )

    def _get_summary(self, entries: Sequence[ServingContainerEntry]) -> dict:
        counters = [
            self.Counter(
                name="uptime_sec",
                field="uptime_sec",
                agg_type=self.Counter.AggType.max,
            ),
            self.Counter(
                name="requests",
                field="requests_num",
                agg_type=self.Counter.AggType.total,
            ),
            self.Counter(
                name="requests_min",
                field="requests_min",
                agg_type=self.Counter.AggType.avg,
                float_precision=2,
            ),
            self.Counter(
                name="latency_ms",
                field="latency_ms",
                agg_type=self.Counter.AggType.avg,
            ),
            self.Counter(
                name="last_update",
                field="last_activity_time",
                agg_type=self.Counter.AggType.max,
            ),
        ]
        for entry in entries:
            for counter in counters:
                counter.add(entry)

        first_entry = entries[0]
        ret = {
            "endpoint": first_entry.endpoint_name,
            "model": first_entry.model_name,
            "url": first_entry.endpoint_url,
            "instances": len(entries),
            **{counter.name: counter() for counter in counters},
        }
        ret["last_update"] = ret.get("last_update")
        return ret

    def get_endpoints(self, company_id: str):
        """
        Group instances by urls and return a summary for each url
        Do not return data for "loading" instances that have no url
        """
        entries = self._get_all(company_id)
        by_url = bucketize(entries, key=attrgetter("endpoint_url"))
        by_url.pop(None, None)
        return [self._get_summary(url_entries) for url_entries in by_url.values()]

    def _get_endpoint_entries(
        self, company_id, endpoint_url: Union[str, None]
    ) -> Sequence[ServingContainerEntry]:
        url_key = self._get_url_key(company_id, endpoint_url)
        timestamp = int(time())
        self.redis.zremrangebyscore(url_key, min=0, max=timestamp)
        container_keys = {key.decode() for key in self.redis.zrange(url_key, 0, -1)}
        if not container_keys:
            return []

        entries = []
        found_keys = set()
        data = self.redis.mget(container_keys) or []
        for d in data:
            try:
                entry = ServingContainerEntry.from_json(d)
                if entry.endpoint_url == endpoint_url:
                    entries.append(entry)
                    found_keys.add(entry.key)
            except Exception as ex:
                log.error(f"Failed parsing container entry {str(ex)}")

        missing_keys = container_keys - found_keys
        if missing_keys:
            self.redis.zrem(url_key, *missing_keys)

        return entries

    def get_loading_instances(self, company_id: str):
        entries = self._get_endpoint_entries(company_id, None)
        return [
            {
                "id": entry.container_id,
                "endpoint": entry.endpoint_name,
                "url": entry.endpoint_url,
                "model": entry.model_name,
                "model_source": entry.model_source,
                "model_version": entry.model_version,
                "preprocess_artifact": entry.preprocess_artifact,
                "input_type": entry.input_type,
                "input_size": entry.input_size,
                "uptime_sec": entry.uptime_sec,
                "age_sec": int((datetime.now(timezone.utc) - entry.register_time).total_seconds()),
                "last_update": entry.last_activity_time,
            }
            for entry in entries
        ]

    def get_endpoint_details(self, company_id, endpoint_url: str) -> dict:
        entries = self._get_endpoint_entries(company_id, endpoint_url)
        if not entries:
            raise errors.bad_request.NoContainersForUrl(url=endpoint_url)

        instances = []
        entry: ServingContainerEntry
        for entry in entries:
            instances.append(
                {
                    "endpoint": entry.endpoint_name,
                    "model": entry.model_name,
                    "url": entry.endpoint_url,
                }
            )

        def get_machine_stats_data(machine_stats: MachineStats) -> dict:
            ret = {"cpu_count": 0, "gpu_count": 0}
            if not machine_stats:
                return ret

            for value, field in (
                (machine_stats.cpu_usage, "cpu_count"),
                (machine_stats.gpu_usage, "gpu_count"),
            ):
                if value is None:
                    continue
                ret[field] = len(value) if isinstance(value, (list, tuple)) else 1

            return ret

        first_entry = entries[0]
        return {
            "endpoint": first_entry.endpoint_name,
            "model": first_entry.model_name,
            "url": first_entry.endpoint_url,
            "preprocess_artifact": first_entry.preprocess_artifact,
            "input_type": first_entry.input_type,
            "input_size": first_entry.input_size,
            "model_source": first_entry.model_source,
            "model_version": first_entry.model_version,
            "uptime_sec": max(e.uptime_sec for e in entries),
            "last_update": max(e.last_activity_time for e in entries),
            "instances": [
                {
                    "id": entry.container_id,
                    "uptime_sec": entry.uptime_sec,
                    "requests": entry.requests_num,
                    "requests_min": entry.requests_min,
                    "latency_ms": entry.latency_ms,
                    "last_update": entry.last_activity_time,
                    "reference": [ref.to_struct() for ref in entry.reference]
                    if isinstance(entry.reference, list)
                    else entry.reference,
                    **get_machine_stats_data(entry.machine_stats),
                }
                for entry in entries
            ],
        }
