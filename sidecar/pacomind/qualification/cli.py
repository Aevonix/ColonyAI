"""Opt-in CLI. Inspect/compare are offline; evaluate invokes selected consumers."""
import asyncio
import json
from pathlib import Path


def add_parser(sub):
    parser = sub.add_parser('models', help='Inspect and qualify model bindings without selecting a deployment')
    commands = parser.add_subparsers(dest='models_command', required=True)
    for command in ('inspect', 'evaluate'):
        item = commands.add_parser(command)
        item.add_argument('binding', help='Existing model binding, or Hermes provider for the native suite')
        item.add_argument('--config', required=True, type=Path,
                          help='Private host model JSON, or Hermes config YAML for the native suite')
        item.add_argument('--suite', choices=['standard', 'native'], default='standard')
        item.add_argument('--hermes-python', type=Path,
                          help='Native suite interpreter; defaults to the configured instance.hermes_python')
        if command == 'evaluate':
            item.add_argument('--roles', help='Default: chat,extraction; native suite: chat')
            item.add_argument('--deadline-seconds', type=float,
                              help='Predeclared elapsed deadline for the native case (default 60)')
            item.add_argument('--cleanup-seconds', type=float,
                              help='Native interruption/exit allowance after cancellation (default 5)')
            item.add_argument('--evidence-mode', choices=['actual_inference', 'controlled'], default='actual_inference',
                              help='Label controlled fixtures separately; neither label attests serving weights')
            item.add_argument('--output', required=True, type=Path, help='New private result directory')
            item.add_argument('--resume', action='store_true', help='Continue untouched cases; interrupted attempts are never rerun')
    item = commands.add_parser('compare')
    item.add_argument('incumbent', type=Path)
    item.add_argument('candidate', type=Path)
    item.add_argument('--json', action='store_true')


def run(args):
    from .records import read, write_once, publish
    from .report import compare, summarize, markdown
    if args.models_command == 'compare':
        report = compare(args.incumbent, args.candidate)
        print(json.dumps(report, indent=2) if args.json else markdown(report))
        return 0
    from .runner import inspect_binding, router_for, evaluate
    native = getattr(args, 'suite', 'standard') == 'native'
    if native:
        from .native import configuration
        config, recipe = configuration(args.config, args.binding,
                                       hermes_python=getattr(args, 'hermes_python', None))
    else:
        config = read(args.config)
        recipe = inspect_binding(config, args.binding)
    if args.models_command == 'inspect':
        print(json.dumps(recipe, indent=2))
        return 0
    from .cases import select_cases, CONSUMERS, EVALUATORS
    roles = [r.strip() for r in (args.roles or ('chat' if native else 'chat,extraction')).split(',') if r.strip()]
    consumers = CONSUMERS
    if native:
        from .native import cases as native_cases, native_cli, native_context
        deadline, cleanup = getattr(args, 'deadline_seconds', None), getattr(args, 'cleanup_seconds', None)
        cases = native_cases(roles, deadline_seconds=60 if deadline is None else deadline,
                            cleanup_seconds=5 if cleanup is None else cleanup)
        consumers = {**CONSUMERS, 'native_cli': native_cli}
        factory = lambda _: native_context(config, recipe)
    else:
        if getattr(args, 'deadline_seconds', None) is not None or getattr(args, 'cleanup_seconds', None) is not None:
            raise ValueError('Deadline overrides apply only to the native suite')
        cases = select_cases(roles)
        factory = lambda case: router_for(config, args.binding, [case])
    asyncio.run(evaluate(args.output, recipe, cases, consumers, EVALUATORS,
        factory, resume=args.resume, evidence_mode=args.evidence_mode))
    report = summarize(args.output)
    # Reports are new immutable views. Earlier reports remain useful when a run resumes.
    index = len(list(args.output.glob('report-*.json'))) + 1
    write_once(args.output / f'report-{index:03d}.json', report)
    text = markdown(report)
    publish(args.output / f'report-{index:03d}.md', text.encode())
    print(text)
    return 0 if all(row['outcome'] == 'pass' for row in report['cases']) else 1
