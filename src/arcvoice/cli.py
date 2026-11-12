"""arcvoice CLI: validate agents, run evals, or talk to an agent in the terminal."""

from __future__ import annotations

import argparse
import sys

from . import contact_id
from .engine import Conversation, check_tools
from .evals import _site, load_scenarios, run_scenario, signal_dict
from .llm import KeywordClassifier, default_classifier
from .spec import load_all, route


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        specs = load_all(args.agents)
        for spec in specs.values():
            check_tools(spec)
    except Exception as exc:  # noqa: BLE001 - report any spec problem cleanly
        print(f"FAIL {exc}")
        return 1
    for spec in specs.values():
        handles = ",".join(c.value for c in spec.handles)
        print(f"ok   {spec.id} v{spec.version}  handles={handles}  steps={len(spec.steps)}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    specs = load_all(args.agents)
    classifier = default_classifier() if args.live else KeywordClassifier()
    results = [run_scenario(s, specs, classifier) for s in load_scenarios(args.scenarios)]
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'} {r.name}")
        for f in r.failures:
            print(f"       {f}")
    rate = sum(r.passed for r in results) / len(results)
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed ({rate:.0%}), gate {args.min_pass:.0%}")
    return 0 if rate >= args.min_pass else 1


def cmd_chat(args: argparse.Namespace) -> int:
    specs = load_all(args.agents)
    event = contact_id.parse(contact_id.build(args.account, args.event, zone=args.zone))
    spec = route(specs, event.category)
    if spec is None:
        print(f"no agent handles {event.category}")
        return 1
    print(f"[{event.description} at account {event.account} -> {spec.id} v{spec.version}]\n")
    convo = Conversation(spec, _site(event.account), signal_dict(event), default_classifier())
    turn = convo.start()
    while True:
        print(f"agent: {turn.say}")
        for action in turn.actions:
            print(f"       [action] {action}")
        if turn.done:
            print(f"\n[disposition: {turn.disposition}]")
            return 0
        try:
            turn = convo.respond(input("you:   "))
        except (EOFError, KeyboardInterrupt):
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arcvoice")
    parser.add_argument("--agents", default="agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="validate every agent spec")

    p_eval = sub.add_parser("eval", help="run scenario evals")
    p_eval.add_argument("--scenarios", default="evals/scenarios")
    p_eval.add_argument("--min-pass", type=float, default=1.0)
    p_eval.add_argument("--live", action="store_true", help="use the real LLM classifier")

    p_chat = sub.add_parser("chat", help="talk to an agent in the terminal")
    p_chat.add_argument("--account", default="1234")
    p_chat.add_argument("--event", default="130", help="Contact ID event code, e.g. 130 burglary, 110 fire")
    p_chat.add_argument("--zone", default="003")

    args = parser.parse_args(argv)
    return {"validate": cmd_validate, "eval": cmd_eval, "chat": cmd_chat}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
