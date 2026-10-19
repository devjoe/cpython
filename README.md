# Expanded Pdb Ctrl+C observation candidate

This standalone test tree observes stock Pdb across native entrypoints,
signal policies, and released/pinned-source runtimes. Published artifacts are
limited to disposable GitHub-hosted runner evidence and runtime identity.
Only the source allowlist belongs in this branch; evidence from other execution
environments must not be copied into it.

The probe changes only disposable test fixtures and their input transport. It
does not patch Pdb, Bdb, resume commands, `breakpointhook`, or CPython signal
handling. Ordinary Pdb commands collect snapshots and release bounded work.
A private observer module in the child exposes only fixture state/snapshot
helpers, including when Ctrl+C selects a library frame. No global interpreter
installation or system configuration is modified.

## Locked runtimes and practical matrix

`runtime-lock.json` pins official CPython main to
`999855b07f9b795ca88ac3349060a06c116a1037`, observed October 19, 2026, and fixes
the released control at **3.15.0**. Pinning identifies a future build input; it
does not claim either runtime was run locally in this phase.

The future candidate has six bounded jobs: two runtimes on Linux, Windows, and
macOS standard hosted runners.
The released job uses setup-python; main is checked out at the locked SHA,
verified before an isolated workspace build, and its imported Pdb path is
verified against that source. Builds omit PGO and installation, use two make
workers on POSIX, and have 15-minute build subprocess timeouts. Missing build
dependencies are infrastructure limitations; no sudo, package-manager install,
security-setting change, or privilege escalation is included in the candidate.

| Layer | Entrypoint/backend | Work/commands | Intended interpretation |
| --- | --- | --- | --- |
| Core | native `python -m pdb`, its actual backend | busy, sleep, bounded socketpair recv × c/n/until/return | Observe live-frame resume, unwind, PM, and delay separately |
| Core | `breakpoint()` and public `pdb.set_trace()` | same 12 cases each | Their native backend; do not pretend to select an unsupported one |
| Core | `Pdb(...).runcall()` | same 12 cases for each available backend | Explicit settrace/monitoring comparison |
| Prompt | every applicable entry/backend | partial input then Ctrl+C | Input must not execute; original outer frame must survive |
| Evaluation | every applicable entry/backend | cancel a bounded `!eval_work()` | Evaluation may unwind; debuggee frame must survive |
| asyncio | runcall, each available backend | asyncio.run/await loop × c/n/until/return | Task identity/cancellation and handler ownership, not blanket bug verdict |
| Handler policies | runcall, each available backend | busy × four commands × five policies | default+nosigint; returning/raising custom handler, with/without nosigint |
| Thread | runcall, each available backend | debugger in worker thread × four commands | Verify SIGINT is handled by main, not silently attributed to worker |
| Transport control | dedicated POSIX PTY | ISIG off then on | Writing a control byte alone must not count as signal delivery |

On the locked 3.15/main sources, both the CLI and public inline entries use
monitoring; the CLI constructor's own backend choice is read from installed
source and checked at its actual prompt. `Pdb` construction exposes both backends. On older interpreters,
the candidate records available capabilities and runs only native combinations.
It never substitutes a backend by monkeypatching a public entrypoint.

Expected modern coverage is **127 cases per POSIX job** and **126 per Windows
job**, totaling 760 across the six future jobs. Counts are stratified, not a
meaningless full Cartesian product. Windows partial-line cancellation uses the private console input buffer
(CONIN$ plus WriteConsoleInputW) rather than pipe stdin. The console membership
is checked before input injection, and Ctrl+C still uses CTRL_C_EVENT. Other
Windows cases use pipe command input. Dedicated `await pdb.set_trace_async()`, ConPTY/IDE/GUI terminals,
arbitrary C extensions, and a free-threaded build are deferred. Actual generated
cases/capabilities are saved by each run.

## Signal isolation and limits

Linux/macOS use `pty.fork()` followed immediately by exec. Before **every** Ctrl+C
byte, the parent verifies the child's private SID/PGID, PTY foreground group,
`ISIG`, and `VINTR=0x03`. The parent's process group must differ. Input is written
only to that PTY master; no SIGINT is sent to a shared session or service.
The child also records its terminal identity. A calibration first disables ISIG,
writes the same byte and verifies no signal receipt, flushes that private input
buffer, then enables ISIG and verifies actual receipt.

