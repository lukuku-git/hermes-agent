#!/usr/bin/env python3
"""Local receipt probe using real tool dispatch and the shared finalizer.

No model, external send, install, restart, or profile configuration mutation.
Leaf tool failures and raw results are never printed.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.execution_receipts import ToolExecutionCollector, format_receipt_response
from model_tools import handle_function_call


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--brain-query")
    source.add_argument("--brain-read")
    parser.add_argument("--workspace-root", type=Path, default=Path("/Users/zeus/lukuku-os"))
    parser.add_argument("--petasos-root", type=Path, default=Path(__file__).resolve().parents[2] / "petasos")
    args = parser.parse_args(argv)
    roots = {"workspace_root": args.workspace_root, "petasos_root": args.petasos_root}
    if args.file is not None:
        tool_name = "read_file"
        tool_args = {"path": str(args.file.resolve()), "offset": 1, "limit": 40}
    else:
        tool_name = "terminal"
        operation = "query" if args.brain_query is not None else "read"
        target = args.brain_query if args.brain_query is not None else args.brain_read
        tool_args = {"command": shlex.join(["vat", "--workspace", str(args.workspace_root.resolve()),
                                            "brain", operation, target]), "timeout": 30}
    # Preflight only the bounded command/path allowlist. Reject sensitive file
    # paths before opening; no result is fabricated or printed as evidence.
    from agent.execution_receipts import _safe_relative, _vat_command
    if tool_name == "read_file":
        allowed = _safe_relative(tool_args["path"], **roots) is not None
    else:
        allowed = _vat_command(tool_args["command"], args.workspace_root) is not None
    if not allowed:
        print("허용된 조회 범위가 아닙니다.")
        return 2
    collector = ToolExecutionCollector()
    with collector.activate():
        handle_function_call(tool_name, tool_args, task_id="receipt-cli-probe",
                             tool_call_id="receipt-cli-probe-call", enabled_tools=[tool_name])
    response = format_receipt_response("조회 결과입니다.", collector.snapshot(), **roots)
    if response == "조회 결과입니다.":
        print("조회 근거를 확인하지 못했습니다.")
        return 1
    print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
