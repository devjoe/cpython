"""Fork-only validation of immutable Pdb candidate 3c43411cc5."""

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
CANDIDATE = '3c43411cc5f5a9bf195c193df2a122219bf4259a'
ROOT = Path(__file__).resolve().parent.parent


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], timeout=30)


def run(command, source, logfile, timeout):
    print('Running:', ' '.join(map(str, command)), flush=True)
    env = {**os.environ, 'PYTHON_COLORS': '0', 'PYTHONNOUSERSITE': '1'}
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSAFEPATH'):
        env.pop(name, None)
    with logfile.open('wb') as output:
        result = subprocess.run(command, cwd=source, stdout=output,
                                stderr=subprocess.STDOUT, timeout=timeout,
                                env=env)
    print('\n'.join(logfile.read_text(errors='replace').splitlines()[-15:]), flush=True)
    return result.returncode


def build(args):
    source = args.source.resolve()
    assert git(source, 'rev-parse', 'HEAD').decode().strip() == BASE
    assert not git(source, 'status', '--porcelain').strip()
    if os.name == 'nt':
        commands = [['cmd.exe', '/d', '/c', str(source / 'PCbuild/build.bat'),
                     '-p', 'x64', '-c', 'Release', '-e']]
        executable = source / 'PCbuild/amd64/python.exe'
    else:
        commands = [['./configure', '--with-pydebug', '--without-ensurepip'],
                    ['make', '-j4']]
        executable = None
    for index, command in enumerate(commands):
        code = run(command, source, args.results / f'build-{index}.log', 1200)
        if code:
            raise RuntimeError(f'Build command failed with exit {code}')
    if executable is None:
        executable = next(source / name for name in ('python', 'python.exe')
                          if (source / name).is_file())
    write_json(args.results / 'build.json', {
        'base': BASE, 'commands': commands, 'executable': str(executable)})


class CaseResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        status = ('pass' if error is None else 'failure'
                  if issubclass(error[0], test.failureException) else 'error')
        self.cases.append({'test': test._testMethodName,
                           'parameters': dict(subtest.params), 'status': status})


def targeted(args):
    import pdb
    from test import test_pdb

    source = args.source.resolve()
    assert sys.version_info[:2] == (3, 16)
    assert Path(pdb.__file__).resolve() == source / 'Lib/pdb.py'
    assert Path(test_pdb.__file__).resolve() == source / 'Lib/test/test_pdb.py'
    if args.supplemental:
        path = ROOT / 'Tools/pdb_minimal_extra_tests.py'
        spec = importlib.util.spec_from_file_location('pdb_minimal_extra_tests', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = module.suite()
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_pdb.PdbSigintTests)
    result = unittest.TextTestRunner(verbosity=2, resultclass=CaseResult).run(suite)
    write_json(args.output, {
        'phase': args.phase, 'supplemental': args.supplemental,
        'version': sys.version, 'executable': sys.executable,
        'py_debug': sysconfig.get_config_var('Py_DEBUG'),
        'config_args': sysconfig.get_config_var('CONFIG_ARGS'),
        'pdb_sha256': digest((source / 'Lib/pdb.py').read_bytes()),
        'test_pdb_sha256': digest((source / 'Lib/test/test_pdb.py').read_bytes()),
        'methods_run': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skips': len(result.skipped),
        'cases': result.cases,
    })
    return int(not result.wasSuccessful())