A nonblocking `signal.set_wakeup_fd` pipe observes the POSIX low-level signal
byte without replacing CPython's SIGINT handler. A disposable daemon appends
ACKs to a separate file. The fixture allows 40 ms for a final observer append
before process exit; the timestamp measures observation, not exact hardware
delivery latency. Custom-handler/main-thread events remain separate evidence.

Windows retains the console ownership code from the previous verified probe:
CREATE_NEW_CONSOLE, a private kill-on-close Job assigned before worker startup,
and an exact helper+worker console membership check before event-zero group-zero
broadcast. The helper ignores Ctrl+C; the worker clears the inherited ignore
attribute. A non-consuming native observer returns FALSE and acknowledges
CTRL_C_EVENT. Prompt cancellation opens only the verified private console input
and uses processed line input; this is classic console coverage, not ConPTY. No CTRL_BREAK/SIGBREAK or nonzero group shortcut is used. The generated artifacts identify the integration and runtime actually executed.

Work is bounded: busy/async loops 1.5 seconds, sleep/socket recv 0.7 seconds.
Each interactive session has a 20-second budget; the Windows parent allows 30
seconds for its disposable helper. The matrix has a 600-second scheduling budget;
unstarted cases after it are explicit NOT_RUN_BUDGET results. Future jobs have a
35-minute outer limit including builds. Cleanup is limited to owned Popen
handles, the private Windows Job, or the private POSIX child session.

## Evidence and semantic contracts

Every case saves configuration, transcript, transport checks, low-level/native
ACK, fixture events, selected frame, **active** call chain, traceback stack,
handler identity, actual backend, work/token/outer IDs, and relevant async task
state. Source hashes and interpreter/Pdb paths identify the input actually run.
Fresh output directories prevent stale ACKs or evidence reuse.

The main observation labels are LIVE_ORIGINAL_FRAME_RESUMED, UNWIND_PROPAGATED,
ASYNC_TASK_CANCELLED, CUSTOM_HANDLER_COMPLETED_WITHOUT_INTERRUPT_STOP,
MAIN_THREAD_SIGINT_WORKER_NOT_TARGETED, and prompt/evaluation cancellation.
Postmortem is an independent flag detected from Pdb's traceback-only interaction
or its PM call chain. A CLI restart prompt is quit before a second fixture
execution; multiple fixture starts are an infrastructure error. A traceback
containing a work frame never proves it is active.

Core/async unwind is an **observation**, not a universal failing assertion. In
particular, a returning custom handler, nosigint, worker-thread signal ownership,
or asyncio task cancellation can intentionally differ from resumable debugging.
Separate contracts check partial-input cancellation, evaluation preserving the
debuggee, actual handler selection/restoration, and main-thread ownership.
Exit codes: 0 = completed observations/contracts matched; 1 = specific contract
mismatch; 2 = infrastructure/receipt/budget problem. Thus a green observational
job is not evidence that the original resumability enhancement is implemented.

## Run and publication boundary

```text
python -I tools/test_expanded.py
python -I tools/expanded_probe.py --list
python -I tools/expanded_probe.py --suite smoke --output fresh-smoke
python -I tools/expanded_probe.py --suite full --output fresh-full
```

Use `--select SUBSTRING` for a bounded subset. No external Python package is
needed for execution. The workflow uses only contents:read, pinned actions,
standard runners, a dedicated `pdb-ctrlc-expanded/**` branch guard, no repository
secrets, and runner-only artifacts. The candidate includes a branch push trigger
because workflow_dispatch requires default-branch registration. Therefore
**publishing this workflow can start jobs** and requires repository-owner
authorization. Do not alter the existing PR,
master/default branch, repository settings, or upstream CI.

Only the files listed in the candidate manifest are intended for future
publication. Local evidence is separately packaged and may contain local paths.
The manifest, source review, and YAML checks do not replace
actual 3.15/main, macOS, or Windows execution.

## Primary references

- [Pdb public entrypoints/backends/nosigint](https://docs.python.org/3.15/library/pdb.html)
- [Signal handling and main-thread ownership](https://docs.python.org/3/library/signal.html)
- [asyncio Runner Ctrl+C cancellation](https://docs.python.org/3/library/asyncio-runner.html#handling-keyboard-interruption)
- [PTY](https://docs.python.org/3/library/pty.html), [termios](https://docs.python.org/3/library/termios.html)
- [Locked main Pdb source](https://github.com/python/cpython/blob/999855b07f9b795ca88ac3349060a06c116a1037/Lib/pdb.py)
- [GenerateConsoleCtrlEvent](https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent)
- [Creation of a Console](https://learn.microsoft.com/en-us/windows/console/creation-of-a-console)
