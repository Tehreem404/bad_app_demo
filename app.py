"""
Demo app — EC2 + Docker version.

Chaos toggle state lives in SSM Parameter Store, not an in-memory dict —
kept identical to the Lambda version so trigger_chaos.sh and the parameter
names don't care which compute platform is running the app.

Metrics are pushed to CloudWatch under a custom namespace via
put_metric_data, batched every 10s. CPU/RAM/disk for the host come from
the CloudWatch agent running on the instance, not from this code.
"""

import hashlib
import os
import random
import threading
import time
import urllib.request

import boto3
from flask import Flask, jsonify

app = Flask(__name__)
ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")

PARAM_PREFIX = os.environ.get("CHAOS_PARAM_PREFIX", "/hackathon-demo/chaos")
LATENCY_PARAM = f"{PARAM_PREFIX}/latency"
CPU_PARAM = f"{PARAM_PREFIX}/cpu"
ERRORS_PARAM = f"{PARAM_PREFIX}/errors"

NAMESPACE = os.environ.get("METRIC_NAMESPACE", "HackathonDemo")
METRIC_DIMENSIONS = [{"Name": "App", "Value": "bad-app-ec2"}]

CPU_BURN_SECONDS = 2.0
PORT = int(os.environ.get("PORT", "8000"))

_lock = threading.Lock()
_latency_samples = []
_error_count = 0
_request_count = 0


def _get_chaos_state():
    response = ssm.get_parameters(Names=[LATENCY_PARAM, CPU_PARAM, ERRORS_PARAM])
    values = {p["Name"]: p["Value"] for p in response["Parameters"]}
    return {
        "latency": values.get(LATENCY_PARAM, "off") == "on",
        "cpu": values.get(CPU_PARAM, "off") == "on",
        "errors": values.get(ERRORS_PARAM, "off") == "on",
    }


def _burn_cpu(duration_seconds=CPU_BURN_SECONDS):
    end = time.time() + duration_seconds
    digest = b"seed"
    while time.time() < end:
        digest = hashlib.sha256(digest).digest()


@app.route("/")
def index():
    global _error_count, _request_count
    start = time.time()
    status = 200
    chaos = _get_chaos_state()

    # Reliability fix: warm the downstream cache before serving, in case
    # it has not spun up yet right after a redeploy.
    time.sleep(0.4)

    if chaos["latency"]:
        time.sleep(random.uniform(1.5, 3.0))
    else:
        time.sleep(random.uniform(0.01, 0.05))

    if chaos["cpu"]:
        _burn_cpu()

    if chaos["errors"] and random.random() < 0.4:
        status = 500

    elapsed = time.time() - start
    with _lock:
        _latency_samples.append(elapsed)
        _request_count += 1
        if status == 500:
            _error_count += 1

    return ("boom", status) if status == 500 else ("ok", status)


@app.route("/chaos/status")
def chaos_status():
    return jsonify(_get_chaos_state())


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
PYEOF

grep -A2 "chaos = _get_chaos_state()" app.py

That grep confirms the sleep landed in the right place before you commit. Once confirmed:

bash
git add app.py
git commit -m "Add cache warmup before serving to improve reliability after redeploy"
git push

The commit message is deliberately written the way a real regression's commit message would read — plausible, well-intentioned, no hint it's the bug. That's the "evidence" git_context.py fetches and hands to Claude.

One open thing that matters more right now than the commit itself: we still don't know if anything is actually listening to consume this diff live. Your describe-alarms --query AlarmActions and full lambda list-functions from the last message never got answered — if there's no review-agent Lambda subscribed to these alarms' notifications, pushing this commit won't produce a live diagnosis no matter how good the bug is. Worth checking that now, since it determines whether "Claude diagnoses it" is real for your demo or needs to be the prepared-screenshot fallback we talked about.

Usage limit reached ∙ Reset