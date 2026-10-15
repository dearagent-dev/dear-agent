#!/usr/bin/env python3
"""Dear Agent — live demo console.

A small, read-only web console for showing how the agent reads the mail: it renders the
inbound message, the queue, the runner Job and the coding agent's live output, and ends on
the draft pull request. It is a *demo aid*, not a product surface: it never writes to the
cluster and it never holds a credential of its own (it reads the database through a
port-forward and the runner log through ``oc logs``).

    scripts/demo_console.py                     # http://127.0.0.1:8765
    scripts/demo_console.py --port 9000 --task "<id@host>"

Requires ``oc`` (logged in) and the project's venv (``psycopg``). Run it with
``.venv/bin/python scripts/demo_console.py`` or make it executable after activating the venv.

The page polls ``/api/state`` every 1.5s; nothing is cached beyond one second.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Dear Agent — live console</title>
<style>
  :root {
    --bg: #0b0d12;
    --panel: #12141c;
    --panel-2: #171a24;
    --line: #242938;
    --ink: #e8eaf2;
    --muted: #8b93a7;
    --accent: #7c9cff;
    --accent-2: #58e0c0;
    --warm: #ffcc7c;
    --bad: #ff7a90;
    --mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background:
      radial-gradient(1100px 600px at 12% -10%, #1b2340 0%, transparent 60%),
      radial-gradient(900px 500px at 100% 0%, #14283a 0%, transparent 55%),
      var(--bg);
    color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    min-height: 100vh;
  }
  header {
    display: flex; align-items: center; gap: 18px;
    padding: 20px 28px; border-bottom: 1px solid var(--line);
    background: rgba(10, 12, 18, .6); backdrop-filter: blur(8px);
    position: sticky; top: 0; z-index: 5;
  }
  .mark { display: flex; align-items: baseline; gap: 10px; }
  .mark b { font-family: var(--serif); font-size: 27px; font-weight: 600; letter-spacing: .2px; }
  .mark span { color: var(--muted); font-size: 12.5px; letter-spacing: .16em; text-transform: uppercase; }
  .status { margin-left: auto; display: flex; align-items: center; gap: 18px; font-size: 12.5px; color: var(--muted); }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #4b5162; display: inline-block; margin-right: 7px; }
  .dot.live { background: var(--accent-2); box-shadow: 0 0 0 4px rgba(88,224,192,.12); animation: pulse 1.6s infinite; }
  .dot.bad { background: var(--bad); box-shadow: 0 0 0 4px rgba(255,122,144,.12); }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .45 } }
  main { display: grid; grid-template-columns: 1.05fr 1.25fr 1.05fr; gap: 18px; padding: 18px 28px 34px; }
  @media (max-width: 1200px) { main { grid-template-columns: 1fr; } }
  .panel { background: linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%);
           border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
  .panel h2 { margin: 0; padding: 14px 18px; font-size: 12px; letter-spacing: .18em;
              text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--line);
              display: flex; align-items: center; gap: 10px; }
  .panel .body { padding: 18px; }
  .envelope { font-family: var(--mono); font-size: 13px; }
  .envelope .row { display: flex; gap: 10px; margin-bottom: 9px; }
  .envelope .k { color: var(--muted); min-width: 74px; }
  .envelope .v { color: var(--ink); word-break: break-word; }
  .envelope .body-text { margin-top: 14px; padding: 14px; background: #0e1118;
                         border: 1px solid var(--line); border-radius: 11px; white-space: pre-wrap;
                         line-height: 1.6; color: #dfe4f2; }
  .stages { display: flex; flex-direction: column; gap: 10px; }
  .stage { display: flex; align-items: center; gap: 13px; padding: 12px 14px; border-radius: 12px;
           border: 1px solid var(--line); background: #0f121a; opacity: .45; transition: all .35s ease; }
  .stage .ico { width: 30px; height: 30px; border-radius: 9px; display: grid; place-items: center;
                background: #1a1f2c; font-size: 15px; }
  .stage .t { font-weight: 600; }
  .stage .s { color: var(--muted); font-size: 12.5px; }
  .stage .at { margin-left: auto; font-family: var(--mono); font-size: 12px; color: var(--muted); }
  .stage.on { opacity: 1; border-color: #33405e; background: linear-gradient(90deg, #141a29, #101521); }
  .stage.on .ico { background: #1d2740; }
  .stage.active { opacity: 1; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(124,156,255,.10); }
  .stage.active .ico { background: #223055; animation: pulse 1.4s infinite; }
  .stage.done .ico { background: rgba(88,224,192,.14); color: var(--accent-2); }
  .stage.bad { border-color: var(--bad); }
  .stage.bad .ico { background: rgba(255,122,144,.14); color: var(--bad); }
  .summary { display: flex; align-items: center; gap: 14px; padding: 14px 28px 0; }
  .summary .subj { font-size: 19px; font-weight: 600; }
  .summary .chip { font-size: 11.5px; padding: 3px 10px; border-radius: 999px;
                   border: 1px solid var(--line); color: var(--muted); letter-spacing: .04em; }
  .summary .chip.done { color: var(--accent-2); border-color: #2b4a3d; }
  .summary .chip.failed { color: var(--bad); border-color: #4a2b34; }
  .summary .chip.running { color: var(--accent); border-color: #2b3a63; animation: pulse 1.6s infinite; }
  .summary .chip.queued { color: var(--warm); border-color: #4a4029; }
  .summary .elapsed { margin-left: auto; color: var(--muted); font-family: var(--mono); font-size: 12.5px; }
  .flash { animation: flash 1.1s ease; }
  @keyframes flash { 0% { border-color: var(--accent-2); box-shadow: 0 0 0 6px rgba(88,224,192,.14) } 100% {} }
  .banner { margin-top: 14px; padding: 13px 15px; border-radius: 12px; border: 1px solid #4a2b34;
            background: linear-gradient(180deg, rgba(255,122,144,.10), rgba(255,122,144,.03));
            display: none; font-size: 13.5px; }
  .banner.show { display: block; }
  .banner b { color: var(--bad); }
  .logwrap { position: relative; }
  .logwrap .tag { position: absolute; top: 9px; right: 12px; font-size: 10.5px; letter-spacing: .12em;
                  text-transform: uppercase; color: var(--muted); background: #0c0f16; padding: 2px 7px;
                  border: 1px solid var(--line); border-radius: 6px; }
  .log { font-family: var(--mono); font-size: 12.2px; line-height: 1.55; background: #0c0f16;
         border: 1px solid var(--line); border-radius: 12px; padding: 13px 14px; height: 420px;
         overflow: auto; white-space: pre-wrap; color: #c8d0e4; }
  .log .hl { color: var(--accent-2); }
  .pr { margin-top: 14px; padding: 16px; border-radius: 13px; border: 1px solid #2b3a2f;
        background: linear-gradient(180deg, rgba(88,224,192,.09), rgba(88,224,192,.03)); display: none; }
  .pr.show { display: block; }
  .pr a { color: var(--accent-2); font-weight: 600; text-decoration: none; font-size: 15px; }
  .pr .meta { color: var(--muted); font-size: 12.5px; margin-top: 6px; }
  .tasks { display: flex; flex-direction: column; gap: 7px; margin-top: 4px; }
  .task { display: flex; gap: 10px; align-items: center; padding: 9px 11px; border-radius: 10px;
          border: 1px solid var(--line); background: #0f121a; cursor: pointer; font-size: 12.5px; }
  .task.sel { border-color: var(--accent); background: #141a2b; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
  .pill.done { color: var(--accent-2); border-color: #2b4a3d; }
  .pill.failed { color: var(--bad); border-color: #4a2b34; }
  .pill.running, .pill.queued { color: var(--warm); border-color: #4a4029; }
  .task .sub { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  footer { padding: 0 28px 30px; color: var(--muted); font-size: 12.5px; }
  code { font-family: var(--mono); color: #cfd6ea; }
</style>
</head>
<body>
<header>
  <div class="mark"><b>Dear Agent</b><span>live console</span></div>
  <div class="status">
    <span id="health"><span class="dot" id="health-dot"></span><span id="health-text">connecting…</span></span>
    <span id="clock"></span>
  </div>
</header>
<main>
  <section class="summary" style="grid-column: 1 / -1">
    <div class="subj" id="s-subject">Waiting for a message…</div>
    <span class="chip" id="s-state">idle</span>
    <span class="chip" id="s-attempts"></span>
    <span class="elapsed" id="s-elapsed"></span>
  </section>
  <section class="panel">
    <h2>📨 The mail the agent received</h2>
    <div class="body">
      <div class="envelope" id="envelope">
        <div class="row"><span class="k">From</span><span class="v" id="e-from">—</span></div>
        <div class="row"><span class="k">Subject</span><span class="v" id="e-subject">—</span></div>
        <div class="row"><span class="k">Repo</span><span class="v" id="e-repo">—</span></div>
        <div class="row"><span class="k">Base</span><span class="v" id="e-base">—</span></div>
        <div class="row"><span class="k">Verify</span><span class="v" id="e-verify">—</span></div>
        <div class="row"><span class="k">Task</span><span class="v" id="e-id">—</span></div>
        <div class="body-text" id="e-body">waiting for a message…</div>
      </div>
      <h2 style="border-top:1px solid var(--line); margin-top:6px">Queue</h2>
      <div class="tasks" id="tasks"></div>
    </div>
  </section>

  <section class="panel">
    <h2>🤖 What the agent does with it</h2>
    <div class="body">
      <div class="stages" id="stages"></div>
      <div class="banner" id="banner"></div>
      <div class="pr" id="pr">
        <a id="pr-link" href="#" target="_blank">draft pull request →</a>
        <div class="meta" id="pr-meta"></div>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>⌨️ The agent at work <span id="job-name" style="margin-left:auto;color:var(--muted);font-family:var(--mono);font-size:11px"></span></h2>
    <div class="body">
      <div class="logwrap">
        <span class="tag" id="log-tag">harness</span>
        <div class="log" id="log">waiting for a runner Job…</div>
      </div>
      <div class="meta" style="color:var(--muted);font-size:12px;margin-top:10px" id="log-note">
        stdout of the credential-free <code>harness</code> container: the coding agent reading the
        repository and editing files. It never sees the database, the git key or the forge token.
      </div>
    </div>
  </section>
</main>
<footer>
  Read-only demo aid: it polls <code>oc logs</code> and the queue over a port-forward and writes nothing.
</footer>

<script>
const STAGES = [
  {id: "received", ico: "📨", t: "Message received", s: "JMAP/IMAP poll → normalizer → dedupe"},
  {id: "queued", ico: "🗂️", t: "In the durable queue", s: "PostgreSQL, idempotent on the message id"},
  {id: "dispatched", ico: "🚀", t: "Runner Job created", s: "the sweep dispatches one Job per task"},
  {id: "coding", ico: "🤖", t: "Coding agent works", s: "harness container, worktree, no credentials"},
  {id: "verify", ico: "✅", t: "Verify gate", s: "the task's allowlisted command, in the harness"},
  {id: "pr", ico: "🔀", t: "Draft pull request", s: "a human reviews and lands it"},
];
const STAGES_HTML = STAGES.map(s => `
  <div class="stage" id="stage-${s.id}">
    <div class="ico">${s.ico}</div>
    <div><div class="t">${s.t}</div><div class="s">${s.s}</div></div>
    <div class="at" id="at-${s.id}"></div>
  </div>`).join("");
document.getElementById("stages").innerHTML = STAGES_HTML;

let selected = new URLSearchParams(location.search).get("task") || null;
let lastTaskId = null;
const fmt = (iso) => iso ? new Date(iso).toLocaleTimeString([], {hour12: false}) : "";
const elapsed = (from, to) => {
  if (!from) return "";
  const secs = Math.max(0, Math.round((new Date(to || Date.now()) - new Date(from)) / 1000));
  return secs < 60 ? secs + "s" : Math.floor(secs / 60) + "m " + (secs % 60) + "s";
};

function set(id, state, at) {
  const el = document.getElementById("stage-" + id);
  if (!el) return;
  el.classList.remove("on", "active", "done", "bad");
  if (state) el.classList.add(state);
  if (at) document.getElementById("at-" + id).textContent = fmt(at);
}

async function tick() {
  document.getElementById("clock").textContent = new Date().toLocaleTimeString([], {hour12: false});
  let s;
  try {
    const r = await fetch("/api/state" + (selected ? "?task=" + encodeURIComponent(selected) : ""));
    s = await r.json();
  } catch (e) {
    document.getElementById("health-dot").className = "dot bad";
    document.getElementById("health-text").textContent = "console offline";
    return;
  }
  const hd = document.getElementById("health-dot"), ht = document.getElementById("health-text");
  if (s.error) { hd.className = "dot bad"; ht.textContent = s.error; }
  else { hd.className = "dot live"; ht.textContent = (s.namespace || "") + " · queue " + (s.queue || "?"); }

  const t = s.task;
  if (!t) {
    document.getElementById("e-body").textContent = s.error || "waiting for a message…";
    return;
  }
  if (!selected) selected = t.id;
  const ev = Object.fromEntries((t.events || []).map(e => [e.kind, e.at]));
  document.getElementById("s-subject").textContent = t.subject || t.id;
  const chip = document.getElementById("s-state");
  chip.className = "chip " + t.state;
  chip.textContent = t.state;
  document.getElementById("s-attempts").textContent = t.attempts ? "attempt " + t.attempts : "";
  const end = ev["task.done"] || ev["task.failed"] || ev["task.rejected"];
  document.getElementById("s-elapsed").textContent =
    t.created_at ? elapsed(t.created_at, end) + (end ? " total" : " and counting") : "";
  if (lastTaskId && lastTaskId !== t.id) {
    const env = document.getElementById("envelope");
    env.classList.remove("flash"); void env.offsetWidth; env.classList.add("flash");
  }
  lastTaskId = t.id;
  document.getElementById("e-from").textContent = t.sender || "—";
  document.getElementById("e-subject").textContent = t.subject || "—";
  document.getElementById("e-repo").textContent = (t.repo_url || "—").replace(/^https:\/\//, "");
  document.getElementById("e-base").textContent = t.base_branch || "—";
  document.getElementById("e-verify").textContent = t.verify || "(none)";
  document.getElementById("e-id").textContent = t.id;
  document.getElementById("e-body").textContent = t.instructions || "(empty)";

  set("received", "done", ev["task.accepted"]);
  set("queued", t.state === "queued" ? "active" : "done", t.created_at);
  set("dispatched", t.job ? (t.job.running ? "active" : "done") : (t.state === "queued" ? "on" : ""), t.job ? t.job.created : null);
  set("coding", t.job && t.job.running ? "active" : (t.job ? "done" : "on"), t.job ? t.job.started : null);
  const v = ev["verify.passed"] || ev["verify.failed"];
  set("verify", ev["verify.failed"] ? "bad" : (ev["verify.passed"] ? "done" : (t.verify && t.job ? "on" : "")), v);
  set("pr", ev["task.done"] ? "done" : (t.state === "failed" ? "bad" : ""), ev["task.done"]);
  if (t.state === "failed") set("coding", "bad", ev["task.failed"]);

  const pr = document.getElementById("pr");
  if (t.pr_url) {
    pr.classList.add("show");
    document.getElementById("pr-link").href = t.pr_url;
    document.getElementById("pr-link").textContent = t.pr_url.replace("https://github.com/", "") + " →";
    document.getElementById("pr-meta").textContent =
      "draft · a human lands it" + (t.branch ? " · branch " + t.branch : "");
  } else pr.classList.remove("show");

  const banner = document.getElementById("banner");
  if (t.state === "failed") {
    const failure = (t.events.find(e => e.kind === "task.failed") || {}).data || {};
    banner.classList.add("show");
    banner.innerHTML = `<b>the run failed</b> — ${failure.failure || "see the log"}` +
      (failure.failure === "harness_failed" ? ". The coding agent exited non-zero: the reason is in its output on the right." : "") +
      (failure.failure === "verify_failed" ? ". The task's own verify command failed: the change is not good enough to publish." : "") +
      (failure.failure === "publish_failed" ? ". The change was made but the push or the pull request failed: a credential or the remote." : "");
  } else banner.classList.remove("show");

  document.getElementById("job-name").textContent = t.job ? t.job.name : "";
  const log = document.getElementById("log");
  if (t.log && t.log.length) {
    document.getElementById("log-tag").textContent = "harness";
    log.textContent = t.log.split("\n").slice(-90).join("\n");
    log.scrollTop = log.scrollHeight;
  } else if (t.control_log && t.control_log.length) {
    document.getElementById("log-tag").textContent = "control";
    log.textContent = t.control_log;
    log.scrollTop = log.scrollHeight;
  } else if (t.job) {
    log.textContent = "(the harness container has not printed yet)";
  }

  document.getElementById("tasks").innerHTML = (s.tasks || []).map(x => `
    <div class="task ${x.id === t.id ? "sel" : ""}" onclick="pick('${x.id.replace(/'/g, "")}')">
      <span class="pill ${x.state}">${x.state}</span>
      <span class="sub">${(x.subject || x.id).slice(0, 46)}</span>
      <span style="margin-left:auto;color:var(--muted);font-family:var(--mono);font-size:11px">${fmt(x.created_at)}</span>
    </div>`).join("");
}
function pick(id) { selected = id; history.replaceState(null, "", "?task=" + encodeURIComponent(id)); tick(); }
tick();
setInterval(tick, 1500);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- cluster access
@dataclass
class Cluster:
    """Read-only view of the cluster: the queue (via a port-forward) and the runner Jobs."""

    namespace: str = "dear-agent"
    local_port: int = 15432
    _dsn: str | None = None
    _forward: subprocess.Popen | None = field(default=None, repr=False)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- oc helpers ------------------------------------------------------------------------
    def oc(self, *args: str, timeout: int = 25) -> str:
        result = subprocess.run(
            ["oc", "-n", self.namespace, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.stdout

    def _database_url(self) -> str:
        if self._dsn:
            return self._dsn
        raw = self.oc(
            "get",
            "secret",
            "dear-agent-postgres",
            "-o",
            "jsonpath={.data.DATABASE_URL}",
        ).strip()
        import base64

        dsn = base64.b64decode(raw).decode() if raw else ""
        if not dsn:
            raise RuntimeError("cannot read DATABASE_URL from the cluster")
        self._dsn = dsn
        return dsn

    def start_forward(self) -> None:
        """Port-forward the database service so the console can read the queue."""
        if self._forward and self._forward.poll() is None:
            return
        self._forward = subprocess.Popen(
            [
                "oc",
                "-n",
                self.namespace,
                "port-forward",
                "svc/dear-agent-postgres",
                f"{self.local_port}:5432",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        import psycopg

        dsn = urllib.parse.urlparse(self._database_url())
        target = dsn._replace(netloc=f"{dsn.username}:{dsn.password}@127.0.0.1:{self.local_port}")
        with (
            psycopg.connect(urllib.parse.urlunparse(target), connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql, params)
            return cur.fetchall()

    # -- the view the page renders ---------------------------------------------------------
    def tasks(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT id, sender, subject, state, attempts, created_at, spec_repo_url, "
            "spec_base_branch, spec_instructions, spec_verify, branch, pr_url "
            "FROM dear_agent_task ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [
            {
                "id": r[0],
                "sender": r[1],
                "subject": r[2],
                "state": r[3],
                "attempts": r[4],
                "created_at": r[5].isoformat(),
                "repo_url": r[6],
                "base_branch": r[7],
                "instructions": r[8],
                "verify": r[9],
                "branch": r[10],
                "pr_url": r[11],
            }
            for r in rows
        ]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT at, kind, data FROM dear_agent_event WHERE task_id = %s ORDER BY id",
            (task_id,),
        )
        return [{"at": r[0].isoformat(), "kind": r[1], "data": r[2]} for r in rows]

    def jobs(self) -> list[dict[str, Any]]:
        raw = self.oc("get", "jobs", "-l", "app.kubernetes.io/component=runner", "-o", "json")
        try:
            items = json.loads(raw or "{}").get("items", [])
        except json.JSONDecodeError:
            return []
        jobs = []
        for item in items:
            annotations = item.get("metadata", {}).get("annotations") or {}
            conditions = item.get("status", {}).get("conditions") or []
            jobs.append(
                {
                    "name": item["metadata"]["name"],
                    "task_id": annotations.get("dearagent.dev/task-id"),
                    "created": item["metadata"].get("creationTimestamp"),
                    "started": item.get("status", {}).get("startTime"),
                    "running": not any(
                        c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                        for c in conditions
                    ),
                    "failed": any(
                        c.get("type") == "Failed" and c.get("status") == "True" for c in conditions
                    ),
                }
            )
        jobs.sort(key=lambda j: j.get("created") or "", reverse=True)
        return jobs

    def harness_log(self, job_name: str, tail: int = 90) -> str:
        return self.oc("logs", f"job/{job_name}", "-c", "harness", f"--tail={tail}", timeout=20)

    def control_log(self, job_name: str, tail: int = 12) -> str:
        """The last lines of the control container: it prints the PR link when it publishes."""
        return self.oc("logs", f"job/{job_name}", "-c", "control", f"--tail={tail}", timeout=20)

    def state(self, task_id: str | None) -> dict[str, Any]:
        """The whole page payload. Cached for a second so a fast poll does not hammer ``oc``."""
        with self._lock:
            now = time.time()
            cached = self._cache.get("state")
            if cached and now - cached[0] < 1.0 and cached[1].get("task", {}).get("id") == task_id:
                return cached[1]
            payload = self._build_state(task_id)
            self._cache["state"] = (now, payload)
            return payload

    def _build_state(self, task_id: str | None) -> dict[str, Any]:
        try:
            tasks = self.tasks()
        except Exception as exc:  # noqa: BLE001 - the page shows the reason
            return {"error": f"database unreachable: {type(exc).__name__}: {exc}", "tasks": []}

        if not tasks:
            return {"namespace": self.namespace, "queue": "empty", "tasks": []}
        chosen = next((t for t in tasks if t["id"] == task_id), tasks[0])
        jobs = self.jobs()
        job = next((j for j in jobs if j["task_id"] == chosen["id"]), None)
        chosen["events"] = self.events(chosen["id"])
        chosen["job"] = job
        chosen["log"] = self.harness_log(job["name"]) if job else ""
        chosen["control_log"] = self.control_log(job["name"]) if job and not job["running"] else ""
        running = sum(1 for t in tasks if t["state"] == "running")
        return {
            "namespace": self.namespace,
            "queue": f"{len(tasks)} shown · {running} running",
            "tasks": [{k: t[k] for k in ("id", "subject", "state", "created_at")} for t in tasks],
            "task": chosen,
        }


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    cluster: Cluster

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            params = urllib.parse.parse_qs(parsed.query)
            payload = self.cluster.state((params.get("task") or [None])[0])
            self._send(json.dumps(payload), "application/json")
            return
        self.send_error(404)

    def _send(self, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        return  # keep the demo terminal quiet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dear Agent live demo console (read-only)")
    parser.add_argument("--namespace", default=os.environ.get("NS", "dear-agent"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-port", type=int, default=15432, help="local port for the DB forward")
    parser.add_argument("--task", default=None, help="pin one task id instead of the newest")
    parser.add_argument("--no-forward", action="store_true", help="assume the forward already runs")
    args = parser.parse_args(argv)

    if shutil.which("oc") is None:
        print("oc not found on PATH", file=sys.stderr)
        return 2
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print(
            "psycopg is not importable: run this with the project's venv "
            "(.venv/bin/python scripts/demo_console.py)",
            file=sys.stderr,
        )
        return 2

    cluster = Cluster(namespace=args.namespace, local_port=args.db_port)
    if not args.no_forward:
        cluster.start_forward()
    Handler.cluster = cluster

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/" + (f"?task={args.task}" if args.task else "")
    print(f"Dear Agent console: {url}")
    print(f"  namespace {args.namespace} · Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
        if cluster._forward is not None:
            cluster._forward.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
