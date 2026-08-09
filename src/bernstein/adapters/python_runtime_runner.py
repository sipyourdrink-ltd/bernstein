"""Runner script for Generic Python-invoked agent runtime adapter (#2959).

Executed as a child process by PythonRuntimeAdapter. Drives the specified
Python agent runtime entrypoint in isolation within the task's workdir.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Python Agent Runtime Worker")
    parser.add_argument("--prompt", required=True, help="Task prompt")
    parser.add_argument("--model", default="gpt-4o", help="Model name")
    parser.add_argument("--workdir", required=True, help="Workdir path")
    parser.add_argument("--runtime-module", default="", help="Module name to import")
    parser.add_argument("--runtime-entrypoint", default="chat", help="Entrypoint function/method name")

    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()

    # Emit initial status
    print(json.dumps({"event": "start", "prompt": args.prompt, "model": args.model, "workdir": str(workdir)}))
    sys.stdout.flush()

    if args.runtime_module:
        try:
            module = importlib.import_module(args.runtime_module)
            entrypoint = getattr(module, args.runtime_entrypoint, None)
            if callable(entrypoint):
                result = entrypoint(prompt=args.prompt, model=args.model, workdir=workdir)
                print(json.dumps({"event": "result", "output": str(result), "status": "completed"}))
            else:
                err_msg = f"Entrypoint {args.runtime_entrypoint!r} not found in {args.runtime_module!r}"
                print(json.dumps({"event": "error", "error": err_msg}))
        except Exception as exc:
            print(json.dumps({"event": "error", "error": f"Failed executing {args.runtime_module}: {exc}"}))
    else:
        # Fallback reference execution
        print(json.dumps({"event": "result", "output": f"Executed prompt: {args.prompt}", "status": "completed"}))

    sys.stdout.flush()


if __name__ == "__main__":
    main()
