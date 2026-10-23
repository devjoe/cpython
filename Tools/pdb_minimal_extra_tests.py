"""Supplemental observations for the frozen candidate; not proposed PR tests."""

import os
from pathlib import Path
import pdb
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager

from test.test_pdb import PdbSigintTests


class SupplementalTests(PdbSigintTests):
    @contextmanager
    def custom_session(self, backend, commands, cls):
        with self.session(backend, commands) as (base, output):
            debugger = cls(stdin=base.stdin, stdout=output, readrc=False,
                           backend=backend)
            try:
                yield debugger, output
            finally:
                debugger.stop_trace()

    def test_extra_repeated_interrupts(self):
        for backend in ('settrace', 'monitoring'):
            for command in ('step', 'next', 'until'):
                with self.subTest(backend=backend, command=command):
                    def function(marker='live'):
                        signal.raise_signal(signal.SIGINT)
                        signal.raise_signal(signal.SIGINT)
                        return 42

                    # The first resume finishes the interrupted CALL; the next
                    # prompt is on the second raise_signal line, before it runs.
                    commands = [command, 'p marker', command, command,
                                'p marker', 'continue']
                    with self.session(backend, commands) as (debugger, output):
                        self.assertEqual(debugger.runcall(function), 42,
                                         output.getvalue())
                        self.assertEqual(output.getvalue().count('Program interrupted'), 2)
                        self.assertEqual(output.getvalue().count("(Pdb) 'live'"), 2)

    def run_script(self, script, commands, *, cli=False):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'debuggee.py')
            path.write_text(textwrap.dedent(script), encoding='utf-8')
            args = [sys.executable, '-E', '-s']
            if cli:
                args += ['-m', 'pdb']
            args.append(str(path))
            proc = subprocess.run(args, input='\n'.join(commands) + '\n',
                                  text=True, capture_output=True, timeout=20,
                                  cwd=folder,
                                  env={**os.environ, 'PYTHON_COLORS': '0'})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn('KeyboardInterrupt', proc.stdout + proc.stderr)
            self.assertNotIn('NameError', proc.stdout + proc.stderr)
            return proc.stdout

    def test_extra_inline(self):
        for backend in ('settrace', 'monitoring'):
            for command in ('step', 'next', 'until'):
                with self.subTest(backend=backend, command=command):
                    script = f'''
                        import pdb, signal
                        debugger = pdb.Pdb(readrc=False, mode='inline',
                                           backend={backend!r})
                        def function(marker='live'):
                            debugger.set_trace()
                            signal.raise_signal(signal.SIGINT)
                            return 42
                        print('DONE', function())
                    '''
                    output = self.run_script(script, [
                        'next', command, 'p marker', 'continue'])
                    self.assertIn("(Pdb) 'live'", output)
                    self.assertIn('DONE 42', output)
                    self.assertEqual(output.count('Program interrupted'), 1)

    def test_extra_cli(self):
        script = '''
            import signal
            def function(marker='live'):
                signal.raise_signal(signal.SIGINT)
                return 42
            print('DONE', function())
        '''
        for command in ('step', 'next', 'until'):
            with self.subTest(backend='monitoring', command=command):
                output = self.run_script(script, [
                    'break function', 'continue', command,
                    'p marker', 'continue', 'quit'], cli=True)
                self.assertIn("(Pdb) 'live'", output)
                self.assertIn('DONE 42', output)
                self.assertEqual(output.count('Program interrupted'), 1)

    def test_extra_subclass_override(self):
        for backend in ('settrace', 'monitoring'):
            with self.subTest(backend=backend):
                calls = []

                class CustomPdb(pdb.Pdb):
                    def sigint_handler(self, signum, frame):
                        calls.append('custom')

                def function():
                    signal.raise_signal(signal.SIGINT)
                    return 42

                with self.custom_session(backend, ['continue', "p 'BREAKPOINT'",
                                                   'continue'], CustomPdb) as (debugger, output):
                    debugger.set_break(__file__, function.__code__.co_firstlineno + 2)
                    self.assertEqual(debugger.runcall(function), 42)
                    self.assertEqual(calls, ['custom'])
                    self.assertIn("(Pdb) 'BREAKPOINT'", output.getvalue())

    def test_extra_subclass_delegation(self):
        for backend in ('settrace', 'monitoring'):
            with self.subTest(backend=backend):
                class CustomPdb(pdb.Pdb):
                    def sigint_handler(self, signum, frame):
                        return super().sigint_handler(signum, frame)

                def function(marker='live'):
                    signal.raise_signal(signal.SIGINT)
                    return 42

                with self.custom_session(backend, ['step', 'p marker',
                                                   'continue'], CustomPdb) as (debugger, output):
                    self.assertEqual(debugger.runcall(function), 42,
                                     output.getvalue())
                    self.assertIn("(Pdb) 'live'", output.getvalue())


def suite():
    return unittest.TestSuite(SupplementalTests(name) for name in
                              sorted(SupplementalTests.__dict__)
                              if name.startswith('test_extra_'))
