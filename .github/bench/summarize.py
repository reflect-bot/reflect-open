#!/usr/bin/env python3
# TEMPORARY (release size bench): merges every attempt of this run into tables.
import glob
import json
import os
import subprocess
from datetime import datetime

REPO = os.environ["GITHUB_REPOSITORY"]
RUN_ID = os.environ["GITHUB_RUN_ID"]
ATTEMPTS = int(os.environ["GITHUB_RUN_ATTEMPT"])
ROUND = os.environ["BENCH_ROUND"]
CONFIGS = ("base", "new")


def gh(path):
    args = ["gh", "api", "--paginate", "--slurp", path]
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)


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


rows = {}
for attempt in range(1, ATTEMPTS + 1):
    for page in gh(f"repos/{REPO}/actions/runs/{RUN_ID}/attempts/{attempt}/jobs?per_page=100"):
        for job in page["jobs"]:
            target, _, config = job["name"].rpartition(" ")
            if config not in CONFIGS:
                continue
            rows[(target, config, attempt)] = {
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

for path in glob.glob(os.path.join(os.environ["BENCH_DIR"], "*.json")):
    with open(path) as file:
        measure = json.load(file)
    rows.setdefault((measure["target"], measure["config"], measure["attempt"]), {}).update(measure)

targets = sorted({target for target, _, _ in rows})
lines = [
    f"# Release size bench, round {ROUND}",
    "",
    "Attempt 1 starts from an empty cargo cache. Attempt 2 reuses the cache that attempt 1 saved.",
    "",
    "## Build time (seconds)",
    "",
    "| target | attempt | config | result | cache hit | setup-rust | build | cache save | job |",
    "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
]
for target in targets:
    for attempt in range(1, ATTEMPTS + 1):
        for config in CONFIGS:
            row = rows.get((target, config, attempt))
            if row:
                cells = [target, attempt, config, row.get("result"), row.get("cache_hit")]
                cells += [row.get(key) for key in ("setup_rust_s", "build_s", "cache_save_s", "job_s")]
                lines.append("| " + " | ".join(text(cell) for cell in cells) + " |")

lines += ["", "## Build time, new vs base", "", "| target | attempt | build | job |", "| --- | --- | ---: | ---: |"]
for target in targets:
    for attempt in range(1, ATTEMPTS + 1):
        base, new = rows.get((target, "base", attempt)), rows.get((target, "new", attempt))
        if base and new:
            build = change(base.get("build_s"), new.get("build_s"))
            job = change(base.get("job_s"), new.get("job_s"))
            lines.append(f"| {target} | {attempt} | {build} | {job} |")

lines += ["", "## Download size (MiB, attempt 1)", "", "| target | file | base | new | change |", "| --- | --- | ---: | ---: | ---: |"]
for target in targets:
    base, new = rows.get((target, "base", 1), {}), rows.get((target, "new", 1), {})
    for key in sorted(key for key in set(base) | set(new) if key.endswith("_bytes")):
        name = key.removesuffix("_bytes")
        lines.append(f"| {target} | {name} | {mib(base.get(key))} | {mib(new.get(key))} | {change(base.get(key), new.get(key))} |")
    for key in ("symbol_count", "line_tables"):
        if key in base or key in new:
            lines.append(f"| {target} | {key} | {text(base.get(key))} | {text(new.get(key))} | |")

report = "\n".join(lines) + "\n"
with open("summary.md", "w") as file:
    file.write(report)
with open("summary.json", "w") as file:
    entries = [{"target": t, "config": c, "attempt": a, **row} for (t, c, a), row in sorted(rows.items())]
    json.dump({"round": ROUND, "rows": entries}, file, indent=2)
with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
    file.write(report)
