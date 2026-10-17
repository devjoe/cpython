#!/usr/bin/env python3
"""Bounded Pdb Ctrl+C probe. Standard library only; see ../README.md.

Public role: orchestrator. Private roles: disposable console helper and worker.
No Pdb resume command, signal handler, or tracing implementation is overridden.
"""

import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback


SCRIPT = Path(__file__).resolve()
PROMPT = "(Pdb) "
STEP_TIMEOUT = 12.0
CASE_TIMEOUT = 45.0
SLEEP_SECONDS = 2.5
BUSY_LIMIT = 9.0
_event_path = None
_observer = None
state = {}
debugger = None


class InfrastructureError(Exception):
    pass


def save(path, value):
    """Only read after the writer exits, except append-only event files."""
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def event(name, **fields):
    row = dict(event=name, pid=os.getpid(), monotonic_ns=time.monotonic_ns(), **fields)
    payload = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(_event_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def events(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # The last append can still be in progress. Never treat it as an ACK.
            continue
    return rows


def win_api():
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    api.GetConsoleProcessList.restype = wintypes.DWORD
    api.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    api.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    api.SetConsoleCtrlHandler.restype = wintypes.BOOL
    return api


def console_members(api):
    import ctypes
    from ctypes import wintypes

    buffer = (wintypes.DWORD * 64)()
    count = api.GetConsoleProcessList(buffer, len(buffer))
    if not count or count > len(buffer):
        raise InfrastructureError(
            f"GetConsoleProcessList unavailable/oversized: count={count}, "
            f"winerror={ctypes.get_last_error()}"
        )
    return sorted(buffer[:count])


class WindowsJob:
    """Own a kill-on-close Job. Helper waits until assignment before spawning."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IOCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.api = win_api()
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise InfrastructureError(f"CreateJobObjectW: {ctypes.get_last_error()}")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise InfrastructureError(f"Cannot configure owned Job: {error}")

    def assign(self, process):
        import ctypes
        # Popen keeps this owned process handle open. Do not reopen a recorded PID.
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise InfrastructureError(f"Cannot assign helper to owned Job: {ctypes.get_last_error()}")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def install_windows_observer():
    """Observe native CTRL_C_EVENT, then return FALSE to CPython's own handler.

    This callback runs on a Windows control-handler thread. It acknowledges only
    delivery, not resumability. Keep a global reference to prevent collection.
    """
    import ctypes
    from ctypes import wintypes

    global _observer
    api = win_api()
    # Undo the helper's inherited ignore attribute, without creating a process
    # group. CPython's SIGINT handler must remain enabled in the worker.
    if not api.SetConsoleCtrlHandler(None, False):
        raise InfrastructureError(f"Cannot enable Ctrl+C: {ctypes.get_last_error()}")
    signal.signal(signal.SIGINT, signal.default_int_handler)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def observer(control_type):
        if control_type == 0:
            try:
                # The callback precedes CPython's native handler. Sample the
                # main thread so a readiness print alone cannot pretend that
                # the signal arrived inside the busy loop or C sleep call.
                active = []
                frame = sys._current_frames().get(threading.main_thread().ident)
                while frame is not None:
                    active.append({"name": frame.f_code.co_name,
                                   "file": frame.f_code.co_filename,
                                   "line": frame.f_lineno})
                    frame = frame.f_back
                event("native_ctrl_c_ack", control_type=control_type, main_stack=active)
            except BaseException:
                pass  # Absence of ACK will be reported as infrastructure failure.
        return False

    _observer = observer
    if not api.SetConsoleCtrlHandler(ctypes.cast(observer, ctypes.c_void_p), True):
        raise InfrastructureError(f"Cannot observe Ctrl+C: {ctypes.get_last_error()}")
    return console_members(api)


def refresh_windows_observer():
    """Keep the non-consuming observer first after Pdb chooses its handler.

    Never call signal.signal here: continue may already have installed Pdb's
    Python handler. Only the observer's native registration order is refreshed.
    """
    import ctypes

    api = win_api()
    pointer = ctypes.cast(_observer, ctypes.c_void_p)
    if not api.SetConsoleCtrlHandler(pointer, False) or not api.SetConsoleCtrlHandler(pointer, True):
        raise InfrastructureError(f"Cannot refresh native observer: {ctypes.get_last_error()}")


def environment(backend):
    import bdb
    import pdb

    paths = {"pdb": pdb.__file__, "bdb": bdb.__file__}
    return {
        "version": sys.version,
        "version_tuple": list(sys.version_info[:3]),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "backend_requested": backend,
        "module_paths": paths,
        "module_sha256": {
            name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for name, path in paths.items()
        },
        "pdb_signature": str(inspect.signature(pdb.Pdb)),
        "harness_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
        "stdin_isatty": sys.stdin.isatty(),
        "stdout_isatty": sys.stdout.isatty(),
        "signal_transport": "CTRL_C_EVENT/isolated console" if os.name == "nt"
                            else "SIGINT/owned child PID",
    }


def probe_snapshot(label):
    """Called through an ordinary Pdb ! command; records both stack and state."""
    current = debugger.curframe
    live = []
    frame = sys._getframe()
    while frame is not None:
        live.append({"id": id(frame), "name": frame.f_code.co_name,
                     "file": frame.f_code.co_filename, "line": frame.f_lineno})
        frame = frame.f_back
    pdb_stack = [
        {"id": id(frame), "name": frame.f_code.co_name,
         "file": frame.f_code.co_filename, "line": line}
        for frame, line in debugger.stack
    ]
    event("snapshot", label=label, state=state.copy(),
          selected={"id": id(current), "name": current.f_code.co_name,
                    "line": current.f_lineno},
          live_work=any(item["id"] == state.get("work_id") and item["name"] == "work"
                        for item in live),
          live_outer=any(item["id"] == state.get("outer_id") and item["name"] == "outer"
                         for item in live),
          exception=repr(current.f_locals.get("__exception__")),
          return_value=repr(current.f_locals.get("__return__")),
          active_stack=live, pdb_stack=pdb_stack,
          sigint_handler=repr(signal.getsignal(signal.SIGINT)))


def work():
    token = object()
    state["work_id"] = id(sys._getframe())
    state["token_id"] = id(token)
    try:
        if os.name == "nt":
            refresh_windows_observer()
        event("work_ready", state=state.copy(), handler=repr(signal.getsignal(signal.SIGINT)),
              scenario=state["scenario"])
        if state["scenario"] == "busy":
            deadline = time.monotonic() + BUSY_LIMIT
            while not state["release"] and time.monotonic() < deadline:
                pass
            if not state["release"]:
                state["natural_timeout"] = True
        else:
            time.sleep(SLEEP_SECONDS)
        state["work_after"] = True
        event("work_completed", same_frame=id(sys._getframe()) == state["work_id"],
              same_token=id(token) == state["token_id"], state=state.copy())
        return 42
    finally:
        state["work_finally"] = True
        event("work_finally", state=state.copy())


def outer():
    state["outer_id"] = id(sys._getframe())
    result = work()
    state["outer_after"] = True
    event("outer_completed", result=result, same_frame=id(sys._getframe()) == state["outer_id"],
          state=state.copy())
    return result


def intended_signal_lines(scenario):
    source, first = inspect.getsourcelines(work)
    needles = ("while not state[", "                pass") if scenario == "busy" else ("time.sleep(SLEEP_SECONDS)",)
    return [first + index for index, line in enumerate(source)
            if any(needle in line for needle in needles)]


def worker(config):
    import pdb

    global _event_path, state, debugger
    root = Path(config["case_dir"])
    _event_path = root / "worker-events.jsonl"
    state = dict(scenario=config["scenario"], release=False, work_after=False,
                 work_finally=False, outer_after=False, natural_timeout=False)
    try:
        meta = environment(config["backend"])
        if os.name == "nt":
            meta["console_members"] = install_windows_observer()
        else:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        options = dict(readrc=False, stdin=sys.stdin, stdout=sys.stdout)
        if "backend" in inspect.signature(pdb.Pdb).parameters:
            options["backend"] = config["backend"]
        elif config["backend"] != "settrace":
            raise InfrastructureError("Requested Pdb backend is unavailable in this interpreter")
        debugger = pdb.Pdb(**options)
        debugger.use_rawinput = False  # Pipe command transport; signal is real OS Ctrl+C.
        meta["backend_actual"] = getattr(debugger, "backend", "settrace")
        meta["intended_signal_lines"] = intended_signal_lines(config["scenario"])
        if meta["backend_actual"] != config["backend"]:
            raise InfrastructureError("Pdb backend did not match the requested backend")
        save(root / "environment.json", meta)
        with (root / "pdb-implementation.txt").open("w", encoding="utf-8") as output:
            for name in ("__init__", "sigint_handler", "interaction", "_cmdloop",
                         "do_continue", "do_next", "do_until", "do_return"):
                method = getattr(pdb.Pdb, name)
                output.write(f"\n{inspect.getsourcefile(method)}:"
                             f"{inspect.getsourcelines(method)[1]} {name}\n")
                output.write(inspect.getsource(method))
        event("worker_ready", backend=meta["backend_actual"],
              console_members=meta.get("console_members"))
        try:
            result = debugger.runcall(outer)
        except KeyboardInterrupt:
            event("escaped_keyboard_interrupt", state=state.copy())
            traceback.print_exc()
        else:
            event("runcall_finished", result=result, state=state.copy())
        return 0
    except BaseException as exc:
        event("worker_infrastructure_error", error=repr(exc))
        traceback.print_exc()
        return 2


class Dialogue:
    """Reader thread makes pipe I/O bounded on both Windows and Linux."""
    def __init__(self, process, path):
        self.process = process
        self.items = queue.Queue()
        self.pending = ""
        self.log = Path(path).open("w", encoding="utf-8")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                data = self.process.stdout.read(1)
                if not data:
                    break
                self.items.put(data)
        finally:
            self.items.put(None)

    def _consume(self, timeout):
        try:
            value = self.items.get(timeout=timeout)
        except queue.Empty:
            return ""
        if value is not None:
            self.log.write(value)
            self.log.flush()
        return value

    def prompt(self, timeout=STEP_TIMEOUT):
        deadline = time.monotonic() + timeout
        while PROMPT not in self.pending:
            left = deadline - time.monotonic()
            if left <= 0:
                raise InfrastructureError("Timed out waiting for a debugger prompt")
            value = self._consume(min(left, 0.1))
            if value is None:
                raise InfrastructureError("Worker exited before a debugger prompt")
            self.pending += value
        before, self.pending = self.pending.split(PROMPT, 1)
        return before

    def command(self, command):
        self.log.write(f"\n>>> {command}\n")
        self.log.flush()
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def finish(self):
        """Continue exception propagation without restarting the debuggee."""
        deadline = time.monotonic() + STEP_TIMEOUT
        while time.monotonic() < deadline:
            value = self._consume(0.1)
            if value is None:
                self.reader.join(timeout=1)
                return
            self.pending += value
            if PROMPT in self.pending:
                _, self.pending = self.pending.split(PROMPT, 1)
                self.command("continue")
        raise InfrastructureError("Worker did not finish after resume")

    def close(self):
        self.reader.join(timeout=1)
        # Consume queued output through EOF after the owned child has exited.
        while True:
            try:
                value = self.items.get_nowait()
            except queue.Empty:
                break
            if value is not None:
                self.log.write(value)
        self.log.close()


def wait_event(path, name, process, timeout=STEP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in events(path):
            if row["event"] == name:
                return row
            if row["event"] == "worker_infrastructure_error":
                raise InfrastructureError(row["error"])
        if process.poll() is not None:
            raise InfrastructureError(f"Worker exited before {name}: {process.returncode}")
        time.sleep(0.01)
    raise InfrastructureError(f"Timed out waiting for {name}")


def classify(rows, transport):
    """No PASS from a prompt, an API return value, or natural completion."""
    snapshots = [r for r in rows if r["event"] == "snapshot" and r["label"] == "interrupted"]
    ack = any(r["event"] == "native_ctrl_c_ack" and r.get("control_type") == 0 for r in rows)
    if transport == "windows" and not ack:
        return "INFRASTRUCTURE_LIMITATION", "No worker native CTRL_C_EVENT acknowledgement"
    if not snapshots:
        return "INCONCLUSIVE", "No post-signal state snapshot"
    snap = snapshots[-1]
    completed = [r for r in rows if r["event"] == "work_completed"]
    outer_done = [r for r in rows if r["event"] == "outer_completed"]
    finished = [r for r in rows if r["event"] == "runcall_finished"]
    escaped = any(r["event"] == "escaped_keyboard_interrupt" for r in rows)
    receipt = ack or snap["exception"] != "None" or escaped
    # On POSIX continue, the helper additionally records Pdb's interrupt message.
    receipt = receipt or any(r["event"] == "pdb_sigint_message" for r in rows)
    safe_stop = (snap["live_work"] and not snap["state"]["work_finally"]
                 and not snap["state"]["work_after"])
    same_work = (len(completed) == 1 and completed[0]["same_frame"]
                 and completed[0]["same_token"] and not completed[0]["state"]["natural_timeout"]
                 and completed[0]["state"]["work_id"] == snap["state"]["work_id"]
                 and completed[0]["state"]["token_id"] == snap["state"]["token_id"]
                 and sum(r["event"] == "work_ready" for r in rows) == 1)
    same_outer = (len(outer_done) == 1 and outer_done[0]["same_frame"]
                  and outer_done[0]["result"] == 42
                  and outer_done[0]["state"]["outer_id"] == snap["state"]["outer_id"])
    if (receipt and safe_stop and same_work and same_outer and len(finished) == 1
            and finished[0]["result"] == 42 and not escaped):
        return "PASS_RESUMABLE", "Original live work frame and token resumed; original outer returned 42"
    if escaped and (snap["state"]["work_finally"] or not snap["live_work"]):
        return "BUG_REPRODUCED_UNWIND", "KeyboardInterrupt unwound work; continue did not resume it"
    return "FAIL_NOT_RESUMABLE", "Signal response did not satisfy original-frame resumption checks"


def child_environment(root):
    # Do not pass tokens, cloud credentials, or arbitrary runner environment to
    # the disposable processes. Windows needs its system/loader directories.
    keep = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC",
            "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(HOME=str(root), USERPROFILE=str(root), TERM="dumb", NO_COLOR="1")
    return env


def helper(config):
    """The only role allowed to generate Ctrl+C; root never attaches a console."""
    import ctypes

    global _event_path
    root = Path(config["case_dir"])
    _event_path = root / "helper-events.jsonl"
    process = None
    dialogue = None
    result = dict(command=config["command"], scenario=config["scenario"],
                  backend=config["backend"], helper_pid=os.getpid())
    worker_events = root / "worker-events.jsonl"
    try:
        # Root releases this gate only after the Windows kill-on-close Job has
        # been assigned. No worker can escape ownership during startup.
        deadline = time.monotonic() + STEP_TIMEOUT
        while not (root / "launch-authorized").exists():
            if time.monotonic() >= deadline:
                raise InfrastructureError("Root never authorized isolated helper startup")
            time.sleep(0.01)
        api = None
        if os.name == "nt":
            api = win_api()
            members = console_members(api)
            event("helper_console_ready", members=members, parent_pid=config["parent_pid"])
            if set(members) != {os.getpid()} or config["parent_pid"] in members:
                raise InfrastructureError("New helper console was not exclusive; refusing CTRL_C_EVENT")
            if not api.SetConsoleCtrlHandler(None, True):
                raise InfrastructureError(f"Helper cannot ignore Ctrl+C: {ctypes.get_last_error()}")
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(SCRIPT), "_worker", str(root / "config.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=root, env=child_environment(root),
            # No CREATE_NEW_PROCESS_GROUP: worker inherits the helper's new
            # console and explicitly re-enables Ctrl+C at startup.
        )
        result["worker_pid"] = process.pid
        dialogue = Dialogue(process, root / "transcript.log")
        ready = wait_event(worker_events, "worker_ready", process)
        if os.name == "nt" and set(ready["console_members"]) != {os.getpid(), process.pid}:
            raise InfrastructureError("Worker did not join the exclusive helper console")
        dialogue.prompt()
        dialogue.command("next")
        dialogue.prompt()
        dialogue.command("!probe_snapshot('before')")
        dialogue.prompt()
        before = [r for r in events(worker_events) if r["event"] == "snapshot"][-1]
        if before["selected"]["name"] != "outer" or before["state"].get("work_id"):
            raise InfrastructureError("Initial stop was not immediately before the work() call")
        dialogue.command(config["command"])
        started = wait_event(worker_events, "work_ready", process)
        time.sleep(0.2)
        if process.poll() is not None or any(r["event"] == "work_finally" for r in events(worker_events)):
            raise InfrastructureError("Work ended before signal delivery")
        if os.name == "nt":
            members = console_members(api)
            if set(members) != {os.getpid(), process.pid}:
                raise InfrastructureError(f"Unexpected console members {members}; refusing broadcast")
            event("signal_requested", transport="CTRL_C_EVENT", group=0, members=members)
            sent_ns = time.monotonic_ns()
            ok = api.GenerateConsoleCtrlEvent(0, 0)
            event("signal_api_return", success=bool(ok), winerror=0 if ok else ctypes.get_last_error())
            if not ok:
                raise InfrastructureError("GenerateConsoleCtrlEvent failed")
            ack = wait_event(worker_events, "native_ctrl_c_ack", process, timeout=5)
            if ack["pid"] != process.pid or ack["monotonic_ns"] < sent_ns or ack["control_type"] != 0:
                raise InfrastructureError("Native Ctrl+C ACK did not match this delivery")
            if not any(frame["name"] == "work" and frame["file"] == str(SCRIPT)
                       and frame["line"] in intended_signal_lines(config["scenario"])
                       for frame in ack["main_stack"]):
                raise InfrastructureError("Ctrl+C arrived outside the intended busy/sleep region")
        else:
            event("signal_requested", transport="SIGINT", target_pid=process.pid)
            sent_ns = time.monotonic_ns()
            os.kill(process.pid, signal.SIGINT)
            event("signal_api_return", success=True)
        interruption = dialogue.prompt()
        result["signal_to_prompt_seconds"] = (time.monotonic_ns() - sent_ns) / 1e9
        result["interrupt_output"] = interruption
        result["signal_after_ready_seconds"] = (sent_ns - started["monotonic_ns"]) / 1e9
        dialogue.command("where")
        dialogue.prompt()
        dialogue.command("!probe_snapshot('interrupted')")
        dialogue.prompt()
        dialogue.command("!state['release'] = True")
        dialogue.prompt()
        dialogue.command("continue")
        dialogue.finish()
        process.wait(timeout=3)
        rows = events(worker_events)
        if "Program interrupted." in interruption:
            rows.append({"event": "pdb_sigint_message"})
            event("pdb_sigint_message")
        result["status"], result["reason"] = classify(rows, "windows" if os.name == "nt" else "posix")
        result["worker_returncode"] = process.returncode
        if process.returncode != 0:
            raise InfrastructureError(f"Worker exit code {process.returncode}")
        acks = [r for r in rows if r["event"] == "native_ctrl_c_ack"]
        if acks:
            result["signal_to_native_ack_seconds"] = (acks[0]["monotonic_ns"] - sent_ns) / 1e9
    except BaseException as exc:
        result.update(status="INFRASTRUCTURE_LIMITATION", reason=repr(exc))
        event("helper_error", error=repr(exc))
        traceback.print_exc()
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()  # Popen handle/PID of this helper's own child only.
            process.wait(timeout=5)
            if dialogue is not None:
                dialogue.close()
        save(root / "result.json", result)
    return 0


def run(args):
    if args.expected_version and platform.python_version() != args.expected_version:
        raise SystemExit(f"Wrong Python: expected {args.expected_version}, got {platform.python_version()}")
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in ("busy", "sleep"):
        for command in ("continue", "next", "until", "return"):
            case_dir = root / f"{scenario}-{command}"
            case_dir.mkdir()  # Refuse to overwrite old evidence or accept stale ACKs.
            config = dict(case_dir=str(case_dir), scenario=scenario, command=command,
                          backend=args.backend, parent_pid=os.getpid())
            save(case_dir / "config.json", config)
            options = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if os.name == "nt" else {"start_new_session": True}
            process = None
            job = None
            with (case_dir / "helper.log").open("w", encoding="utf-8") as log:
                try:
                    job = WindowsJob() if os.name == "nt" else None
                    process = subprocess.Popen(
                        [sys.executable, "-I", "-u", str(SCRIPT), "_helper", str(case_dir / "config.json")],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        cwd=case_dir, env=child_environment(case_dir), **options,
                    )
                    if job is not None:
                        job.assign(process)
                    (case_dir / "launch-authorized").touch()
                    process.wait(timeout=CASE_TIMEOUT)
                except (OSError, InfrastructureError, subprocess.TimeoutExpired) as exc:
                    save(case_dir / "result.json", dict(status="INFRASTRUCTURE_LIMITATION",
                         reason=f"Helper startup/timeout: {exc!r}", **config))
                finally:
                    if job is not None:
                        job.close()  # Only our assigned helper and its descendants.
                    if process is not None and process.poll() is None:
                        if os.name != "nt":
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        process.kill()  # Own Popen handle, including pre-assignment failures.
                        process.wait(timeout=5)
            result_path = case_dir / "result.json"
            if not result_path.exists():
                save(result_path, dict(status="INFRASTRUCTURE_LIMITATION",
                     reason=f"Helper exited without result: {process.returncode if process else 'not launched'}", **config))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            results.append(result)
            print(json.dumps(result), flush=True)
            save(root / "summary.json", dict(environment=environment(args.backend), results=results))
    statuses = [row["status"] for row in results]
    # Evidence collection can finish with an old bug reproduced. Default exit 1
    # makes that visible in Actions; code 2 distinguishes incomplete execution.
    if any(s in ("INFRASTRUCTURE_LIMITATION", "INCONCLUSIVE") for s in statuses):
        return 2
    return 0 if all(s == "PASS_RESUMABLE" for s in statuses) else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] in ("_helper", "_worker"):
        config = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        return helper(config) if sys.argv[1] == "_helper" else worker(config)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("settrace", "monitoring"), required=True)
    parser.add_argument("--expected-version", help="Require an exact installed runtime, e.g. 3.15.0")
    parser.add_argument("--output", required=True, help="Fresh evidence directory")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
