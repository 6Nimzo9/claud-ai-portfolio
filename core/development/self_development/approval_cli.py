from __future__ import annotations

import argparse
import json

from self_development_engine import SelfDevelopmentEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Claud AI self-development local control")
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="Review and approve/reject one exact pending action")
    review.add_argument("approval_id", type=int)
    sub.add_parser("pause", help="Pause agent actions")
    sub.add_parser("resume", help="Resume agent controller; queued tasks do not auto-run")
    stop = sub.add_parser("stop", help="Emergency stop agent actions")
    stop.add_argument("--reason", default="Emergency stop requested from local PowerShell")
    sub.add_parser("status", help="Show controller status")
    args = parser.parse_args()

    if args.command == "review":
        try:
            engine = SelfDevelopmentEngine()
            engine.approvals_cli(args.approval_id)
            return 0
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 1
    if args.command in {"pause", "resume", "stop"}:
        try:
            engine = SelfDevelopmentEngine()
            if args.command == "pause":
                print(engine.pause("Paused from local PowerShell"))
            elif args.command == "resume":
                print(engine.resume())
            else:
                print(engine.emergency_stop(args.reason))
            return 0
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 1
    if args.command == "status":
        try:
            engine = SelfDevelopmentEngine()
            print(json.dumps(engine.status(), ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
