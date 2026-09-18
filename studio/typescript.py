"""MultiPL-E task adapter with explicit Node/CommonJS compilation.

Called only inside the network-isolated verifier container.
"""

import os
from pathlib import Path
import subprocess
import tempfile


def evaluate(source, tests, timeout=30):
    with tempfile.TemporaryDirectory(prefix="typescript-") as directory:
        root = Path(directory)
        path = root / "solution.ts"
        # MultiPL-E supplies this shim for a compiler without Node declarations.
        # The pinned @types/node package supplies the real declaration instead.
        tests = tests.replace("declare var require: any;", "")
        path.write_text(source + "\n" + tests)
        compiler = os.environ.get("BENCH_TSC", "/opt/bench-types/node_modules/.bin/tsc")
        types = os.environ.get("BENCH_TYPE_ROOTS", "/opt/bench-types/node_modules/@types")
        try:
            build = subprocess.run(
                [compiler, str(path), "--module", "commonjs", "--moduleResolution", "node",
                 "--target", "ES2022", "--types", "node", "--typeRoots", types,
                 "--skipLibCheck", "--outDir", str(root / "build"), "--noEmitOnError"],
                text=True, capture_output=True, timeout=timeout,
            )
            if build.returncode:
                log = build.stdout + build.stderr
                if "TS2688" in log or "TS6053" in log:
                    raise RuntimeError("TypeScript verifier dependencies unavailable: " + log[-2000:])
                return {"passed": False, "failure_kind": "compile_error", "detail": "TypeScript compilation failed", "log": log}
            # .cjs is explicit even if Node's automatic module detection changes.
            compiled = root / "build/solution.js"
            executable = compiled.with_suffix(".cjs")
            compiled.rename(executable)
            result = subprocess.run(["node", str(executable)], text=True, capture_output=True, timeout=timeout)
            return {"passed": result.returncode == 0,
                    "failure_kind": None if result.returncode == 0 else "test_failure",
                    "detail": "All tests passed" if result.returncode == 0 else "Executable tests failed",
                    "log": result.stdout + result.stderr}
        except subprocess.TimeoutExpired:
            return {"passed": False, "failure_kind": "timeout", "detail": "Verifier time limit exceeded", "log": "Timed out"}
