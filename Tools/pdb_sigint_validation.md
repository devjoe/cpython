# Pdb SIGINT candidate validation

This fork branch compares a focused candidate with official CPython commit
`6044c6275dcfcb2f2fc49742920e4cfb094089a4`. It is an experimental review branch,
not an upstream pull request or a claim of maintainer acceptance.

The proposed library changes are in `Lib/pdb.py`, `Lib/test/test_pdb.py`,
`Doc/library/pdb.rst`, and the corresponding NEWS entry. The validation script,
this note, and `.github/workflows/pdb-sigint-candidate.yml` are fork-only CI
infrastructure and need not be included in a future upstream proposal.

## Comparison

The three jobs use standard Linux, macOS, and Windows runners. Each builds the
same immutable native baseline once. The regression tests are overlaid for
both phases; only `Lib/pdb.py` changes between baseline and candidate runs.
No native source or Bdb implementation changes. JSON reports verify the actual
interpreter, source SHA, Pdb/test hashes, the signal-handler change,
and both backends. There is no system Python installation.

There are 23 test methods and 157 explicitly recorded cases. The baseline
contract predicts 59 assertion failures and 98 passing controls. The candidate
must pass all 157 with no errors or skips. A baseline error, missing case,
unexpected pass/failure, or source mismatch is not accepted as reproduction.
The candidate then runs the complete `test_pdb`, `test_bdb`, and
`test_remote_pdb` suites. Their ordinary platform/dependency skips remain visible
in the logs and JUnit XML.

The tests exercise live locals and continuation, no premature `finally`,
replacement of the original stepping target, untraced callees, all command
aliases, continue with/without breakpoints, repeat interruption, signal
ownership and dispositions, handler replacement by debuggee code, two debugger
instances, recursive debug, non-main threads, prompt/evaluation interruption,
and normal command-line Pdb without postmortem fallback.

SIGINT is generated synchronously with `signal.raise_signal` in bounded code;
these tests do not claim to simulate a physical keyboard or a terminal input
implementation. Command streams are finite. Child commands and jobs have
timeouts. Direct regression Pdb instances use `readrc=False`; native CLI tests
retain the real CLI entry point and reject a pre-existing home/source `.pdbrc`.

## Why the extra safeguards are necessary

Merely sharing the old install block is insufficient while tracing is active:
the old handler calls `set_step()` before `set_trace()`, which can stop inside
Bdb. Removing that eager `set_step()` avoids changing the stop state before
`Bdb.set_trace()` stops tracing, resets the state, and enables instruction
stepping. No wrapper is needed, and overriding `sigint_handler` does not
implicitly disable tracing. Both the resume-command prompt window and the
trace-callback interruption remain covered by behavior tests.

The exact installed bound-method object is tracked through `weakref.ref`, not
`WeakMethod` (which recreates bound methods). This preserves identity comparisons
without retaining a debugger after the debuggee replaces its handler. A lifetime
test passes on the baseline, fails with a strong reference, and passes with the
weak reference.

The three new commands avoid installing a handler through the post-mortem API,
at an untraced run boundary selected with `up`, or when stepping out of the last
frame of a run session. This guard does not
apply to `continue`: inline debugging can still have live module code after a
return stop. Native inline tests check an actual resumable module-frame pause;
post-mortem tests check that the default handler remains installed. Metadata
fixtures explicitly establish a live-session boundary.

Before these changes, two inline cases and all twelve post-mortem command cases
failed against candidate `d1d4549`. The F1/F2-only wrapper variant passes those
cases, but fails both subclass-handler breakpoint controls. The simplified
handler passes all of them. These controls distinguish the revised behavior
from both the official baseline and the previous candidate.

## Scope and limitations

The candidate does not promise immediate preemption of arbitrary C code or
blocking I/O. It does not redesign asyncio cancellation, worker-thread signal
delivery, remote Pdb, or terminal prompt cancellation. In particular, earlier
Windows console-input observations are not attributed to Pdb by these tests.
Existing paths that finish without any later interaction, such as an initial
`continue` without breakpoints, are not a complete handler-lifetime redesign.
Unknown handlers installed by arbitrary C code are not a new restoration
guarantee. These limits must remain part of human review.

## Reproduction and artifacts

With a clean checkout of the locked official source in `runtime-source`:

```sh
python -I Tools/pdb_sigint_validation.py build
python -I Tools/pdb_sigint_validation.py compare
```

POSIX uses `--with-pydebug --without-ensurepip` and `make -j4`; Windows uses the
existing PCbuild x64 Release build with its normal external dependencies. The
test command and actual configuration are recorded. The helper restores the
runtime's original library/test files when comparison finishes.

Artifacts include build logs, phase-by-phase case JSON and logs, an aggregate
contract summary, and complete suite output/JUnit XML. Only fresh hosted-runner
evidence is uploaded. No private machine logs, environment dump, credentials,
or conversation transcript is part of this branch or its artifacts.

## Follow-up after independent review

Opus reviewed commit `97c5822401df7d802eb937e240fc40eabaf6cf87` and reported
no definite blocking issue. Its `up`-to-untraced-boundary example reproduced
handler residue on both backends. The follow-up adds a narrowly scoped guard
and six behavior cases; the reviewed commit fails all six. These later changes
are not claimed to have been reviewed by Opus.

The guard remains conservative at inline return boundaries. It is not a full
handler-lifetime model. CLI post-mortem calls `interaction` directly without
the API's reset, so its restart window can still retain a handler. The API
documentation is intentionally narrower. Recursive `debug` still uses its
existing `nosigint` default, and initial `continue` paths that never return to a
prompt retain their existing lifetime limitation. These are follow-up topics.
