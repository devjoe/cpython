# Pdb Ctrl+C verification candidate

This standalone test tree probes whether native Ctrl+C can return Pdb to a
resumable original frame. It uses released CPython runtimes and disposable test
processes. Generated artifacts contain only GitHub-hosted runner test evidence.
Windows results must be read from actual run artifacts; a prompt or successful
signal API call is not a passing result.

## Intended runs

The workflow creates six independent jobs:

| OS | Exact CPython | Pdb backend | Cases |
| --- | --- | --- | --- |
| Windows Server 2022 | 3.12.3 | settrace | 8 |
| Windows Server 2022 | 3.15.0 | settrace | 8 |
| Windows Server 2022 | 3.15.0 | monitoring | 8 |
| Ubuntu 22.04 | 3.12.3 | settrace | 8 |
| Ubuntu 22.04 | 3.15.0 | settrace | 8 |
| Ubuntu 22.04 | 3.15.0 | monitoring | 8 |

Each job uses `actions/setup-python` to select an installed/released runtime;
there is no CPython checkout build. `--expected-version` rejects a fallback to
another patch version. The official [Python 3.15.0 release page](https://www.python.org/downloads/release/python-3150/)
identifies it as the stable release dated October 9, 2026. If the action's runtime
catalog lacks an exact version for an image, that job has an infrastructure
limitation, not a debugger result. A future CPython-main job can pass a built
interpreter to this script, but is intentionally absent from this candidate.

## Windows isolation and ownership

The orchestrator starts a disposable helper using **CREATE_NEW_CONSOLE**, never
CREATE_NEW_PROCESS_GROUP. The helper waits for a file gate until the orchestrator
assigns it to a private, unnamed Windows Job Object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Its worker inherits the Job. The root never
attaches to, detaches from, or sends a control event to the runner's console.

Before worker startup, `GetConsoleProcessList` must contain only the helper PID.
Before signal generation, it must contain exactly the helper and worker PIDs.
Unexpected membership, console-creation failure, missing console APIs, or failed
Job assignment aborts the case as `INFRASTRUCTURE_LIMITATION` without broadcasting
to a shared console. No breakaway flag is used.

The helper enables its own Ctrl+C ignore attribute. Its worker explicitly clears
the inherited ignore attribute with `SetConsoleCtrlHandler(NULL, FALSE)`, then
uses CPython's normal `signal.default_int_handler` before Pdb takes control.
After `work_ready` and a 0.2-second settling interval, the helper sends
`GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)` **inside the verified private console**.
Group zero is intentional: Microsoft documents that nonzero groups can return
success without receiving CTRL_C_EVENT. CTRL_BREAK_EVENT/SIGBREAK is never used.

A worker-native observer acknowledges receipt of event type zero, then returns
FALSE so CPython's own handler receives the event. It does not replace the Python
SIGINT handler or Pdb's resume commands. Its native registration is refreshed
after Pdb chooses a resume command so it observes before any native handler that
Pdb's signal registration may have installed. It also samples the main thread's stack
before CPython handles the event; the work frame must be at the intended busy-loop
or sleep-call line. This rejects a signal arriving during a readiness print.
Missing, wrong-PID, pre-delivery, wrong-location, or wrong-event-type ACKs are
infrastructure failures. The observer uses a ctypes callback
on the Windows control-handler thread; its effect on timing remains a limitation
until measured on Windows. Console member lists, request/API result/ACK times,
environment, actual backend, stack snapshots, transcript, and resumption state
are preserved per case.

If the helper stalls, closing the private Job terminates only its assigned
process tree. The code never reopens an arbitrary recorded PID for cleanup.
Normal cleanup also uses only the worker's owned Popen handle. On Linux the
helper has its own session; SIGINT is sent only to its owned worker PID, and
timeout cleanup is limited to that private session.

## What is tested and what PASS means

Commands are `continue`, `next`, `until` without a line argument, and `return`.
Each is issued at the `result = work()` line in `outer()`. The debuggee has either
a finite Python busy loop (maximum 9 seconds) or `time.sleep(2.5)`. The readiness
record confirms that the chosen command has actually entered the work function.
Pdb uses its real command loop with redirected pipes, `readrc=False`, isolated
HOME/USERPROFILE, and `python -I`. This tests native Windows console Ctrl+C event
delivery and debugger semantics; it does not test keyboard editing, ConPTY,
Windows Terminal, IDE terminals, or all C extensions.

The selected Pdb frame and the thread's **currently active** frame chain are
recorded separately. A traceback can retain a dead work frame; its presence in
`where` does not establish resumability. No postmortem wrapper or automatic
debuggee restart is used. Pdb may still stop at exception/return trace events
during KeyboardInterrupt propagation; those stops are captured rather than
counted as recovery.

`PASS_RESUMABLE` requires all of the following:

1. Actual signal receipt evidence, including a native event-zero ACK on Windows.
2. At the interrupted prompt, the original work frame is still in the active
   call chain, and neither its completion marker nor its `finally` has run.
3. An ordinary Pdb command changes a global release flag, then `continue`
   completes exactly one work call with the same frame ID and token ID.
4. The original outer frame completes and runcall returns 42, with no escaped
   KeyboardInterrupt and no busy-loop natural deadline being mistaken for resume.

`BUG_REPRODUCED_UNWIND` means work has unwound and KeyboardInterrupt escapes after
continue. `FAIL_NOT_RESUMABLE` records other completed but nonrecovering behavior.
`INFRASTRUCTURE_LIMITATION` and `INCONCLUSIVE` never mean fixed. A late prompt
after sleep can be resumable while still demonstrating the next-trace-event
limitation; compare the signal/ACK timestamp, prompt delay, and selected line.

Exit codes: **0** all eight cases pass; **1** at least one completed case is not
resumable; **2** at least one case is infrastructure-limited/inconclusive. Thus a
red job can be a successful reproduction of the old bug; read `summary.json`.
If runtime setup fails before the harness, use the setup step's Actions log and
do not infer anything about debugger behavior.

## Reproduce locally after reviewing the code

Use a fresh output directory each time; existing evidence is not overwritten.
No third-party Python packages are required.

```text
python -I tools/test_probe.py
python -I tools/pdb_ctrlc_probe.py --backend settrace --expected-version 3.12.3 --output probe-results-312
python -I tools/pdb_ctrlc_probe.py --backend settrace --expected-version 3.15.0 --output probe-results-315-settrace
python -I tools/pdb_ctrlc_probe.py --backend monitoring --expected-version 3.15.0 --output probe-results-315-monitoring
```

Invoke each command with the corresponding installed interpreter. Each prompt
wait is bounded to 12 seconds, native ACK to 5 seconds, and each disposable helper
to 45 seconds; the Actions job has a 12-minute outer limit. The subprocess
environment is allowlisted and excludes runner/cloud tokens and arbitrary
credentials. Environment logs list interpreter/module details, not environment
variables.

## Actions execution scope

The files under this directory are laid out as a standalone branch tree:
`.github/workflows/pdb-ctrlc-probe.yml`, `tools/`, and this README. A dedicated
**standalone test tree** containing only these files avoids running inherited
CPython workflows. The workflow itself accepts only a `pdb-ctrlc-probe/**` branch in
`devjoe/cpython`, for example `pdb-ctrlc-probe/2026-10-17`, and never runs on the
existing PR branch or a pull_request event. Use a fresh branch name within that scope for an independent probe.

Only `contents: read` is granted. Checkout does not persist credentials. There
are no configured-secret references, dependency installs, PR writes, comments,
or repository settings changes. All three actions are pinned to commit SHAs
resolved from their public Git refs on October 17, 2026. Artifacts upload even
when the probe step fails, with a 14-day retention.

GitHub [requires a workflow_dispatch workflow to exist on the default branch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
This candidate therefore also has a dedicated-branch push trigger. Initial
execution would occur on the authorized push; no default-branch change is
proposed. Publishing or running the workflow requires repository-owner authorization.

Fork Actions may require owner enablement. Repository push permission alone
does not guarantee workflow-write permission or Actions availability. Policies
may block pinned actions or runtime downloads, and hosted runners may restrict
console or Job APIs. Report such failures separately from debugger results;
do not change repository security settings to make a probe run.

## Primary references

- [GenerateConsoleCtrlEvent](https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent)
- [Creation of a Console](https://learn.microsoft.com/en-us/windows/console/creation-of-a-console)
- [SetConsoleCtrlHandler](https://learn.microsoft.com/en-us/windows/console/setconsolectrlhandler)
- [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)
- [CPython 3.15.0 Pdb source](https://github.com/python/cpython/blob/v3.15.0/Lib/pdb.py)
- [CPython 3.15.0 Bdb source](https://github.com/python/cpython/blob/v3.15.0/Lib/bdb.py)
