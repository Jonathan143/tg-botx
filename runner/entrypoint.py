"""Only copied into the disposable workload image. Never import in the Bot."""

from __future__ import annotations

import contextlib
import json
import sys


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            raise ValueError("input limit")
        payload = json.loads(raw)
        namespace = {"__name__": "__custom_command__"}
        # This executes unrestricted Python INSIDE the OS-isolated workload.
        # Redirection is protocol hygiene, not a sandbox/security mechanism.
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(payload["code"], "<custom-command>", "exec"), namespace)
            result = namespace["main"](payload["context"])
        if (
            not isinstance(result, dict)
            or set(result) - {"text", "data"}
            or not isinstance(result.get("text"), str)
        ):
            raise ValueError("invalid result")
        output = json.dumps(
            result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(output) > 16 * 1024:
            raise ValueError("output limit")
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
    except BaseException:
        # User code and exceptions can contain secrets; expose no traceback.
        sys.exit(1)


if __name__ == "__main__":
    main()
