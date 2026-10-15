
import os
import time
import uuid
import threading
from urllib.parse import urlparse

import gradio as gr
import requests


# Informational quotas, not live account usage.
PLAN_LIMITS = {
    "Free — 100,000 requests/day": 100_000,
    "Paid — 10 million requests/month included": 10_000_000,
}

# This app is intentionally a low-volume connectivity tester.
MAX_TEST_REQUESTS = 20

jobs = {}
jobs_lock = threading.Lock()


def worker_test(job_id, url, count, delay):
    started_at = time.monotonic()

    with jobs_lock:
        jobs[job_id]["status"] = "Running"

    session = requests.Session()
    session.headers.update({"User-Agent": "Workers-Connectivity-Tester/1.0"})

    try:
        for index in range(count):
            request_started = time.monotonic()

            try:
                response = session.get(
                    url,
                    timeout=10,
                    allow_redirects=False,
                )
                duration = (time.monotonic() - request_started) * 1000
                success = 200 <= response.status_code < 400

                line = (
                    f"Request {index + 1}/{count}: "
                    f"HTTP {response.status_code} — {duration:.0f} ms"
                )
            except requests.RequestException as exc:
                success = False
                line = f"Request {index + 1}/{count}: ERROR — {exc}"

            with jobs_lock:
                job = jobs[job_id]
                job["done"] += 1
                job["success"] += int(success)
                job["logs"].append(line)

            if index < count - 1:
                time.sleep(delay)

    finally:
        session.close()

        with jobs_lock:
            job = jobs[job_id]
            job["status"] = "Completed"
            job["elapsed"] = time.monotonic() - started_at


def start_test(url, plan, count, delay, confirmed):
    if not confirmed:
        raise gr.Error(
            "Confirm that you own the endpoint or have permission to test it."
        )

    url = url.strip()
    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise gr.Error(
            "Enter a valid HTTPS URL, such as "
            "https://your-worker.workers.dev"
        )

    if count < 1 or count > MAX_TEST_REQUESTS:
        raise gr.Error("Choose between 1 and 20 requests.")

    job_id = uuid.uuid4().hex[:10]

    with jobs_lock:
        jobs[job_id] = {
            "status": "Starting",
            "done": 0,
            "success": 0,
            "logs": [],
            "start": time.monotonic(),
            "elapsed": 0,
            "count": int(count),
            "plan": plan,
        }

    thread = threading.Thread(
        target=worker_test,
        args=(job_id, url, int(count), float(delay)),
        daemon=True,
    )
    thread.start()

    return job_id, "Test started.", ""


def refresh(job_id):
    if not job_id:
        return "Ready. Configure your test and press Start.", ""

    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return (
                "Job not found. The service may have restarted.",
                "",
            )

        done = job["done"]
        total = job["count"]
        elapsed = time.monotonic() - job["start"]
        rate = done / elapsed if elapsed > 0 else 0
        remaining = (total - done) / rate if rate > 0 else 0
        quota = PLAN_LIMITS[job["plan"]]
        percent = 100 * done / total

        summary = (
            f"Status: {job['status']}\n"
            f"Progress: {done}/{total} ({percent:.0f}%)\n"
            f"Successful responses: {job['success']}\n"
            f"Failed or non-success responses: {done - job['success']}\n"
            f"Elapsed time: {elapsed:.1f} seconds\n"
            f"Estimated remaining: {remaining:.1f} seconds\n"
            f"Average rate: {rate:.2f} requests/second\n"
            f"Selected plan: {job['plan']}\n"
            f"Illustrative plan quota: {quota:,}\n"
            f"Quota is not live account usage."
        )

        log_text = "\n".join(job["logs"])

    return summary, log_text


with gr.Blocks(title="Workers Test Dashboard") as demo:
    gr.Markdown("# Cloudflare Workers Test Dashboard")
    gr.Markdown(
        "Check connectivity, response times, and test progress. "
        "This dashboard does not read your Cloudflare account analytics."
    )

    url = gr.Textbox(
        label="HTTPS endpoint you control",
        placeholder="https://your-worker.workers.dev",
    )

    plan = gr.Dropdown(
        choices=list(PLAN_LIMITS.keys()),
        value="Free — 100,000 requests/day",
        label="Plan to model (informational only)",
    )

    count = gr.Slider(
        minimum=1,
        maximum=MAX_TEST_REQUESTS,
        value=5,
        step=1,
        label="Number of requests (maximum 20)",
    )

    delay = gr.Slider(
        minimum=1,
        maximum=10,
        value=1,
        step=1,
        label="Delay between requests (seconds)",
    )

    confirmed = gr.Checkbox(
        label="I own this endpoint or have permission to test it."
    )

    job_id = gr.Textbox(label="Job ID", interactive=False)
    start_button = gr.Button("Start connectivity test", variant="primary")
    summary = gr.Textbox(label="Live progress", lines=10)
    logs = gr.Textbox(label="Request log", lines=10)

    start_button.click(
        fn=start_test,
        inputs=[url, plan, count, delay, confirmed],
        outputs=[job_id, summary, logs],
        queue=False,
    )

    timer = gr.Timer(1)
    timer.tick(
        fn=refresh,
        inputs=[job_id],
        outputs=[summary, logs],
        queue=False,
    )


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", "10000")),
    )
