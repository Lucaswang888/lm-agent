#!/usr/bin/env python3

"""Local browser UI for library migration requests."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import parse_qs, urlparse

import typer

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Library Migration Agent</title>
  <style>
    :root {
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f9;
      color: #1f2937;
    }
    body {
      margin: 0;
      padding: 32px;
    }
    main {
      max-width: 980px;
      margin: 0 auto;
    }
    h1 {
      font-size: 28px;
      margin: 0 0 8px;
    }
    p {
      margin: 0 0 20px;
      color: #4b5563;
      line-height: 1.55;
    }
    label {
      display: block;
      font-weight: 650;
      margin: 18px 0 8px;
    }
    textarea, input {
      box-sizing: border-box;
      width: 100%;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 12px 14px;
      font: inherit;
      background: white;
    }
    .path-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
    }
    .library-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    textarea {
      min-height: 120px;
      resize: vertical;
    }
    .actions {
      display: flex;
      gap: 12px;
      align-items: center;
      margin: 18px 0;
      flex-wrap: wrap;
    }
    .status {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin: 18px 0;
    }
    .step {
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 10px;
      background: white;
      color: #475569;
      font-weight: 650;
      text-align: center;
    }
    .step.active {
      border-color: #0f766e;
      background: #ccfbf1;
      color: #134e4a;
    }
    .step.done {
      border-color: #16a34a;
      background: #dcfce7;
      color: #166534;
    }
    .step.failed {
      border-color: #dc2626;
      background: #fee2e2;
      color: #991b1b;
    }
    button {
      border: 0;
      border-radius: 8px;
      background: #0f766e;
      color: white;
      font: inherit;
      font-weight: 700;
      padding: 12px 18px;
      cursor: pointer;
    }
    button:disabled {
      background: #94a3b8;
      cursor: wait;
    }
    button.secondary {
      background: #334155;
      white-space: nowrap;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #0f172a;
      color: #e2e8f0;
      border-radius: 8px;
      padding: 16px;
      min-height: 180px;
      line-height: 1.45;
    }
    .hint {
      font-size: 14px;
      color: #64748b;
    }
    @media (max-width: 720px) {
      body { padding: 18px; }
      .status { grid-template-columns: 1fr; }
      .path-row { grid-template-columns: 1fr; }
      .library-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main>
  <h1>Library Migration Agent</h1>
    <p>只需要提供仓库文件夹位置、原库和目标库。页面会生成 Agent 任务和可执行命令，避免浏览器长时间等待模型调用。</p>

  <form id="form">
    <label for="project">仓库文件夹位置</label>
    <div class="path-row">
      <input id="project" name="project" value="demo_projects/flask_weather_api">
      <button id="browse" class="secondary" type="button">浏览文件夹</button>
    </div>
    <div class="hint">可以手动输入路径，也可以点击“浏览文件夹”打开本机文件夹选择窗口。</div>

    <div class="library-row">
      <div>
        <label for="source">原库</label>
        <input id="source" name="source" value="flask">
      </div>
      <div>
        <label for="target">目标库</label>
        <input id="target" name="target" value="quart">
      </div>
    </div>
    <div class="hint">例如原库填 httplib2，目标库填 requests。</div>

    <div class="actions">
      <button id="preview" type="submit" name="mode" value="preview">预览 Agent 任务</button>
      <button id="command" type="submit" name="mode" value="command">生成执行命令</button>
      <button id="run" type="submit" name="mode" value="run">后台开始迁移</button>
      <button id="stop" type="button">停止迁移</button>
    </div>
  </form>

  <div class="status">
    <div class="step" id="step-queued">排队</div>
    <div class="step" id="step-pig">准备 PIG context</div>
    <div class="step" id="step-agent">Agent 迁移中</div>
    <div class="step" id="step-verify">Strict check</div>
  </div>

  <label for="output">输出</label>
  <pre id="output">等待运行。</pre>
</main>

<script>
const form = document.getElementById("form");
const buttons = [...document.querySelectorAll("button[type='submit']")];
const output = document.getElementById("output");
const projectInput = document.getElementById("project");
let submitter = "preview";
let currentJobId = null;
let pollTimer = null;
buttons.forEach((button) => {
  button.addEventListener("click", () => { submitter = button.value; });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  buttons.forEach((button) => { button.disabled = true; });
  output.textContent = "运行中...";
  const body = new URLSearchParams(new FormData(form));
  body.set("mode", submitter);
  try {
    const response = await fetch("/run", {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body
    });
    const data = await response.json();
    if (data.job_id) {
      currentJobId = data.job_id;
      output.textContent = data.output || "后台迁移任务已启动。";
      startPolling();
    } else {
      output.textContent = data.output;
    }
  } catch (error) {
    output.textContent = String(error);
  } finally {
    if (!currentJobId) buttons.forEach((button) => { button.disabled = false; });
  }
});

document.getElementById("stop").addEventListener("click", async () => {
  if (!currentJobId) return;
  await fetch(`/stop?id=${encodeURIComponent(currentJobId)}`, {method: "POST"});
  output.textContent += "\\n\\n停止请求已发送。";
});

document.getElementById("browse").addEventListener("click", async () => {
  const browseButton = document.getElementById("browse");
  browseButton.disabled = true;
  browseButton.textContent = "选择中...";
  try {
    const response = await fetch("/choose-folder", {method: "POST"});
    const data = await response.json();
    if (data.error) {
      output.textContent = data.error;
      return;
    }
    if (data.path) projectInput.value = data.path;
  } catch (error) {
    output.textContent = String(error);
  } finally {
    browseButton.disabled = false;
    browseButton.textContent = "浏览文件夹";
  }
});

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(fetchStatus, 1000);
  fetchStatus();
}

async function fetchStatus() {
  if (!currentJobId) return;
  const response = await fetch(`/status?id=${encodeURIComponent(currentJobId)}`);
  const data = await response.json();
  output.textContent = data.output || "";
  updateSteps(data.phase, data.status);
  if (["completed", "failed", "stopped"].includes(data.status)) {
    clearInterval(pollTimer);
    pollTimer = null;
    currentJobId = null;
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function updateSteps(phase, status) {
  const order = ["queued", "pig", "agent", "verify"];
  for (const key of order) {
    const element = document.getElementById(`step-${key}`);
    element.className = "step";
    if (status === "failed" && key === phase) element.classList.add("failed");
    else if (key === phase) element.classList.add("active");
    else if (order.indexOf(key) < order.indexOf(phase)) element.classList.add("done");
  }
}
</script>
</body>
</html>
"""


