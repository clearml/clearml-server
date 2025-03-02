from time import time, sleep

from apiserver.apierrors import errors
from apiserver.tests.automated import TestService


class TestServing(TestService):
    def test_status_report(self):
        container_id1 = "container_1"
        container_id2 = "container_2"
        url = "http://test_url"
        reference = [
            {"type": "app_id", "value": "test"},
            {"type": "app_instance", "value": "abd478c8"},
            {"type": "model", "value": "262829d3"},
            {"type": "model", "value": "7ea29c04"},
        ]
        container_infos = [
            {
                "container_id": container_id,  # required
                "endpoint_name": "my endpoint",  # required
                "endpoint_url": url,  # can be omitted for register but required for status report
                "model_name": "my model",  # required
                "model_source": "s3//my_bucket",  # optional right now
                "model_version": "3.1.0",  # optional right now
                "preprocess_artifact": "some string here",  # optional right now
                "input_type": "another string here",  # optional right now
                "input_size": 9_000_000,  # optional right now, bytes
                "tags": ["tag1", "tag2"],  # optional
                "system_tags": None,  # optional
                **({"reference": reference} if container_id == container_id1 else {}),
            }
            for container_id in (container_id1, container_id2)
        ]

        # registering instances
        for container_info in container_infos:
            self.api.serving.register_container(
                **container_info,
                timeout=100,  # expiration timeout in seconds. Optional, the default value is 600
            )
        for idx, container_info in enumerate(container_infos):
            mul = idx + 1
            self.api.serving.container_status_report(
                **container_info,
                uptime_sec=1000 * mul,
                requests_num=1000 * mul,
                requests_min=5 * mul,  # requests per minute
                latency_ms=100 * mul,  # average latency
                machine_stats={  # the same structure here as used by worker status_reports
                    "cpu_usage": [10, 20],
                    "memory_used": 50 * 1024,
                },
            )

        # getting endpoints and endpoint details
        endpoints = self.api.serving.get_endpoints().endpoints
        self.assertTrue(any(e for e in endpoints if e.url == url))
        details = self.api.serving.get_endpoint_details(endpoint_url=url)
        self.assertEqual(details.url, url)
        self.assertEqual(details.uptime_sec, 2000)
        self.assertEqual(
            {
                inst.id: [
                    inst[field]
                    for field in (
                        "uptime_sec",
                        "requests",
                        "requests_min",
                        "latency_ms",
                        "cpu_count",
                        "gpu_count",
                        "reference",
                    )
                ]
                for inst in details.instances
            },
            {
                "container_1": [1000, 1000, 5, 100, 2, 0, reference],
                "container_2": [2000, 2000, 10, 200, 2, 0, []],
            },
        )
        # make sure that the first call did not invalidate anything
        new_details = self.api.serving.get_endpoint_details(endpoint_url=url)
        self.assertEqual(details, new_details)

        # charts
        sleep(5)  # give time to ES to accomodate data
        to_date = int(time()) + 40
        from_date = to_date - 100
        for metric_type, title, value in (
            (None, "Number of Requests", 3000),
            ("requests_min", "Requests per Minute", 15),
            ("latency_ms", "Average Latency (ms)", 150),
            ("cpu_count", "CPU Count", 4),
            ("cpu_util", "Average CPU Load (%)", 15),
            ("ram_used", "RAM Used (GB)", 100.0),
        ):
            res = self.api.serving.get_endpoint_metrics_history(
                endpoint_url=url,
                from_date=from_date,
                to_date=to_date,
                interval=1,
                **({"metric_type": metric_type} if metric_type else {}),
            )
            self.assertEqual(res.computed_interval, 40)
            self.assertEqual(res.total.title, title)
            length = len(res.total.dates)
            self.assertTrue(3 >= length >= 1)
            self.assertEqual(len(res.total["values"]), length)
            self.assertIn(value, res.total["values"])
            self.assertEqual(set(res.instances), {container_id1, container_id2})
            for inst in res.instances.values():
                self.assertEqual(inst.dates, res.total.dates)
                self.assertEqual(len(inst["values"]), length)

        # unregistering containers
        for container_id in (container_id1, container_id2):
            self.api.serving.unregister_container(container_id=container_id)
        endpoints = self.api.serving.get_endpoints().endpoints
        self.assertFalse(any(e for e in endpoints if e.url == url))

        with self.api.raises(errors.bad_request.NoContainersForUrl):
            self.api.serving.get_endpoint_details(endpoint_url=url)
