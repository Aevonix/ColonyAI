"""``colony feeds`` — manage spec-driven feed instances.

Also runnable without an installed package (any python3 with PyYAML):
    PYTHONPATH=<repo>/sidecar python3 -m colony_sidecar.feeds.cli <cmd> ...
"""

from __future__ import annotations

import argparse
import json
import sys

from . import manager
from .spec import FeedSpec, SpecError


def build_parser(prog: str = "colony feeds") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Spec-driven intelligence feeds")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Instantiate a feed from a spec file")
    c.add_argument("spec", help="Path to the feed spec (.yaml or .json)")
    c.add_argument("--dry-run", action="store_true",
                   help="Validate and print rendered prompts without creating anything")

    v = sub.add_parser("validate", help="Validate a spec file and exit")
    v.add_argument("spec")

    sub.add_parser("list", help="List feed instances")

    for name, hlp in (("status", "Show one instance's queue/brief/job state"),
                      ("pause", "Pause all of an instance's jobs"),
                      ("resume", "Resume all of an instance's jobs")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("name")

    r = sub.add_parser("run", help="Run one stage of an instance now")
    r.add_argument("name")
    r.add_argument("stage", choices=["collect", "distill", "digest", "alert", "discovery"])

    d = sub.add_parser("delete", help="Remove an instance's jobs and shims")
    d.add_argument("name")
    d.add_argument("--purge", action="store_true", help="Also delete the instance data dir")
    return p


def main(argv: list[str] | None = None, prog: str = "colony feeds") -> None:
    args = build_parser(prog).parse_args(argv)
    try:
        if args.cmd == "validate":
            spec = FeedSpec.load(args.spec)
            print(f"OK: {spec.name} ({spec.title})")
        elif args.cmd == "create":
            result = manager.create(args.spec, dry_run=args.dry_run)
            if args.dry_run:
                for stage, prompt in result["prompts"].items():
                    print(f"===== {stage} prompt =====\n{prompt}\n")
                print(f"dry-run OK: {result['name']} -> {result['root']}")
            else:
                print(json.dumps(result, indent=2))
        elif args.cmd == "list":
            for inst in manager.list_instances():
                state = "paused" if inst.get("paused") else "active"
                print(f"{inst['name']:24} {state:7} jobs={','.join(inst['jobs'])}")
        elif args.cmd == "status":
            print(json.dumps(manager.status(args.name), indent=2))
        elif args.cmd == "pause":
            manager.pause(args.name)
            print(f"paused {args.name}")
        elif args.cmd == "resume":
            manager.resume(args.name)
            print(f"resumed {args.name}")
        elif args.cmd == "run":
            manager.run(args.name, args.stage)
        elif args.cmd == "delete":
            manager.delete(args.name, purge=args.purge)
            print(f"deleted {args.name}")
    except SpecError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main(prog="python3 -m colony_sidecar.feeds.cli")
