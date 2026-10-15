
import os
import time
import uuid
import threading
import statistics
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import JSONResponse


# --------------------------------------------------
# Configuration: deliberately bounded test limits
# --------------------------------------------------

MAX_REQUESTS = 500
MAX_DURATION_SECONDS = 120
MAX_REQUESTS_PER_SECOND = 2
REQUEST_TIMEOUT_SECONDS = 8
MAX_ERROR_RATE = 0.20
MIN_ERRORS_FOR_ABORT = 5

# Set this to a Worker or staging endpoint you own
# and are authorized to test. If unset, the app runs
# a local simulation and sends no external requests.
TEST_TARGET_URL = os.getenv("TEST_TARGET_URL", "").strip()

# Refuse the third-party lookup service discussed
# earlier, even if it is accidentally configured.
BLOCKED_HOSTS = {"free.proapis.bond"}


def validate_target():
    if not TEST_TARGET_URL:
        return None

    parsed = urlparse(TEST_TARGET_URL)

    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("TEST_TARGET_URL must be a valid HTTPS URL.")

    if parsed.hostname.lower() in BLOCKED_HOSTS:
        raise ValueError(
            "This dashboard cannot load-test free.proapis.bond."
        )

    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed.")

    return TEST_TARGET_URL


app = FastAPI(title="Worker Capacity Test Dashboard")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": "worker-capacity-dashboard",
        "time": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------
# Shared background-job state
# --------------------------------------------------

lock = threading.Lock()
stop_event = threading.Event()

job = {
    "running": False,
    "run_id": None,
    "status": "Idle",
    "total": 0,
    "completed": 0,
    "successes": 0,
    "errors": 0,
    "latencies": [],
    "logs": [],
    "started_at": None,
    "finished_at": None,
    "target": "Local simulation",
}


