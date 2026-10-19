"""Future CI preparation only: verify exact source identity, build in workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
LOCK = json.loads((ROOT / "runtime-lock.json").read_text())


def run(args, cwd=None, timeout=180):
    subprocess.run(args, cwd=cwd, check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("release", "main"), required=True)
    parser.add_argument("--source", default="runtime-source")
    parser.add_argument("--output", default="runtime-identity.json")
    args = parser.parse_args()
    if args.runtime == "release":
        expected = LOCK["release"]
        if ".".join(map(str, sys.version_info[:3])) != expected:
            raise SystemExit("Bootstrap interpreter is not the exact locked release")
        executable = Path(sys.executable).resolve()
        identity = dict(kind="release", expected_version=expected)
    else:
        source = Path(args.source).resolve()
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        if sha != LOCK["main_sha"]:
            raise SystemExit("Source SHA differs from immutable runtime lock")
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip():
            raise SystemExit("Refusing to build modified CPython source")
        if os.name == "nt":
            run(["cmd.exe", "/d", "/c", str(source / "PCbuild/build.bat"),
                 "-p", "x64", "-c", "Release", "-e"], cwd=source, timeout=900)
            executable = source / "PCbuild/amd64/python.exe"
        else:
            run(["./configure", "--without-ensurepip", "--disable-test-modules"], cwd=source)
            run(["make", "-j2"], cwd=source, timeout=900)
            executable = source / "python"
            if not executable.exists():
                executable = source / "python.exe"  # Case-insensitive macOS checkout.
        identity = dict(kind="main", expected_source_sha=LOCK["main_sha"], actual_source_sha=sha,
                        pdb_source_sha256=hashlib.sha256((source / "Lib/pdb.py").read_bytes()).hexdigest())
    code = "import sys,pdb,json;print(json.dumps({'version':sys.version,'pdb':pdb.__file__,'git':getattr(sys,'_git',None)}))"
    actual = json.loads(subprocess.check_output([str(executable), "-I", "-c", code], text=True))
    if args.runtime == "main" and Path(actual["pdb"]).resolve() != (source / "Lib/pdb.py").resolve():
        raise SystemExit("Built interpreter imported Pdb from a different runtime")
    identity.update(executable=str(executable), actual=actual)
    Path(args.output).write_text(json.dumps(identity, indent=2) + "\n")
    print(json.dumps(identity))


if __name__ == "__main__":
    main()
