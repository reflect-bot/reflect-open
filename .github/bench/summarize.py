#!/usr/bin/env python3
# TEMPORARY (release size bench): merges every attempt of the bench runs into
# tables. A re-run hides the artifacts of earlier attempts, so the measurements
# come from the job logs, which every attempt keeps.
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
# This run, plus earlier bench runs to report again.
RUN_IDS = os.environ.get("BENCH_RUN_IDS", "").split() + [os.environ["GITHUB_RUN_ID"]]
CONFIGS = ("base", "new")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url, auth=True):
    request = urllib.request.Request(url)
    if auth:
        request.add_header("Authorization", f"Bearer {TOKEN}")
        request.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.build_opener(NoRedirect).open(request) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # Job logs redirect to storage that rejects the GitHub token.
        if error.code in (301, 302, 307) and error.headers.get("Location"):
            return fetch(error.headers["Location"], auth=False)
        raise


def api(path):
    return json.loads(fetch(f"https://api.github.com/repos/{REPO}/{path}"))


def read_measure(job_id):
    lines = [line.split(" ", 1)[-1] for line in fetch_log(job_id).splitlines()]
    for line in reversed(lines):
        if line.startswith("BENCH_JSON "):
            return json.loads(line.removeprefix("BENCH_JSON "))
    # Older runs printed the JSON over several lines.
    start = max((i for i, line in enumerate(lines) if line == "{"), default=None)
    if start is None:
        return {}
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return json.loads("\n".join(lines[start : end + 1]))


def fetch_log(job_id):
    return fetch(f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs")


def seconds(item):
    start, end = item.get("started_at"), item.get("completed_at")
    if not start or not end:
        return None
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    return round((parse(end) - parse(start)).total_seconds())


def step_seconds(job, matches):
    for step in job.get("steps") or []:
        if matches(step["name"]):
            return seconds(step)
    return None


def text(value):
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def mib(value):
    return "" if value is None else f"{value / 2**20:.2f}"


def change(base, new):
    if not base or new is None:
        return ""
    return f"{(new - base) / base * 100:+.1f}%"


def summarize(run_id):
    run = api(f"actions/runs/{run_id}")
    rows = {}
    for attempt in range(1, run["run_attempt"] + 1):
        jobs = api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")["jobs"]
        for job in jobs:
            target, _, config = job["name"].rpartition(" ")
            if config not in CONFIGS:
                continue
            row = {
                "result": job["conclusion"],
                "setup_rust_s": step_seconds(
                    job, lambda name: "setup-rust" in name and not name.startswith("Post ")
                ),
                "build_s": step_seconds(job, lambda name: name == "Build"),
                "cache_save_s": step_seconds(
                    job, lambda name: "setup-rust" in name and name.startswith("Post ")
                ),
                "job_s": seconds(job),
            }
            if job["conclusion"] == "success":
                row.update(read_measure(job["id"]))
            rows[(target, config, attempt)] = row

    attempts = range(1, run["run_attempt"] + 1)
    targets = sorted({target for target, _, _ in rows})
    lines = [
        f"## Run {run_id} ({run['head_sha'][:8]})",
        "",
        "Attempt 1 starts from an empty cargo cache. Attempt 2 reuses the cache that attempt 1 saved.",
        "",
        "### Build time (seconds)",
        "",
        "| target | attempt | config | result | cache hit | setup-rust | build | cache save | job |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for target in targets:
        for attempt in attempts:
            for config in CONFIGS:
                row = rows.get((target, config, attempt))
                if row:
                    cells = [target, attempt, config, row.get("result"), row.get("cache_hit")]
                    cells += [row.get(key) for key in ("setup_rust_s", "build_s", "cache_save_s", "job_s")]
                    lines.append("| " + " | ".join(text(cell) for cell in cells) + " |")

    lines += ["", "### Build time, new vs base", "", "| target | attempt | build | job |", "| --- | --- | ---: | ---: |"]
    for target in targets:
        for attempt in attempts:
            base, new = rows.get((target, "base", attempt)), rows.get((target, "new", attempt))
            if base and new:
                build = change(base.get("build_s"), new.get("build_s"))
                job = change(base.get("job_s"), new.get("job_s"))
                lines.append(f"| {target} | {attempt} | {build} | {job} |")

    lines += ["", "### Download size (MiB, attempt 1)", "", "| target | file | base | new | change |", "| --- | --- | ---: | ---: | ---: |"]
    for target in targets:
        base, new = rows.get((target, "base", 1), {}), rows.get((target, "new", 1), {})
        for key in sorted(key for key in set(base) | set(new) if key.endswith("_bytes")):
            name = key.removesuffix("_bytes")
            lines.append(f"| {target} | {name} | {mib(base.get(key))} | {mib(new.get(key))} | {change(base.get(key), new.get(key))} |")
        for key in ("symbol_count", "line_tables"):
            if key in base or key in new:
                lines.append(f"| {target} | {key} | {text(base.get(key))} | {text(new.get(key))} | |")
    entries = [{"target": t, "config": c, "attempt": a, **row} for (t, c, a), row in sorted(rows.items())]
    return "\n".join(lines) + "\n", {"run": run_id, "rows": entries}


reports = [summarize(run_id) for run_id in RUN_IDS]
report = "# Release size bench\n\n" + "\n".join(markdown for markdown, _ in reports)
with open("summary.md", "w") as file:
    file.write(report)
with open("summary.json", "w") as file:
    json.dump([data for _, data in reports], file, indent=2)
with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
    file.write(report)
print(report)
