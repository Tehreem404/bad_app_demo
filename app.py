"""
Demo app — EC2 + Docker version.

Clean baseline: no chaos toggles or SSM-driven flags. During the demo, a
separate "bad" version of this file (with the CPU-burning reliability bug)
gets pushed to replace it entirely.

Metrics are pushed to CloudWatch under a custom namespace via
put_metric_data, batched every 10s. CPU/RAM/disk for the host come from
the CloudWatch agent running on the instance, not from this code.
"""

import os
import random
import threading
import time
import urllib.request

import boto3
from flask import Flask

app = Flask(__name__)
cloudwatch = boto3.client("cloudwatch")

NAMESPACE = os.environ.get("METRIC_NAMESPACE", "HackathonDemo")
METRIC_DIMENSIONS = [{"Name": "App", "Value": "bad-app-ec2"}]

PORT = int(os.environ.get("PORT", "8000"))

_lock = threading.Lock()
_latency_samples = []
_error_count = 0
_request_count = 0


@app.route("/")
def index():
    global _error_count, _request_count
    start = time.time()
    status = 200

    time.sleep(random.uniform(0.01, 0.05))

    elapsed = time.time() - start
    with _lock:
        _latency_samples.append(elapsed)
        _request_count += 1
        if status == 500:
            _error_count += 1

    return ("boom", status) if status == 500 else ("ok", status)


@app.route("/healthz")
def healthz():
    return "ok"


def _flush_metrics():
    global _error_count, _request_count
    while True:
        time.sleep(10)
        with _lock:
            samples, count, errors = _latency_samples[:], _request_count, _error_count
            _latency_samples.clear()
            _request_count = 0
            _error_count = 0

        if not samples:
            continue

        metric_data = [
            {
                "MetricName": "RequestLatency",
                "Dimensions": METRIC_DIMENSIONS,
                "Unit": "Seconds",
                "StatisticValues": {
                    "SampleCount": len(samples),
                    "Sum": sum(samples),
                    "Minimum": min(samples),
                    "Maximum": max(samples),
                },
            },
            {
                "MetricName": "ErrorRate",
                "Dimensions": METRIC_DIMENSIONS,
                "Unit": "Percent",
                "Value": (errors / count * 100) if count else 0,
            },
        ]
        try:
            cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)
        except Exception as e:
            print(f"put_metric_data failed: {e}")


def _generate_traffic():
    while True:
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=5)
        except Exception:
            pass
        time.sleep(0.2)


if __name__ == "__main__":
    threading.Thread(target=_flush_metrics, daemon=True).start()
    threading.Thread(target=_generate_traffic, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
