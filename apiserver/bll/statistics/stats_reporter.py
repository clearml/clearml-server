import logging
import queue
import random
import time
from datetime import timedelta, datetime
from time import sleep
from typing import Sequence, Optional

import dpath
import requests
from requests.adapters import HTTPAdapter, Retry

from apiserver.bll.query import Builder as QueryBuilder
from apiserver.bll.util import get_server_uuid
from apiserver.bll.workers import WorkerStats, WorkerBLL
from apiserver.config_repo import config
from apiserver.config.info import get_deployment_type
from apiserver.database.model import Company, User
from apiserver.database.model.queue import Queue
from apiserver.database.model.task.task import Task
from apiserver.utilities.dicts import nested_get
from apiserver.utilities.json import dumps
from apiserver.version import __version__ as current_version
from .resource_monitor import ResourceMonitor, stat_threads

log = config.logger(__file__)

worker_bll = WorkerBLL()


class StatisticsReporter:
    send_queue = queue.Queue()
    supported = config.get("apiserver.statistics.supported", True)

    @classmethod
    def start(cls):
        if not cls.supported:
            return
        ResourceMonitor.start()
        cls.start_sender()
        cls.start_reporter()

    @classmethod
    @stat_threads.register("reporter", daemon=True)
    def start_reporter(cls):
        """
        Periodically send statistics reports for companies who have opted in.
        Note: in clearml we usually have only a single company
        """
        if not cls.supported:
            return

        report_interval = timedelta(
            hours=config.get("apiserver.statistics.report_interval_hours", 24)
        )
        sleep(report_interval.total_seconds())
        while True:
            try:
                for company in Company.objects(
                    defaults__stats_option__enabled=True
                ).only("id"):
                    stats = cls.get_statistics(company.id)
                    cls.send_queue.put(stats)

            except Exception as ex:
                log.exception(f"Failed collecting stats: {str(ex)}")

            sleep(report_interval.total_seconds())

    @classmethod
    @stat_threads.register("sender", daemon=True)
    def start_sender(cls):
        if not cls.supported:
            return

        url = config.get("apiserver.statistics.url")

        retries = config.get("apiserver.statistics.max_retries", 5)
        max_backoff = config.get("apiserver.statistics.max_backoff_sec", 5)
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(retries))
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers["Content-type"] = "application/json"

        WarningFilter.attach()

        while True:
            try:
                report = cls.send_queue.get()

                # Set a random backoff factor each time we send a report
                adapter.max_retries.backoff_factor = random.random() * max_backoff

                session.post(url, data=dumps(report))

            except Exception as ex:
                log.warning(f"Failed reporting stats: {str(ex)}")

    @classmethod
    def get_statistics(cls, company_id: str) -> dict:
        """
        Returns a statistics report per company
        """
        return {
            "time": datetime.utcnow(),
            "company_id": company_id,
            "server": {
                "version": current_version,
                "deployment": get_deployment_type(),
                "uuid": get_server_uuid(),
                "queues": {"count": Queue.objects(company=company_id).count()},
                "users": {"count": User.objects(company=company_id).count()},
                "resources": ResourceMonitor.get_stats(),
                "experiments": next(
                    iter(cls._get_experiments_stats(company_id).values()), {}
                ),
            },
            "agents": cls._get_agents_statistics(company_id),
        }

    @classmethod
    def _get_agents_statistics(cls, company_id: str) -> Sequence[dict]:
        result = cls._get_resource_stats_per_agent(company_id, key="resources")
        dpath.merge(
            result, cls._get_experiments_stats_per_agent(company_id, key="experiments")
        )
        return [{"uuid": agent_id, **data} for agent_id, data in result.items()]

    @classmethod
    def _get_resource_stats_per_agent(cls, company_id: str, key: str) -> dict:
        agent_resource_threshold_sec = timedelta(
            hours=config.get("apiserver.statistics.report_interval_hours", 24)
        ).total_seconds()
        to_timestamp = int(time.time())
        from_timestamp = to_timestamp - int(agent_resource_threshold_sec)
        es_req = {
            "size": 0,
            "query": QueryBuilder.dates_range(from_timestamp, to_timestamp),
            "aggs": {
                "workers": {
                    "terms": {"field": "worker"},
                    "aggs": {
                        "categories": {
                            "terms": {"field": "category"},
                            "aggs": {"count": {"cardinality": {"field": "variant"}}},
                        },
                        "metrics": {
                            "terms": {"field": "metric"},
                            "aggs": {
                                "min": {"min": {"field": "value"}},
                                "max": {"max": {"field": "value"}},
                                "avg": {"avg": {"field": "value"}},
                            },
                        },
                    },
                }
            },
        }
        res = cls._run_worker_stats_query(company_id, es_req)

        def _get_cardinality_fields(categories: Sequence[dict]) -> dict:
            names = {"cpu": "num_cores"}
            return {
                names[c["key"]]: nested_get(c, ("count", "value"))
                for c in categories
                if c["key"] in names
            }

        def _get_metric_fields(metrics: Sequence[dict]) -> dict:
            names = {
                "cpu_usage": "cpu_usage",
                "memory_used": "mem_used_gb",
                "memory_free": "mem_free_gb",
            }
            return {
                names[m["key"]]: {
                    "min": nested_get(m, ("min", "value")),
                    "max": nested_get(m, ("max", "value")),
                    "avg": nested_get(m, ("avg", "value")),
                }
                for m in metrics
                if m["key"] in names
            }

        buckets = nested_get(res, ("aggregations", "workers", "buckets"), default=[])
        return {
            b["key"]: {
                key: {
                    "interval_sec": agent_resource_threshold_sec,
                    **_get_cardinality_fields(nested_get(b, ("categories", "buckets"), [])),
                    **_get_metric_fields(nested_get(b, ("metrics", "buckets"), [])),
                }
            }
            for b in buckets
        }

    @classmethod
    def _get_experiments_stats_per_agent(cls, company_id: str, key: str) -> dict:
        agent_relevant_threshold = timedelta(
            days=config.get("apiserver.statistics.agent_relevant_threshold_days", 30)
        )
        to_timestamp = int(time.time())
        from_timestamp = to_timestamp - int(agent_relevant_threshold.total_seconds())
        workers = cls._get_active_workers(company_id, from_timestamp, to_timestamp)
        if not workers:
            return {}

        stats = cls._get_experiments_stats(company_id, list(workers.keys()))
        return {
            worker_id: {key: {**workers[worker_id], **stat}}
            for worker_id, stat in stats.items()
        }

    @classmethod
    def _get_active_workers(
        cls, company_id, from_timestamp: int, to_timestamp: int
    ) -> dict:
        es_req = {
            "size": 0,
            "query": QueryBuilder.dates_range(from_timestamp, to_timestamp),
            "aggs": {
                "workers": {
                    "terms": {"field": "worker"},
                    "aggs": {"last_activity_time": {"max": {"field": "timestamp"}}},
                }
            },
        }
        res = cls._run_worker_stats_query(company_id, es_req)
        buckets = nested_get(res, ("aggregations", "workers", "buckets"), default=[])
        return {
            b["key"]: {"last_activity_time": b["last_activity_time"]["value"]}
            for b in buckets
        }

    @classmethod
    def _run_worker_stats_query(cls, company_id, es_req) -> dict:
        return worker_bll.es_client.search(
            index=f"{WorkerStats.worker_stats_prefix_for_company(company_id)}*",
            body=es_req,
        )

    @classmethod
    def _get_experiments_stats(
        cls, company_id, workers: Optional[Sequence] = None
    ) -> dict:
        pipeline = [
            {
                "$match": {
                    "company": company_id,
                    "started": {"$exists": True, "$ne": None},
                    "last_update": {"$exists": True, "$ne": None},
                    "status": {"$nin": ["created", "queued"]},
                    **({"last_worker": {"$in": workers}} if workers else {}),
                }
            },
            {
                "$project": {
                    "last_worker": 1,
                    "last_update": 1,
                    "started": 1,
                    "last_iteration": 1,
                }
            },
            {
                "$group": {
                    "_id": "$last_worker" if workers else None,
                    "count": {"$sum": 1},
                    "avg_run_time_sec": {
                        "$avg": {
                            "$divide": [
                                {"$subtract": ["$last_update", "$started"]},
                                1000,
                            ]
                        }
                    },
                    "avg_iterations": {"$avg": "$last_iteration"},
                }
            },
            {
                "$project": {
                    "count": 1,
                    "avg_run_time_sec": {"$trunc": "$avg_run_time_sec"},
                    "avg_iterations": {"$trunc": "$avg_iterations"},
                }
            },
        ]
        return {
            group["_id"]: {k: v for k, v in group.items() if k != "_id"}
            for group in Task.aggregate(pipeline)
        }


class WarningFilter(logging.Filter):
    @classmethod
    def attach(cls):
        from urllib3.connectionpool import (
            ConnectionPool,
        )  # required to make sure the logger is created

        assert ConnectionPool  # make sure import is not optimized out

        logging.getLogger("urllib3.connectionpool").addFilter(cls())

    def filter(self, record):
        if (
            record.levelno == logging.WARNING
            and len(record.args) > 2
            and record.args[2] == "/stats"
        ):
            return False
        return True
