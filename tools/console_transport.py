"""Native Windows transport extracted from the previously verified probe.
Origin: devjoe/cpython commit 3eb108d14ce75ea8f0a98b95d4bfaf958e36068d.
Only ownership, console APIs, and non-consuming native observation are reused.
"""
import os, sys, json, time, threading, signal
from pathlib import Path
_event_path = None
_observer = None

class InfrastructureError(Exception):
    pass


def event(name, **fields):
    row = dict(event=name, pid=os.getpid(), monotonic_ns=time.monotonic_ns(), **fields)
    payload = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(_event_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


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


class NativeConsoleInput:
    """Input only for our already verified private console, never runner stdin."""
    def __init__(self, api):
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class Key(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD),
                        ("virtual", wintypes.WORD), ("scan", wintypes.WORD),
                        ("char", wintypes.WCHAR), ("control", wintypes.DWORD)]
        class Payload(ctypes.Union):
            _fields_ = [("key", Key), ("raw", ctypes.c_byte * 16)]
        class Input(ctypes.Structure):
            _fields_ = [("kind", wintypes.WORD), ("payload", Payload)]
        if ctypes.sizeof(Key) != 16 or ctypes.sizeof(Input) != 20:
            raise InfrastructureError("Unexpected Windows console input ABI")
        self.api = api
        self.Input = Input
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetConsoleMode.restype = wintypes.BOOL
        api.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.SetConsoleMode.restype = wintypes.BOOL
        api.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Input),
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        api.WriteConsoleInputW.restype = wintypes.BOOL
        handle = api.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise InfrastructureError(f"Cannot open private console input: {ctypes.get_last_error()}")
        self.handle = handle
        self.stream = os.fdopen(msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY), "rb", buffering=0)
        mode = wintypes.DWORD()
        if not api.GetConsoleMode(handle, ctypes.byref(mode)) or not api.SetConsoleMode(handle, mode.value | 3):
            self.stream.close()
            raise InfrastructureError("Cannot establish processed/line input in private console")
        self.mode = mode.value | 3

    def write(self, text):
        import ctypes
        from ctypes import wintypes
        records = (self.Input * (len(text) * 2))()
        for index, char in enumerate(text):
            char = "\r" if char == "\n" else char
            for offset, down in enumerate((True, False)):
                item = records[2 * index + offset]
                item.kind = 1  # KEY_EVENT; Ctrl+C itself is sent by the real API.
                item.payload.key.down = down
                item.payload.key.repeat = 1
                item.payload.key.virtual = 13 if char == "\r" else 0
                item.payload.key.char = char
        written = wintypes.DWORD()
        if not self.api.WriteConsoleInputW(self.handle, records, len(records), ctypes.byref(written)) or written.value != len(records):
            raise InfrastructureError("Incomplete input write to private console")

    def close(self):
        self.stream.close()