@app.command()
def main(
    host: Annotated[str, typer.Option("--host", help="Host to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="Port to bind.")] = 8765,
) -> None:
    """Start a local web UI with a natural-language input box."""
    server = ThreadingHTTPServer((host, port), _handler_factory(Path.cwd()))
    url = f"http://{host}:{port}"
    typer.echo(f"Library migration UI running at {url}")
    typer.echo("Open this URL in your browser. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nStopping UI.")
    finally:
        server.server_close()


def _handler_factory(cwd: Path) -> type[BaseHTTPRequestHandler]:
    jobs: dict[str, MigrationJob] = {}
    jobs_lock = threading.Lock()
    active_job_id: str | None = None

    def get_active_job_id() -> str | None:
        with jobs_lock:
            return active_job_id

    def set_active_job_id(job_id: str | None) -> None:
        nonlocal active_job_id
        with jobs_lock:
            active_job_id = job_id

    class MigrationUIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/status":
                query = parse_qs(parsed.query)
                job = _get_job(jobs, jobs_lock, _first(query, "id"))
                if not job:
                    self._send_json({"status": "missing", "phase": "queued", "output": "任务不存在。"})
                    return
                self._send_json(job.snapshot())
                return
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            self._send_html(HTML_PAGE)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/stop":
                query = parse_qs(parsed.query)
                job = _get_job(jobs, jobs_lock, _first(query, "id"))
                if job:
                    job.stop()
                    self._send_json(job.snapshot())
                else:
                    self._send_json({"status": "missing", "phase": "queued", "output": "任务不存在。"})
                return
            if parsed.path == "/choose-folder":
                self._send_json(_choose_folder(cwd))
                return
            if parsed.path != "/run":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            result = _run_migration_request(form, cwd, jobs, jobs_lock, get_active_job_id, set_active_job_id)
            self._send_json(result)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_html(self, value: str) -> None:
            data = value.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, value: dict[str, object]) -> None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return MigrationUIHandler


class MigrationJob:
    _MAX_OUTPUT_LINES = 500

    def __init__(self, job_id: str, args: list[str], cwd: Path, on_done: Callable[[], None]) -> None:
        self.job_id = job_id
        self.args = args
        self.cwd = cwd
        self.on_done = on_done
        self.status = "queued"
        self.phase = "queued"
        self.output = ""
        self._lines: list[str] = []
        self.process: subprocess.Popen[str] | None = None
        self.returncode: int | None = None
        self._lock = threading.Lock()
        self._append("后台迁移任务已启动。")

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        process: subprocess.Popen[str] | None
        with self._lock:
            if self.status in {"completed", "failed", "stopped"}:
                return
            self.status = "stopped"
            self._append("收到停止请求。")
            process = self.process
        if process and process.poll() is None:
            _terminate_process_tree(process)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            if self.process and self.status == "running" and self.process.poll() is not None:
                self.returncode = self.process.returncode
                self.status = "completed" if self.process.returncode == 0 else "failed"
            return {
                "job_id": self.job_id,
                "status": self.status,
                "phase": self.phase,
                "returncode": self.returncode,
                "output": self.output,
            }

    def _run(self) -> None:
        with self._lock:
            already_stopped = self.status == "stopped"
        if already_stopped:
            self.on_done()
            return
        self._set("running", "pig", "准备 PIG context。")
        self._set("running", "agent", "Agent 迁移中：正在调用模型、读取项目、准备修改。")
        try:
            self.process = subprocess.Popen(
                self.args,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self._append(line.rstrip())
                lowered = line.lower()
                if "pig-style migration context" in lowered or "code slices prepared" in lowered:
                    self._phase("pig")
                if "migration static/api verification" in lowered:
                    self._phase("verify")
            returncode = self.process.wait()
        except Exception as exc:
            self._set("failed", self.phase, f"任务异常：{exc}")
            self.on_done()
            return
        with self._lock:
            self.returncode = returncode
            if self.status == "stopped":
                self._append("任务已停止。")
            elif returncode == 0:
                self.status = "completed"
                self.phase = "verify"
                self._append("任务完成。")
            else:
                self.status = "failed"
                self._append(f"任务失败，退出码 {returncode}。")
        self.on_done()

    def _set(self, status: str, phase: str, line: str) -> None:
        with self._lock:
            self.status = status
            self.phase = phase
            self._append(line)

    def _phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def _append(self, line: str) -> None:
        self._lines.append(f"{_timestamp()} {line}")
        if len(self._lines) > self._MAX_OUTPUT_LINES:
            self._lines = self._lines[-self._MAX_OUTPUT_LINES :]
        self.output = "\n".join(self._lines) + "\n"


def _run_migration_request(
    form: dict[str, list[str]],
    cwd: Path,
    jobs: dict[str, MigrationJob],
    jobs_lock: threading.Lock,
    get_active_job_id: Callable[[], str | None],
    set_active_job_id: Callable[[str | None], None],
) -> dict[str, object]:
    project = _field(form, "project")
    source = _field(form, "source")
    target = _field(form, "target")
    if not project:
        return {"returncode": 2, "output": "请输入仓库文件夹位置。"}
    if not source:
        return {"returncode": 2, "output": "请输入原库。"}
    if not target:
        return {"returncode": 2, "output": "请输入目标库。"}

    mode = _field(form, "mode")
    args = [
        sys.executable,
        "-m",
        "minisweagent.run.migrate",
        "--project",
        project,
        "--source",
        source,
        "--target",
        target,
    ]
    pig_report, strict_report = _default_report_paths(project)
    args.extend(["--pig-report", str(pig_report)])
    args.extend(["--strict-report", str(strict_report)])
    args.append("--strict-static-check")
    if mode == "command":
        args.append("--yolo")
        return {
            "returncode": 0,
            "output": _render_command(args, cwd),
        }
    if mode == "run":
        args.append("--yolo")
        active_job_id = get_active_job_id()
        if active_job_id:
            active_job = _get_job(jobs, jobs_lock, active_job_id)
            if active_job and active_job.status in {"queued", "running"}:
                return {
                    "returncode": 0,
                    "job_id": active_job_id,
                    "output": "已有迁移任务正在运行，不会重复启动新进程。当前页面会继续显示这个任务的进度。",
                }
        job_id = datetime.now().strftime("%Y%m%d%H%M%S")
        job = MigrationJob(job_id, args, cwd, on_done=lambda: set_active_job_id(None))
        with jobs_lock:
            jobs[job_id] = job
        set_active_job_id(job_id)
        job.start()
        return {"returncode": 0, "job_id": job_id, "output": "后台迁移任务已启动。"}
    if mode != "run":
        args.append("--print-task")

    try:
        result = subprocess.run(args, cwd=cwd, check=False, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if exc.stderr:
            output = f"{output}\n\n[stderr]\n{exc.stderr}"
        output = (
            f"{output}\n\nMigration process timed out after 15 minutes. "
            "The migration may be waiting on a model/API configuration or a long-running test."
        )
        return {"returncode": 124, "output": output}
    output = result.stdout
    if result.stderr:
        output = f"{output}\n\n[stderr]\n{result.stderr}"
    return {"returncode": result.returncode, "output": output}


def _get_job(jobs: dict[str, MigrationJob], jobs_lock: threading.Lock, job_id: str) -> MigrationJob | None:
    with jobs_lock:
        return jobs.get(job_id)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    threading.Timer(2.0, _kill_process_tree_if_alive, args=(process,)).start()


def _kill_process_tree_if_alive(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()


def _first(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key, [])
    return items[0] if items else ""


def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def _choose_folder(cwd: Path) -> dict[str, object]:
    if sys.platform == "darwin":
        return _choose_folder_macos(cwd)
    return _choose_folder_tkinter(cwd)


def _choose_folder_macos(cwd: Path) -> dict[str, object]:
    script = (
        'POSIX path of (choose folder with prompt '
        '"选择要迁移的项目文件夹" default location '
        f'(POSIX file "{_escape_applescript(str(cwd.resolve()))}" as alias))'
    )
    try:
        result = subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"error": "选择文件夹超时，请重新点击浏览文件夹。"}
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if "User canceled" in message or "用户已取消" in message:
            return {"cancelled": True}
        return {"error": f"无法打开系统文件夹选择窗口：{message or 'unknown error'}"}
    path = result.stdout.strip().rstrip("/")
    if not path:
        return {"cancelled": True}
    return {"path": path}


def _choose_folder_tkinter(cwd: Path) -> dict[str, object]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return {"error": f"当前环境不能打开系统文件夹选择窗口：{exc}"}
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(initialdir=str(cwd.resolve()), title="选择要迁移的项目文件夹")
    finally:
        root.destroy()
    if not path:
        return {"cancelled": True}
    return {"path": path}


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _default_report_paths(project: str) -> tuple[Path, Path]:
    slug = _slug(Path(project).name or "project")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path("outputs/migration_ui")
    return root / f"{stamp}-{slug}-pig-context.json", root / f"{stamp}-{slug}-strict-report.json"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "project"


def _render_command(args: list[str], cwd: Path) -> str:
    command = " ".join(_shell_quote(arg) for arg in args)
    return (
        "复制下面命令到终端执行。它会真正调用模型并修改项目；如果运行很久，可以在终端按 Ctrl+C 停止。\n\n"
        f"cd {_shell_quote(str(cwd))}\n"
        f"PYTHONPATH=github_upload/mini-swe-agent-agent-code/src {command}"
    )


def _shell_quote(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_@%+=:,./-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _field(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key, [])
    return values[0].strip() if values else ""


if __name__ == "__main__":
    app()
