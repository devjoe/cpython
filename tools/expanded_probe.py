"""Bounded observational Pdb matrix. No debugger implementation is patched."""
import argparse
import ast
import importlib.util
import inspect
import json
import os
from pathlib import Path
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
import traceback

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture.py"
TIMEOUT = 5


def load_native():
    spec = importlib.util.spec_from_file_location("console_transport", HERE / "console_transport.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def rows(path):
    if not Path(path).exists():
        return []
    result = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def event_rows(root, name):
    return [r for r in rows(root / "events.jsonl") if r["event"] == name]


def wait_event(root, name, session, seconds=TIMEOUT):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        matches = event_rows(root, name)
        if matches:
            return matches[-1]
        if session.poll() is not None:
            raise RuntimeError(f"Child exited before event {name}")
        session.pump(0.01)
    raise TimeoutError(f"No event {name}")


def environment(root):
    keep = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(HOME=str(root), USERPROFILE=str(root), TERM="dumb", NO_COLOR="1")
    return env


def command(cfg):
    args = [sys.executable, "-I", "-u"]
    if cfg["entry"] == "cli":
        args += ["-m", "pdb"]
    return args + [str(FIXTURE), str(Path(cfg["root"]) / "config.json")]


class Dialogue:
    def init_dialogue(self, root):
        self.pending = ""
        self.deadline = time.monotonic() + 20
        self.log = (root / "transcript.log").open("w", encoding="utf-8")

    def record(self, data):
        self.pending += data
        self.log.write(data)
        self.log.flush()

    def prompt(self, seconds=TIMEOUT):
        deadline = time.monotonic() + seconds
        while "(Pdb) " not in self.pending:
            if self.poll() is not None:
                self.pump(0)
                if "(Pdb) " not in self.pending:
                    return None
            if time.monotonic() >= deadline:
                return None
            self.pump(0.02)
        before, self.pending = self.pending.split("(Pdb) ", 1)
        return before

    def sendline(self, text):
        self.log.write("\n>>> " + text + "\n")
        self.log.flush()
        self.send((text + "\n").encode())

    def issue(self, text):
        self.sendline(text)
        result = self.prompt()
        if result is None:
            raise RuntimeError(f"No prompt after command {text}")
        return result


class PtySession(Dialogue):
    def __init__(self, cfg):
        import pty
        import termios
        self.root = Path(cfg["root"])
        self.status = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(self.root)
                os.execve(sys.executable, command(cfg), environment(self.root))
            except BaseException:
                os._exit(120)
        os.set_blocking(self.fd, False)
        self.init_dialogue(self.root)
        # Only this new PTY's echo is disabled; ISIG/canonical/VINTR are retained.
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def pump(self, seconds):
        if time.monotonic() > self.deadline:
            raise TimeoutError("Disposable PTY case exceeded 20-second budget")
        if select.select([self.fd], [], [], seconds)[0]:
            try:
                value = os.read(self.fd, 65536)
            except (BlockingIOError, OSError):
                return
            self.record(value.decode("utf-8", "replace"))

    def send(self, value):
        os.write(self.fd, value)

    def poll(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def terminal(self, require_isig=True):
        import termios
        attrs = termios.tcgetattr(self.fd)
        meta = dict(pid=self.pid, sid=os.getsid(self.pid), pgid=os.getpgid(self.pid),
                    foreground=os.tcgetpgrp(self.fd), isig=bool(attrs[3] & termios.ISIG),
                    vintr=list(attrs[6][termios.VINTR]), parent_pgid=os.getpgrp())
        assert meta["sid"] == meta["pgid"] == meta["foreground"] == self.pid, meta
        assert meta["parent_pgid"] != self.pid, meta
        assert meta["vintr"] == [3], meta
        if require_isig:
            assert meta["isig"], meta
        with (self.root / "transport.jsonl").open("a") as out:
            out.write(json.dumps(dict(event="pty_check", ns=time.monotonic_ns(), **meta)) + "\n")
        return meta

    def interrupt(self):
        self.terminal()
        self.send(b"\x03")

    def close(self):
        if self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
            self.status = -signal.SIGKILL
        os.close(self.fd)
        self.log.close()


class WindowsSession(Dialogue):
    def __init__(self, cfg, native):
        self.root = Path(cfg["root"])
        self.native = native
        self.api = native.win_api()
        before = native.console_members(self.api)
        if before != [os.getpid()]:
            raise RuntimeError(f"Helper console not exclusive: {before}")
        if not self.api.SetConsoleCtrlHandler(None, True):
            raise RuntimeError("Cannot isolate helper from Ctrl+C")
        self.console_input = native.NativeConsoleInput(self.api) if cfg['kind'] in ('prompt_cancel','plain_input') else None
        self.process = subprocess.Popen(command(cfg), cwd=self.root, env=environment(self.root),
                         stdin=self.console_input.stream if self.console_input else subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.pid = self.process.pid
        self.items = queue.Queue()
        self.init_dialogue(self.root)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while True:
            data = self.process.stdout.read(1)
            if not data:
                break
            self.items.put(data)

    def pump(self, seconds):
        if time.monotonic() > self.deadline:
            raise TimeoutError("Disposable console case exceeded 20-second budget")
        try:
            self.record(self.items.get(timeout=max(seconds, 0.001)).decode("utf-8", "replace"))
        except queue.Empty:
            pass
        while not self.items.empty():
            self.record(self.items.get_nowait().decode("utf-8", "replace"))

    def send(self, value):
        if self.console_input:
            members = self.native.console_members(self.api)
            if set(members) != {os.getpid(), self.pid}:
                raise RuntimeError("Unexpected console members before input write")
            self.console_input.write(value.decode('utf-8'))
        else:
            self.process.stdin.write(value)
            self.process.stdin.flush()

    def poll(self):
        return self.process.poll()

    def interrupt(self):
        members = self.native.console_members(self.api)
        if set(members) != {os.getpid(), self.pid}:
            raise RuntimeError(f"Unexpected console members; no broadcast: {members}")
        with (self.root / "transport.jsonl").open("a") as out:
            out.write(json.dumps(dict(event="windows_console_check", members=members,
                                     helper=os.getpid(), worker=self.pid, ns=time.monotonic_ns())) + "\n")
        if not self.api.GenerateConsoleCtrlEvent(0, 0):
            raise RuntimeError("GenerateConsoleCtrlEvent failed")

    def close(self):
        self.deadline = float("inf")
        if self.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)
        self.thread.join(timeout=1)
        self.pump(0)
        self.log.close()
        if self.console_input:
            self.console_input.close()


def snapshot(session, root, label):
    session.issue(f"!__import__('_pdb_probe_observer').snapshot({label!r})")
    matches = [r for r in event_rows(root, "snapshot") if r["label"] == label]
    if not matches:
        raise RuntimeError("Pdb snapshot command produced no evidence")
    return matches[-1]


def finish(session, root):
    # CLI may enter postmortem or restart at the first module line. Stop before
    # executing a second fixture run. Never mistake that fresh prompt for resume.
    session.sendline("continue")
    for index in range(12):
        text = session.prompt(seconds=3)
        if text is None:
            return
        if event_rows(root, "program_end"):
            # Native CLI has restarted its debugger at the first module line,
            # but has not executed the fixture again. Its old globals were
            # cleared, so do not invoke a stale observer function here.
            session.sendline("quit")
            return
        current = snapshot(session, root, f"finishing-{index}")
        if current["postmortem"] or event_rows(root, "target_end"):
            session.sendline("quit")
            return
        session.sendline("continue")
    raise RuntimeError("Too many finishing prompts")


def classify(cfg, data, signals):
    def named(name):
        return [r for r in data if r["event"] == name]
    kind = cfg["kind"]
    snap = [r for r in named("snapshot") if r["label"] == "interrupted"]
    receipt = any((r.get("control_type") == 0 or r.get("signal") == signal.SIGINT)
                  for r in signals if r["pid"] == cfg.get("child_pid"))
    result = dict(receipt=receipt, contract="observation", observation="UNCLASSIFIED",
                  postmortem_observed=any(r.get("postmortem", False) for r in named("snapshot")))
    if not receipt:
        return dict(result, observation="NO_SIGNAL_RECEIPT", contract="infrastructure")
    if kind == "calibration":
        return dict(result, observation="ISIG_OFF_NO_SIGNAL_THEN_ISIG_ON_SIGNAL", contract="match")
    if kind == "plain_input":
        cancelled = named("plain_input_cancelled")
        if cancelled:
            label = ("PLAIN_CONSOLE_INPUT_DEFERRED_UNTIL_LINE_COMPLETION" if cancelled[-1]["after_line_completion"]
                     else "PLAIN_CONSOLE_INPUT_CANCELLED_PROMPTLY")
        else:
            label = "PLAIN_CONSOLE_INPUT_RETURNED_WITHOUT_KEYBOARD_INTERRUPT"
        return dict(result, observation=label)
    if kind == "worker_thread":
        main = named("fixture_started")[0]["main_thread"]
        interrupts = named("main_thread_interrupt")
        completed = named("work_completed")
        okay = (len(interrupts) == len(completed) == 1 and interrupts[0]["thread"] == main
                and completed[0]["thread"] != main and completed[0]["same_frame"]
                and completed[0]["same_token"] and not named("escaped_keyboard_interrupt"))
        return dict(result, observation="MAIN_THREAD_SIGINT_WORKER_NOT_TARGETED" if okay
                    else "WORKER_THREAD_OTHER_RESPONSE", contract="match" if okay else "mismatch")
    done = named("work_completed")
    outers = named("outer_completed")
    complete = (len(done) == 1 and done[0]["same_frame"] and done[0]["same_token"]
                and len(outers) == 1 and outers[0]["result"] == 42)
    if kind in ("prompt_cancel", "eval_cancel"):
        before = [r for r in named("snapshot") if r["label"] == "before"][-1]
        intact = bool(snap and snap[-1]["live_outer"] and snap[-1]["selected"]["id"] == before["selected"]["id"])
        cancelled = kind == "prompt_cancel" or (named("eval_finally") and not named("eval_completed"))
        okay = intact and complete and bool(cancelled) and not snap[-1]["state"]["partial_executed"]
        return dict(result, observation=("PROMPT_CANCELLED_FRAME_PRESERVED" if kind == "prompt_cancel"
                      else "EVAL_CANCELLED_DEBUGGEE_PRESERVED") if okay else "CANCELLATION_CONTRACT_MISMATCH",
                    contract="match" if okay else "mismatch")
    live = bool(snap and snap[-1]["live_work"] and any(f["id"] == snap[-1]["state"]["work_id"]
                    and f["name"] == "work" for f in snap[-1]["active"]))
    if live and not snap[-1]["state"]["work_finally"] and not snap[-1]["state"]["work_after"] and complete:
        same = done[0]["state"]["work_id"] == snap[-1]["state"]["work_id"]
        same = same and done[0]["state"]["token_id"] == snap[-1]["state"]["token_id"]
        same = same and outers[0]["state"]["outer_id"] == snap[-1]["state"]["outer_id"]
        same = same and len(named("work_ready")) == 1 and not done[0]["state"]["natural_timeout"]
        if same and not named("escaped_keyboard_interrupt") and not named("async_cancelled"):
            result["observation"] = "LIVE_ORIGINAL_FRAME_RESUMED"
    elif named("async_cancelled"):
        result["observation"] = "ASYNC_TASK_CANCELLED"
    elif snap and snap[-1]["postmortem"]:
        result["observation"] = "POSTMORTEM_UNWOUND_FRAME"
    elif named("escaped_keyboard_interrupt") or (named("work_finally") and not done):
        result["observation"] = "UNWIND_PROPAGATED"
    elif named("custom_handler") and complete:
        result["observation"] = "CUSTOM_HANDLER_COMPLETED_WITHOUT_INTERRUPT_STOP"
    elif complete:
        result["observation"] = "NORMAL_COMPLETION_WITHOUT_LIVE_INTERRUPT_STOP"
    if kind == "policy":
        started = named("work_ready")[-1]
        custom_expected = "custom" in cfg["policy"] and (cfg["command"] != "continue" or cfg["policy"].startswith("nosigint"))
        handler_matches = ("custom_handler" in started["handler"]) if custom_expected else (
            "Pdb.sigint_handler" in started["handler"] if cfg["command"] == "continue" and not cfg["policy"].startswith("nosigint")
            else "default_int_handler" in started["handler"])
        result["contract"] = "match" if handler_matches else "mismatch"
        result["handler_at_work"] = started["handler"]
        before = [r for r in named("snapshot") if r["label"] == "before"][-1]
        if snap:
            result["handler_restored_at_prompt"] = snap[-1]["handler"] == before["handler"]
            if not result["handler_restored_at_prompt"]:
                result["contract"] = "mismatch"
    result["resumability_goal_achieved"] = result["observation"] == "LIVE_ORIGINAL_FRAME_RESUMED"
    return result


def execute_case(cfg, session):
    root = Path(cfg["root"])
    result = {k: cfg[k] for k in ("id", "entry", "backend", "scenario", "command", "kind", "policy")}
    cfg["child_pid"] = session.pid
    try:
        if cfg["kind"] == "plain_input":
            wait_event(root,"plain_input_ready",session)
            session.send(b"UNSUBMITTED_INPUT")
            time.sleep(0.1)
            session.interrupt()
            deadline=time.monotonic()+0.35
            while not event_rows(root,"plain_input_cancelled") and time.monotonic()<deadline:
                session.pump(0.01)
            if not event_rows(root,"plain_input_cancelled"):
                # Plain-input baseline only; never unblock Pdb input to pass it.
                (root/"input-line-completed").touch()
                session.send(b"\n")
            wait_event(root,"program_end",session)
            time.sleep(0.08)
        elif cfg["kind"] == "calibration":
            import termios
            wait_event(root, "calibration_ready", session)
            session.terminal()
            attrs = termios.tcgetattr(session.fd)
            attrs[3] &= ~termios.ISIG
            termios.tcsetattr(session.fd, termios.TCSANOW, attrs)
            session.terminal(require_isig=False)
            session.send(b"\x03")
            time.sleep(0.15)
            assert not event_rows(root, "calibration_received")
            assert not rows(root / "signals.jsonl")
            termios.tcflush(session.fd, termios.TCIFLUSH)
            attrs[3] |= termios.ISIG
            termios.tcsetattr(session.fd, termios.TCSANOW, attrs)
            session.interrupt()
            wait_event(root, "calibration_received", session)
            time.sleep(0.08)
        else:
            if session.prompt() is None:
                raise RuntimeError("No initial debugger prompt")
            call_line = next(i for i, line in enumerate(FIXTURE.read_text().splitlines(), 1) if "# CALL_SITE" in line)
            if cfg["entry"] == "cli":
                session.issue(f"break {FIXTURE}:{call_line}")
                session.issue("continue")
                # The setup breakpoint must be removed before issuing the tested
                # command so a second hit cannot masquerade as a SIGINT stop.
                session.issue("clear 1")
            else:
                initial = snapshot(session, root, "position")
                if initial["selected"]["line"] != call_line:
                    session.issue(f"until {call_line}")
            before = snapshot(session, root, "before")
            assert before["selected"]["name"] == "outer" and before["selected"]["line"] == call_line
            if before["backend"] != cfg["backend"]:
                raise RuntimeError(f"Actual backend {before['backend']} differs from selected {cfg['backend']}")
            result["backend_actual"] = before["backend"]
            if cfg["kind"] == "prompt_cancel":
                session.send(b"!__import__('_pdb_probe_observer').state.update(partial_executed=True)")
            elif cfg["kind"] == "eval_cancel":
                session.sendline("!eval_work()")
                wait_event(root, "eval_ready", session)
            else:
                session.sendline(cfg["command"])
                wait_event(root, "work_ready", session)
                if cfg["scenario"] == "io":
                    wait_event(root, "io_ready", session)
                if cfg["scenario"] == "asyncio":
                    wait_event(root, "async_ready", session)
            time.sleep(0.1)
            sent = time.monotonic_ns()
            session.interrupt()
            interrupted_text = session.prompt(seconds=0.35)
            if interrupted_text is None and "custom_return" in cfg["policy"]:
                (root / "release").touch()
                interrupted_text = session.prompt(seconds=2)
            if interrupted_text is None:
                interrupted_text = session.prompt(seconds=3)
            result["signal_to_prompt_seconds"] = (time.monotonic_ns() - sent) / 1e9
            result["interrupt_output"] = interrupted_text
            if interrupted_text is not None:
                interrupted = snapshot(session, root, "interrupted")
                session.issue("!__import__('_pdb_probe_observer').state['release'] = True")
                if interrupted["postmortem"]:
                    session.sendline("quit")
                else:
                    finish(session, root)
            # Preserve ACK evidence before terminating a disposable process.
            deadline = time.monotonic() + 0.2
            while not rows(root / "signals.jsonl") and time.monotonic() < deadline:
                session.pump(0.01)
            deadline = time.monotonic() + 1
            while session.poll() is None and time.monotonic() < deadline:
                session.pump(0.01)
        result.update(classify(cfg, rows(root / "events.jsonl"), rows(root / "signals.jsonl")))
        result["fixture_starts"] = len(event_rows(root, "fixture_started"))
        if result["fixture_starts"] != 1:
            result.update(observation="RESTART_DETECTED", contract="infrastructure")
    except BaseException as exc:
        result.update(observation="INFRASTRUCTURE_LIMITATION", contract="infrastructure", error=repr(exc))
        (root / "driver-error.txt").write_text(traceback.format_exc())
    finally:
        session.close()
    write_json(root / "result.json", result)
    return result


def matrix(full=True):
    import pdb
    backends = ["settrace", "monitoring"] if "backend" in inspect.signature(pdb.Pdb).parameters else ["settrace"]
    inline = "monitoring" if hasattr(pdb, "set_default_backend") else "settrace"
    result = []
    def add(kind, entry, backend, scenario="busy", command="continue", policy="default"):
        spec = dict(kind=kind, entry=entry, backend=backend, scenario=scenario, command=command, policy=policy)
        spec["id"] = "-".join(spec.values())
        result.append(spec)
    entries = [("cli", native_cli_backend()), ("breakpoint", inline), ("set_trace", inline)] + [("runcall", b) for b in backends]
    for entry, backend in entries:
        for scenario in ("busy", "sleep", "io"):
            for cmd in ("continue", "next", "until", "return"):
                if full or (entry == "runcall" and backend == "settrace") or (scenario == "busy" and cmd == "continue"):
                    add("core", entry, backend, scenario, cmd)
        for kind in ("prompt_cancel", "eval_cancel"):
            add(kind, entry, backend)
    for backend in backends:
        for cmd in ("continue", "next", "until", "return"):
            add("asyncio", "runcall", backend, "asyncio", cmd)
            add("worker_thread", "runcall", backend, "busy", cmd)
            for policy in ("nosigint", "custom_return", "nosigint_custom_return", "custom_raise", "nosigint_custom_raise"):
                if full or cmd == "continue":
                    add("policy", "runcall", backend, "busy", cmd, policy)
    if os.name != "nt":
        add("calibration", "none", "none")
    else:
        add("plain_input", "none", "none", "io")
    return result


def native_cli_backend():
    """Read the installed CLI's own constructor choice; never change it."""
    import pdb
    tree = ast.parse(inspect.getsource(pdb.main))
    explicit = {kw.value.value for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "Pdb"
                for kw in node.keywords if kw.arg == "backend" and isinstance(kw.value, ast.Constant)
                and kw.value.value in ("settrace", "monitoring")}
    if len(explicit) > 1:
        raise RuntimeError("CLI has multiple backend branches; explicit capability review needed")
    return next(iter(explicit)) if explicit else (pdb.get_default_backend() if hasattr(pdb,"get_default_backend") else "settrace")


def run_one(cfg):
    root = Path(cfg["root"])
    if os.name != "nt":
        return execute_case(cfg, PtySession(cfg))
    native = load_native()
    process = None
    job = None
    try:
        job = native.WindowsJob()
        with (root / "helper.log").open("w") as output:
            process = subprocess.Popen([sys.executable, "-I", "-u", str(Path(__file__).resolve()),
                         "_windows_helper", str(root / "config.json")], cwd=root, env=environment(root),
                         stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
            job.assign(process)
            (root / "job-assigned").touch()
            process.wait(timeout=30)
        if not (root / "result.json").exists():
            raise RuntimeError("Windows helper ended without result")
        return json.loads((root / "result.json").read_text())
    except BaseException as exc:
        value = dict(cfg, observation="INFRASTRUCTURE_LIMITATION", contract="infrastructure", error=repr(exc))
        write_json(root / "result.json", value)
        return value
    finally:
        if job is not None:
            job.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "_windows_helper":
        cfg = json.loads(Path(sys.argv[2]).read_text())
        root = Path(cfg["root"])
        deadline = time.monotonic() + 5
        while not (root / "job-assigned").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("No Job assignment authorization")
            time.sleep(0.01)
        execute_case(cfg, WindowsSession(cfg, load_native()))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--suite", choices=("smoke", "full"), default="full")
    parser.add_argument("--select", help="Substring of generated case ID")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--expected-version")
    parser.add_argument("--budget-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.expected_version and ".".join(map(str,sys.version_info[:3])) != args.expected_version:
        raise SystemExit("Interpreter version did not match exact requested version")
    cases = matrix(args.suite == "full")
    if args.select:
        cases = [c for c in cases if args.select in c["id"]]
    if args.list:
        print(json.dumps(cases, indent=2))
        return 0
    if not args.output or not cases:
        parser.error("Fresh --output and nonempty case selection required")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    import pdb
    write_json(output / "capabilities.json", dict(version=sys.version,
         explicit_backend="backend" in inspect.signature(pdb.Pdb).parameters,
         native_cli_backend=native_cli_backend(),
         inline_backend="monitoring" if hasattr(pdb,"set_default_backend") else "settrace",
         excluded=[],
         deferred=["await pdb.set_trace_async entry", "ConPTY and GUI/IDE terminals", "unbounded C extensions"],
         planned_cases=len(cases)))
    results = []
    deadline = time.monotonic() + args.budget_seconds
    for case in cases:
        root = output / case["id"]
        root.mkdir()
        cfg = dict(case, root=str(root))
        write_json(root / "config.json", cfg)
        if time.monotonic() > deadline:
            row = dict(case, observation="NOT_RUN_BUDGET", contract="infrastructure")
        else:
            row = run_one(cfg)
        results.append(row)
        print(json.dumps({k:v for k,v in row.items() if k not in ("interrupt_output", "root")}), flush=True)
        write_json(output / "summary.json", dict(version=sys.version, cases=results))
    return 2 if any(r["contract"] == "infrastructure" for r in results) else (
        1 if any(r["contract"] == "mismatch" for r in results) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