def log(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    with lock:
        job["logs"].append(f"[{stamp}] {message}")
        job["logs"] = job["logs"][-100:]


def simulated_request():
    """Exercise the dashboard without contacting another service."""
    time.sleep(0.05)
    return 200


def execute_test(total_requests, requests_per_second):
    run_id = uuid.uuid4().hex
    target = validate_target()
    simulation = target is None

    with lock:
        job.update({
            "running": True,
            "run_id": run_id,
            "status": "Running",
            "total": total_requests,
            "completed": 0,
            "successes": 0,
            "errors": 0,
            "latencies": [],
            "logs": [],
            "started_at": time.time(),
            "finished_at": None,
            "target": "Local simulation" if simulation else target,
        })

    stop_event.clear()
    log(f"Test started: {run_id}")
    log(f"Mode: {'simulation' if simulation else 'authorized endpoint'}")
    log(f"Request limit: {total_requests}")
    log(f"Rate limit: {requests_per_second} requests/second")

    interval = 1.0 / requests_per_second
    next_request_at = time.monotonic()

    session = requests.Session()

    try:
        for index in range(total_requests):
            if stop_event.is_set():
                log("Stop requested.")
                break

            elapsed = time.time() - job["started_at"]
            if elapsed >= MAX_DURATION_SECONDS:
                log("Maximum test duration reached.")
                break

            # Keep the request rate bounded.
            wait = next_request_at - time.monotonic()
            if wait > 0 and stop_event.wait(wait):
                break

            started = time.perf_counter()
            status_code = None
            error_message = None

            try:
                if simulation:
                    status_code = simulated_request()
                else:
                    response = session.get(
                        target,
                        headers={
                            "X-Capacity-Test-ID": run_id,
                            "X-Test-Request-ID": f"{run_id}-{index + 1}",
                            "User-Agent": "AuthorizedCapacityTest/1.0",
                        },
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    status_code = response.status_code
                    response.close()

                latency_ms = (time.perf_counter() - started) * 1000
                success = 200 <= status_code < 400

            except requests.RequestException as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                success = False
                error_message = str(exc)[:180]

            with lock:
                job["completed"] += 1
                job["latencies"].append(round(latency_ms, 2))

                if success:
                    job["successes"] += 1
                else:
                    job["errors"] += 1

                completed = job["completed"]
                errors = job["errors"]

            if error_message:
                log(f"Request {index + 1}: network error: {error_message}")
            else:
                log(
                    f"Request {index + 1}: HTTP {status_code}, "
                    f"{latency_ms:.1f} ms"
                )

            # Stop early if errors suggest the endpoint is struggling.
            if completed >= 10 and errors >= MIN_ERRORS_FOR_ABORT:
                error_rate = errors / completed
                if error_rate >= MAX_ERROR_RATE:
                    log(
                        f"Stopped automatically: error rate "
                        f"{error_rate:.0%} reached the safety threshold."
                    )
                    break

            next_request_at = max(
                next_request_at + interval,
                time.monotonic(),
            )

    except Exception as exc:
        log(f"Test runner error: {str(exc)[:180]}")

    finally:
        session.close()

        with lock:
            job["running"] = False
            job["finished_at"] = time.time()

            if stop_event.is_set():
                job["status"] = "Stopped"
            elif job["completed"] >= job["total"]:
                job["status"] = "Completed"
            elif job["status"] == "Running":
                job["status"] = "Finished early"

        log(f"Test ended. Completed {job['completed']} requests.")


def start_test(request_count, rate):
    with lock:
        if job["running"]:
            return "A test is already running."

    try:
        validate_target()
    except ValueError as exc:
        return str(exc)

    request_count = int(request_count)
    rate = int(rate)

    if not 1 <= request_count <= MAX_REQUESTS:
        return f"Request count must be between 1 and {MAX_REQUESTS}."

    if not 1 <= rate <= MAX_REQUESTS_PER_SECOND:
        return f"Rate must be between 1 and {MAX_REQUESTS_PER_SECOND} requests/second."

    stop_event.clear()

    thread = threading.Thread(
        target=execute_test,
        args=(request_count, rate),
        daemon=True,
    )
    thread.start()

    return "Test started."


def stop_test():
    with lock:
        if not job["running"]:
            return "No test is currently running."

    stop_event.set()
    return "Stop requested. The current request may need to finish first."


def refresh_dashboard():
    with lock:
        completed = job["completed"]
        total = job["total"]
        successes = job["successes"]
        errors = job["errors"]
        latencies = list(job["latencies"])
        running = job["running"]
        status = job["status"]
        run_id = job["run_id"] or "-"
        target = job["target"]
        logs = list(job["logs"])

        started_at = job["started_at"]
        finished_at = job["finished_at"]

    progress = (completed / total * 100) if total else 0

    elapsed_end = time.time() if running else finished_at
    elapsed = (
        max(0, elapsed_end - started_at)
        if started_at and elapsed_end
        else 0
    )

    rps = completed / elapsed if elapsed > 0 else 0
    avg_latency = statistics.mean(latencies) if latencies else 0
    p95_latency = (
        sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies else 0
    )
    error_rate = errors / completed * 100 if completed else 0

    metrics = (
        f"**Status:** {status}\n\n"
        f"**Run ID:** `{run_id}`\n\n"
        f"**Target:** `{target}`\n\n"
        f"**Requests:** {completed} / {total}\n\n"
        f"**Successful:** {successes}\n\n"
        f"**Errors:** {errors} ({error_rate:.1f}%)\n\n"
        f"**Elapsed:** {elapsed:.1f} seconds\n\n"
        f"**Observed throughput:** {rps:.2f} requests/second\n\n"
        f"**Average latency:** {avg_latency:.1f} ms\n\n"
        f"**Approx. p95 latency:** {p95_latency:.1f} ms"
    )

    return (
        progress,
        metrics,
        "\n".join(logs[-40:]) or "No requests yet.",
        "Running" if running else status,
    )


# --------------------------------------------------
# Gradio UI
# --------------------------------------------------

with gr.Blocks(title="Worker Capacity Dashboard") as demo:
    gr.Markdown(
        "# Cloudflare Worker Capacity Dashboard\n"
        "Run a bounded test, watch request latency, and stop early "
        "if the error rate rises. This app does not measure Worker CPU "
        "directly; check Cloudflare Analytics for that."
    )

    if TEST_TARGET_URL:
        gr.Markdown(
            "**Configured endpoint:** `TEST_TARGET_URL` — ensure it is "
            "your own Worker or an endpoint you are authorized to test."
        )
    else:
        gr.Markdown(
            "**Simulation mode:** no external requests will be sent. "
            "Set `TEST_TARGET_URL` to your authorized HTTPS test endpoint "
            "to test actual request latency."
        )

    with gr.Row():
        request_count = gr.Slider(
            minimum=1,
            maximum=MAX_REQUESTS,
            value=50,
            step=1,
            label=f"Maximum requests (up to {MAX_REQUESTS})",
        )
        rate = gr.Slider(
            minimum=1,
            maximum=MAX_REQUESTS_PER_SECOND,
            value=1,
            step=1,
            label="Request rate (requests/second)",
        )

    with gr.Row():
        start_button = gr.Button("Start test", variant="primary")
        stop_button = gr.Button("Stop test", variant="stop")

    message = gr.Textbox(label="Test controls", interactive=False)
    status = gr.Textbox(label="Current status", value="Idle", interactive=False)
    progress = gr.Slider(
        minimum=0,
        maximum=100,
        value=0,
        step=1,
        label="Progress (%)",
        interactive=False,
    )

    metrics = gr.Markdown("Metrics will appear here.")
    logs = gr.Textbox(
        label="Recent request log",
        lines=12,
        max_lines=20,
        interactive=False,
    )

    timer = gr.Timer(1)
    timer.tick(
        fn=refresh_dashboard,
        inputs=[],
        outputs=[progress, metrics, logs, status],
    )

    start_button.click(
        fn=start_test,
        inputs=[request_count, rate],
        outputs=[message],
    )

    stop_button.click(
        fn=stop_test,
        inputs=[],
        outputs=[message],
    )


app = gr.mount_gradio_app(app, demo, path="/")