def compare(args):
    source = args.source.resolve()
    assert git(source, 'rev-parse', 'HEAD').decode().strip() == BASE
    assert not (Path.home() / '.pdbrc').exists()
    assert not (source / '.pdbrc').exists()
    # The extra workflow and probes may change; all proposed source stays frozen.
    assert not git(ROOT, 'diff', CANDIDATE, 'HEAD', '--', 'Lib', 'Doc', 'Misc').strip()
    assert not git(source, 'diff', '--name-only', '--', '*.c', '*.h', 'configure',
                   'Makefile.pre.in', 'PCbuild').strip()
    executable = (args.python or Path(json.loads(
        (args.results / 'build.json').read_text())['executable'])).resolve()
    pdb_file, test_file = source / 'Lib/pdb.py', source / 'Lib/test/test_pdb.py'
    saved = pdb_file.read_bytes(), test_file.read_bytes()
    candidate = git(ROOT, 'show', f'{CANDIDATE}:Lib/pdb.py')
    tests = git(ROOT, 'show', f'{CANDIDATE}:Lib/test/test_pdb.py')
    original = git(source, 'show', f'{BASE}:Lib/pdb.py')
    assert git(ROOT, 'show', f'{CANDIDATE}:Lib/bdb.py') == git(source, 'show', f'{BASE}:Lib/bdb.py')
    summary = {'base': BASE, 'candidate': CANDIDATE,
               'harness_commit': git(ROOT, 'rev-parse', 'HEAD').decode().strip(),
               'same_native_build': True, 'phases': {}}
    try:
        test_file.write_bytes(tests)
        for phase, content in (('baseline', original), ('candidate', candidate)):
            pdb_file.write_bytes(content)
            for supplemental in (False, True):
                name = phase + ('-supplemental' if supplemental else '-core')
                output = args.results / (name + '.json')
                command = [str(executable), '-E', '-s', '-B', str(Path(__file__).resolve()),
                           '--source', str(source), '--results', str(args.results),
                           'targeted', '--phase', phase, '--output', str(output)]
                if supplemental:
                    command.append('--supplemental')
                code = run(command, source, args.results / (name + '.log'), 180)
                report = json.loads(output.read_text())
                assert report['pdb_sha256'] == digest(content)
                assert report['test_pdb_sha256'] == digest(tests)
                assert len(report['cases']) == (19 if supplemental else 50)
                assert {c['parameters']['backend'] for c in report['cases']} == {'settrace', 'monitoring'}
                mismatches = []
                if not supplemental:
                    for case in report['cases']:
                        should_fail = phase == 'baseline' and (
                            case['test'] in ('test_next_until', 'test_step') or
                            case['test'] == 'test_continue' and case['parameters']['breakpoint'])
                        expected = 'failure' if should_fail else 'pass'
                        if case['status'] != expected:
                            mismatches.append(case)
                summary['phases'][name] = {
                    'exit_code': code, 'cases': len(report['cases']),
                    'passed': sum(c['status'] == 'pass' for c in report['cases']),
                    'failures': report['failures'], 'errors': report['errors'],
                    'skips': report['skips'], 'contract_mismatches': mismatches}
        command = [str(executable), '-m', 'test', '-j2', '--timeout', '180',
                   '--junit-xml', str(args.results / 'pdb-bdb-remote.xml'), '-v',
                   'test_pdb', 'test_bdb', 'test_remote_pdb']
        summary['suite_exit_code'] = run(command, source, args.results / 'full-suite.log', 600)
        if sys.platform != 'win32':
            command = [str(executable), '-m', 'test', '-R', '3:3', '-v',
                       '-m', '*PdbSigintTests*', 'test_pdb']
            summary['refleak_exit_code'] = run(command, source,
                                               args.results / 'refleak.log', 180)
    finally:
        pdb_file.write_bytes(saved[0])
        test_file.write_bytes(saved[1])
        write_json(args.results / 'summary.json', summary)
    core = [summary['phases'][phase + '-core'] for phase in ('baseline', 'candidate')]
    core_ok = (core[0]['exit_code'] == 1 and core[1]['exit_code'] == 0
               and not any(p['errors'] or p['skips'] or p['contract_mismatches'] for p in core))
    # Supplemental failures stay visible as failures, never converted to passes.
    return int(not core_ok or summary['suite_exit_code'] != 0
               or summary.get('refleak_exit_code', 0) != 0
               or summary['phases']['candidate-supplemental']['exit_code'] != 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('runtime-source'))
    parser.add_argument('--results', type=Path, default=Path('validation-results'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build')
    comparison = commands.add_parser('compare')
    comparison.add_argument('--python', type=Path)
    tests = commands.add_parser('targeted')
    tests.add_argument('--phase', required=True)
    tests.add_argument('--output', type=Path, required=True)
    tests.add_argument('--supplemental', action='store_true')
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.results = args.results.resolve()
    args.results.mkdir(parents=True, exist_ok=True)
    if args.command == 'build':
        build(args)
        return 0
    return targeted(args) if args.command == 'targeted' else compare(args)


if __name__ == '__main__':
    sys.exit(main())
