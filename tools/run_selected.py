"""Launch the interpreter recorded by the isolated build/release preparation."""
import json
from pathlib import Path
import subprocess
import sys

identity = json.loads(Path("runtime-identity.json").read_text())
script = Path(__file__).resolve().with_name("expanded_probe.py")
args = [identity["executable"], "-I", str(script), "--suite", "full", "--output", "expanded-results"]
if identity["kind"] == "release":
    args += ["--expected-version", identity["expected_version"]]
raise SystemExit(subprocess.run(args, timeout=630).returncode)
