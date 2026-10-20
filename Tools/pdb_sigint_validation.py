"""Bounded baseline/candidate validation for the fork's Pdb test branch."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import unittest


BASE = '6044c6275dcfcb2f2fc49742920e4cfb094089a4'
ROOT = Path(__file__).resolve().parent.parent


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_bytes(path):
    # Git checkouts on Windows can have CRLF; overlays use canonical LF bytes.
    return path.read_bytes().replace(b'\r\n', b'\n')


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], timeout=15)


def run(command, source, logfile, timeout):
    print('Running:', ' '.join(map(str, command)), flush=True)
    env = {**os.environ, 'PYTHON_COLORS': '0', 'PYTHONNOUSERSITE': '1'}
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSAFEPATH'):
        env.pop(name, None)
    with logfile.open('wb') as output:
        result = subprocess.run(command, cwd=source, stdout=output,
                                stderr=subprocess.STDOUT, timeout=timeout,
                                env=env)
    print('\n'.join(logfile.read_text(errors='replace').splitlines()[-20:]),
          flush=True)
    return result.returncode


def build(args):
    source = args.source.resolve()
    if git(source, 'rev-parse', 'HEAD').decode().strip() != BASE:
        raise RuntimeError('Build source differs from the locked baseline')
    if git(source, 'status', '--porcelain').strip():
        raise RuntimeError('Build source is not clean')
    if os.name == 'nt':
        commands = [['cmd.exe', '/d', '/c', str(source / 'PCbuild/build.bat'),
                     '-p', 'x64', '-c', 'Release', '-e']]
        executable = source / 'PCbuild/amd64/python.exe'
    else:
        commands = [['./configure', '--with-pydebug', '--without-ensurepip'],
                    ['make', '-j4']]
        executable = None
    for index, command in enumerate(commands):
        code = run(command, source, args.results / f'build-{index}.log', 900)
        if code:
            raise RuntimeError(f'Build command {index} failed with exit {code}')
    if executable is None:
        executable = next((source / name for name in ('python', 'python.exe')
                           if (source / name).is_file()
                           and os.access(source / name, os.X_OK)), None)
    if executable is None or not executable.is_file():
        raise RuntimeError('No built interpreter executable')
    write_json(args.results / 'build.json', {
        'source_sha': BASE,
        'commands': commands,
        'executable': str(executable),
    })


def parameter(value):
    if hasattr(value, 'name'):
        return value.name
    if callable(value):
        return value.__name__
    return value


class CaseResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        status = ('pass' if error is None else
                  'failure' if issubclass(error[0], test.failureException)
                  else 'error')
        self.cases.append({
            'test': test._testMethodName,
            'parameters': {k: parameter(v) for k, v in subtest.params.items()},
            'status': status,
        })


def targeted(args):
    import pdb
    from test import test_pdb

    source = args.source.resolve()
    if sys.version_info[:2] != (3, 16):
        raise RuntimeError('Regression tests require the built 3.16 runtime')
    if Path(pdb.__file__).resolve() != source / 'Lib/pdb.py':
        raise RuntimeError('Interpreter imported Pdb from a different source')
    if Path(test_pdb.__file__).resolve() != source / 'Lib/test/test_pdb.py':
        raise RuntimeError('Interpreter imported a different regression test')
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(test_pdb.PdbSigintTests),
        unittest.defaultTestLoader.loadTestsFromName(
            'test.test_pdb.PdbTestCase.test_sigint_cli_resume_commands'),
        unittest.defaultTestLoader.loadTestsFromName(
            'test.test_pdb.PdbTestInline.test_sigint_after_inline_return'),
    ])
    result = unittest.TextTestRunner(verbosity=2, resultclass=CaseResult).run(suite)
    write_json(args.output, {
        'phase': args.phase,
        'version': sys.version,
        'build_git': getattr(sys, '_git', None),
        'source_sha': git(source, 'rev-parse', 'HEAD').decode().strip(),
        'config_args': sysconfig.get_config_var('CONFIG_ARGS'),
        'py_debug': sysconfig.get_config_var('Py_DEBUG'),
        'pdb_sha256': digest((source / 'Lib/pdb.py').read_bytes()),
        'test_pdb_sha256': digest((source / 'Lib/test/test_pdb.py').read_bytes()),
        'optional_modules': {name: importlib.util.find_spec(name) is not None
                             for name in ('readline', '_ssl', 'zlib', '_ctypes')},
        'methods_run': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skips': len(result.skipped),
        'cases': result.cases,
    })
    return 0 if result.wasSuccessful() else 1


def baseline_failure(case):
    name = case['test']
    params = case['parameters']
    if name in ('test_sigint_resume_commands', 'test_sigint_repeated_resume',
                'test_sigint_recursive_debug', 'test_sigint_during_trace_callback',
                'test_sigint_cli_resume_commands'):
        return True
    if name == 'test_sigint_continue':
        return params['breakpoint']
    if name == 'test_sigint_handler_replaced':
        return params['command'] == 'continue'
    if name == 'test_sigint_handler_restored':
        return params['command'] != 'continue' or params['handler'] == 'SIG_DFL'
    return False


def compare(args):
    source = args.source.resolve()
    if (Path.home() / '.pdbrc').exists() or (source / '.pdbrc').exists():
        raise RuntimeError('Refusing to run CLI tests with a pre-existing .pdbrc')
    if git(source, 'rev-parse', 'HEAD').decode().strip() != BASE:
        raise RuntimeError('Runtime source differs from the locked baseline')
    native_changes = git(source, 'diff', '--name-only', '--', '*.c', '*.h',
                         'configure', 'Makefile.pre.in', 'PCbuild')
    if native_changes.strip():
        raise RuntimeError('Native build inputs have changed')
    executable = args.python
    if executable is None:
        executable = Path(json.loads((args.results / 'build.json').read_text())['executable'])
    executable = executable.resolve()
    pdb_file = source / 'Lib/pdb.py'
    test_file = source / 'Lib/test/test_pdb.py'
    saved_pdb, saved_test = pdb_file.read_bytes(), test_file.read_bytes()
    candidate = source_bytes(ROOT / 'Lib/pdb.py')
    tests = source_bytes(ROOT / 'Lib/test/test_pdb.py')
    original = git(source, 'show', f'{BASE}:Lib/pdb.py')
    bdb_original = git(source, 'show', f'{BASE}:Lib/bdb.py')
    if (source_bytes(ROOT / 'Lib/bdb.py') != bdb_original or
            source_bytes(source / 'Lib/bdb.py') != bdb_original):
        raise RuntimeError('Bdb differs from the baseline')
    handler = lambda data: data.split(b'\n    def sigint_handler(', 1)[1].split(b'\n    def ', 1)[0]
    expected_handler = handler(original).replace(
        b'        self.set_step()\n',
        b'        # set_trace() must stop tracing before changing the stop state.\n')
    if handler(candidate) != expected_handler:
        raise RuntimeError(
            'sigint_handler differs beyond removing eager set_step and explaining it')
    summary = {'base_sha': BASE, 'same_native_build': True,
               'sigint_handler_change': 'remove eager set_step before set_trace',
               'bdb_sha256_git_lf': digest(bdb_original),
               'bdb_sha256_raw': digest((source / 'Lib/bdb.py').read_bytes()),
               'phases': {}}
    try:
        test_file.write_bytes(tests)
        for phase, content in (('baseline', original), ('candidate', candidate)):
            pdb_file.write_bytes(content)
            output = (args.results / f'{phase}.json').resolve()
            output.unlink(missing_ok=True)
            command = [str(executable), '-E', '-s', str(Path(__file__).resolve()),
                       '--source', str(source), '--results', str(args.results),
                       'targeted', '--phase', phase, '--output', str(output)]
            code = run(command, source, args.results / f'{phase}.log', 90)
            report = json.loads(output.read_text())
            if report['pdb_sha256'] != digest(content):
                raise RuntimeError('Executed Pdb source hash differs from phase')
            if report['test_pdb_sha256'] != digest(tests):
                raise RuntimeError('Regression tests differ between phases')
            if len(report['cases']) != 157 or report['methods_run'] != 23:
                raise RuntimeError('Regression case or method count changed')
            if {c['parameters']['backend'] for c in report['cases']} != {'settrace', 'monitoring'}:
                raise RuntimeError('Both backends were not exercised')
            expected = lambda c: 'failure' if phase == 'baseline' and baseline_failure(c) else 'pass'
            mismatches = [c for c in report['cases'] if c['status'] != expected(c)]
            summary['phases'][phase] = {
                'exit_code': code,
                'cases': len(report['cases']),
                'passed': sum(c['status'] == 'pass' for c in report['cases']),
                'failures': report['failures'],
                'errors': report['errors'],
                'skips': report['skips'],
                'contract_mismatches': mismatches,
            }
        command = [str(executable), '-m', 'test', '-j2', '--timeout', '180',
                   '--junit-xml', str((args.results / 'pdb-bdb.xml').resolve()),
                   '-v', 'test_pdb', 'test_bdb', 'test_remote_pdb']
        summary['suite_exit_code'] = run(command, source,
                                         args.results / 'pdb-bdb.log', 420)
    finally:
        pdb_file.write_bytes(saved_pdb)
        test_file.write_bytes(saved_test)
        write_json(args.results / 'summary.json', summary)
    phases = summary['phases']
    return int(summary['suite_exit_code'] != 0 or
               phases['baseline']['exit_code'] != 1 or
               phases['candidate']['exit_code'] != 0 or any(
        p['errors'] or p['skips'] or p['contract_mismatches']
        for p in phases.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('runtime-source'))
    parser.add_argument('--results', type=Path, default=Path('candidate-results'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build')
    comparison = commands.add_parser('compare')
    comparison.add_argument('--python', type=Path)
    tests = commands.add_parser('targeted')
    tests.add_argument('--phase', required=True)
    tests.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.results = args.results.resolve()
    args.results.mkdir(parents=True, exist_ok=True)
    if args.command == 'build':
        build(args)
        return 0
    if args.command == 'targeted':
        return targeted(args)
    return compare(args)


if __name__ == '__main__':
    sys.exit(main())
