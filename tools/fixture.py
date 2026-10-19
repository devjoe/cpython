"""Disposable debuggee. Observes stock Pdb; never patches Pdb or its handlers."""
import asyncio
import importlib.util
import hashlib
import inspect
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import threading
import time
import traceback
import types
import pdb

ROOT = Path(sys.argv[1]).resolve().parent
CFG = json.loads(Path(sys.argv[1]).read_text())
STATE = dict(release=False, work_after=False, work_finally=False, outer_after=False,
             natural_timeout=False, main_interrupt=False, eval_after=False, eval_finally=False,
             partial_executed=False)
TASK = None
ENTRY_COUNT = 0
NATIVE = None


def emit(name, **fields):
    row = dict(event=name, pid=os.getpid(), thread=threading.get_ident(),
               ns=time.monotonic_ns(), **fields)
    fd = os.open(ROOT / "events.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(row) + "\n").encode())
    finally:
        os.close(fd)


def install_observer():
    global NATIVE
    if os.name == "nt":
        path = Path(__file__).with_name("console_transport.py")
        spec = importlib.util.spec_from_file_location("native", path)
        NATIVE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(NATIVE)
        NATIVE._event_path = ROOT / "signals.jsonl"
        members = NATIVE.install_windows_observer()
        emit("console_members", members=members)
    else:
        # Preserve CPython's actual SIGINT handler. set_wakeup_fd observes the
        # low-level signal path even when Pdb changes its Python handler.
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
        def observe():
            while True:
                if not select.select([read_fd], [], [], 0.1)[0]:
                    continue
                try:
                    data = os.read(read_fd, 4096)
                except BlockingIOError:
                    continue
                for number in data:
                    row = dict(event="posix_signal_ack", signal=number,
                               pid=os.getpid(), ns=time.monotonic_ns())
                    fd = os.open(ROOT / "signals.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                    try:
                        os.write(fd, (json.dumps(row) + "\n").encode())
                    finally:
                        os.close(fd)
        threading.Thread(target=observe, daemon=True, name="signal-observer").start()


def custom_handler(signum, frame):
    emit("custom_handler", signal=signum, frame=frame.f_code.co_name,
         raises=CFG["policy"].endswith("raise"))
    if CFG["policy"].endswith("raise"):
        raise KeyboardInterrupt


def snapshot(label):
    frame = sys._getframe()
    active = []
    instance = None
    postmortem = False
    while frame:
        candidate = frame.f_locals.get("self")
        if isinstance(candidate, pdb.Pdb) and instance is None:
            instance = candidate
        if isinstance(candidate, pdb.Pdb) and frame.f_code.co_name == "interaction":
            postmortem = postmortem or (frame.f_locals.get("frame", "missing") is None
                                       and frame.f_locals.get("traceback") is not None)
        active.append(dict(name=frame.f_code.co_name, line=frame.f_lineno,
                           file=frame.f_code.co_filename, id=id(frame)))
        frame = frame.f_back
    if instance is None:
        raise RuntimeError("No active Pdb found in snapshot command")
    selected = instance.curframe
    task = None if TASK is None else dict(id=id(TASK), done=TASK.done(),
                 cancelled=TASK.cancelled(), cancelling=TASK.cancelling())
    emit("snapshot", label=label, state=STATE.copy(), active=active,
         selected=dict(name=selected.f_code.co_name, line=selected.f_lineno, id=id(selected)),
         live_work=any(f["id"] == STATE.get("work_id") and f["name"] == "work" for f in active),
         live_outer=any(f["id"] == STATE.get("outer_id") and f["name"] == "outer" for f in active),
         backend=getattr(instance, "backend", "settrace"), mode=getattr(instance, "mode", None),
         handler=repr(signal.getsignal(signal.SIGINT)), task=task,
         postmortem=postmortem or any("post_mortem" in f["name"] for f in active),
         traceback_stack=[dict(name=f.f_code.co_name, line=line, id=id(f)) for f,line in instance.stack])


async def async_body():
    global TASK
    TASK = asyncio.current_task()
    emit("async_ready", task_id=id(TASK), handler=repr(signal.getsignal(signal.SIGINT)))
    deadline = time.monotonic() + 1.5
    try:
        while not STATE["release"] and not (ROOT / "release").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.04)
        emit("async_completed", task_id=id(asyncio.current_task()), same_task=TASK is asyncio.current_task())
        return 42
    except asyncio.CancelledError:
        emit("async_cancelled", task_id=id(TASK))
        raise
    finally:
        emit("async_finally", task_id=id(TASK))


def work():
    token = object()
    STATE["work_id"] = id(sys._getframe())
    STATE["token_id"] = id(token)
    if NATIVE is not None:
        NATIVE.refresh_windows_observer()
    emit("work_ready", state=STATE.copy(), handler=repr(signal.getsignal(signal.SIGINT)))
    try:
        scenario = CFG["scenario"]
        if scenario == "busy":
            deadline = time.monotonic() + 1.5
            while not STATE["release"] and not (ROOT / "release").exists() and time.monotonic() < deadline:
                pass
            STATE["natural_timeout"] = not STATE["release"] and not (ROOT / "release").exists()
        elif scenario == "sleep":
            time.sleep(0.7)
        elif scenario == "io":
            reader, writer = socket.socketpair()
            try:
                reader.settimeout(0.7)
                emit("io_ready", kind="socketpair_recv", timeout_seconds=0.7)
                try:
                    reader.recv(1)
                except TimeoutError:
                    emit("io_timeout")
            finally:
                reader.close()
                writer.close()
        elif scenario == "asyncio":
            asyncio.run(async_body())
        STATE["work_after"] = True
        emit("work_completed", same_frame=id(sys._getframe()) == STATE["work_id"],
             same_token=id(token) == STATE["token_id"], state=STATE.copy())
        return 42
    finally:
        STATE["work_finally"] = True
        emit("work_finally", state=STATE.copy())


def eval_work():
    emit("eval_ready")
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            pass
        STATE["eval_after"] = True
        emit("eval_completed")
    finally:
        STATE["eval_finally"] = True
        emit("eval_finally")


def outer():
    STATE["outer_id"] = id(sys._getframe())
    if CFG["entry"] == "breakpoint":
        breakpoint()
    elif CFG["entry"] == "set_trace":
        pdb.set_trace()
    result = work()  # CALL_SITE
    STATE["outer_after"] = True
    emit("outer_completed", result=result, same_frame=id(sys._getframe()) == STATE["outer_id"], state=STATE.copy())
    return result


def debug_target():
    try:
        if CFG["entry"] == "runcall":
            options = dict(readrc=False, nosigint=CFG["policy"].startswith("nosigint"))
            if "backend" in inspect.signature(pdb.Pdb).parameters:
                options["backend"] = CFG["backend"]
            elif CFG["backend"] != "settrace":
                emit("unsupported_backend")
                return
            result = pdb.Pdb(**options).runcall(outer)
        else:
            result = outer()
        emit("target_finished", result=result)
    except KeyboardInterrupt:
        emit("escaped_keyboard_interrupt", state=STATE.copy())
        if CFG["entry"] == "cli":
            raise
    except BaseException as exc:
        emit("target_exception", type=type(exc).__name__, detail=str(exc))
        raise
    finally:
        emit("target_end", handler=repr(signal.getsignal(signal.SIGINT)))
        # The daemon records the low-level wakeup byte without replacing the
        # Python handler. Give its final append time to finish before process exit.
        time.sleep(0.04)


ENTRY_COUNT += 1
observer_module = types.ModuleType("_pdb_probe_observer")
observer_module.snapshot = snapshot
observer_module.state = STATE
sys.modules[observer_module.__name__] = observer_module
emit("fixture_started", count=ENTRY_COUNT, version=sys.version,
     main_thread=threading.get_ident(), handler=repr(signal.getsignal(signal.SIGINT)),
     executable=sys.executable, pdb_path=pdb.__file__,
     stdin_isatty=sys.stdin.isatty(),
     source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in Path(__file__).parent.glob('*.py')})
if os.name != "nt":
    import termios
    attrs = termios.tcgetattr(0)
    emit("terminal_ready", sid=os.getsid(0), pgid=os.getpgrp(), foreground=os.tcgetpgrp(0),
         isig=bool(attrs[3] & termios.ISIG), vintr=list(attrs[6][termios.VINTR]))
install_observer()
if CFG["kind"] == "calibration":
    def calibration(signum, frame):
        emit("calibration_received", signal=signum)
        STATE["release"] = True
    signal.signal(signal.SIGINT, calibration)
    emit("calibration_ready")
    deadline = time.monotonic() + 3
    while not STATE["release"] and time.monotonic() < deadline:
        time.sleep(0.01)
    emit("calibration_end", received=STATE["release"])
elif CFG["kind"] == "worker_thread":
    finished = threading.Event()
    def worker_entry():
        try:
            debug_target()
        finally:
            finished.set()
    thread = threading.Thread(target=worker_entry, name="debug-worker")
    thread.start()
    # Avoid interruption of Thread.join itself (which has separate historical
    # lock/liveness edge cases); SIGINT is observed by the main thread's sleep.
    while not finished.is_set():
        try:
            time.sleep(0.03)
        except KeyboardInterrupt:
            STATE["main_interrupt"] = True
            emit("main_thread_interrupt")
            STATE["release"] = True
    thread.join(timeout=0.1)
    emit("program_end")
else:
    if "custom" in CFG["policy"]:
        signal.signal(signal.SIGINT, custom_handler)
    debug_target()
    emit("program_end")
