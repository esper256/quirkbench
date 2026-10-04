"""P0 argument/help contract for the planned public CLI.

This parser is a specification fixture. The executable CLI does not call it until
the owning packets implement the corresponding application services.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="quirkbench", description="Planned product command contract v1")
    root.add_argument("--state", type=Path, help="explicit legacy or selected controller state root")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("setup", help="configure the foreground controller")
    commands.add_parser("pair", help="create a short-lived target enrollment code")
    commands.add_parser("targets", help="show separate target recovery, enrollment and experiment readiness facts")
    target = commands.add_parser("target", help="target administration")
    target_actions = target.add_subparsers(dest="action", required=True)
    qualify = target_actions.add_parser("qualify", help="record separately authorized physical qualification")
    qualify.add_argument("target_id", metavar="TARGET")
    qualify.add_argument("--request-id")

    session = commands.add_parser("session", help="investigation sessions over existing campaigns")
    session_actions = session.add_subparsers(dest="action", required=True)
    start = session_actions.add_parser("start", help="create a session and queue its attended baseline")
    start.add_argument("--device", required=True, metavar="TARGET", help="enrolled target ID; preserves the public --device name")
    start.add_argument("--problem", type=Path, metavar="FILE")
    start.add_argument("--driver", choices=("external", "managed"))
    start.add_argument("--request-id")
    for name, description in (
        ("context", "read bounded session context"),
        ("recipes", "list installed eligible recipes"),
        ("proposal-schema", "read the exact proposal schema"),
        ("status", "show independent progress and safety facts"),
        ("observations", "list human observation requests and responses"),
    ):
        command = session_actions.add_parser(name, help=description)
        command.add_argument("session_id", metavar="SESSION")
        command.add_argument("--json", action="store_true")
    propose = session_actions.add_parser("propose", help="accept a proposal as a durable operation")
    propose.add_argument("session_id", metavar="SESSION")
    propose.add_argument("--file", type=Path, required=True, metavar="FILE")
    propose.add_argument("--request-id", required=True)
    capture = session_actions.add_parser("capture-source", help="queue an exclusive dirty-source capture")
    capture.add_argument("session_id", metavar="SESSION")
    capture.add_argument("--request-id", required=True)
    watch = session_actions.add_parser("watch", help="watch events without invoking an agent")
    watch.add_argument("session_id", metavar="SESSION")
    watch.add_argument("--json", action="store_true")
    for name in ("pause", "resume"):
        command = session_actions.add_parser(name, help=f"{name} scheduling with reconciliation")
        command.add_argument("session_id", metavar="SESSION")
        command.add_argument("--request-id")
    export = session_actions.add_parser("export", help="write a public investigation bundle")
    export.add_argument("session_id", metavar="SESSION")
    export.add_argument("--output", type=Path, required=True, metavar="PATH")
    respond = session_actions.add_parser("respond", help="durably answer a typed human request")
    respond.add_argument("session_id", metavar="SESSION")
    respond.add_argument("--request", required=True, metavar="ID")
    respond.add_argument("--file", type=Path, required=True, metavar="FILE")
    respond.add_argument("--request-id", required=True)

    experiment = commands.add_parser("experiment", help="experiment specifications")
    experiment_actions = experiment.add_subparsers(dest="action", required=True)
    listing = experiment_actions.add_parser("list", help="list experiment specifications for a session")
    listing.add_argument("--session", required=True, metavar="SESSION")
    listing.add_argument("--json", action="store_true")
    attempt = commands.add_parser("attempt", help="physical attempt records")
    attempt_actions = attempt.add_subparsers(dest="action", required=True)
    show = attempt_actions.add_parser("show", help="show an attempt without conflating it with its experiment")
    show.add_argument("attempt_id", metavar="ATTEMPT")
    show.add_argument("--json", action="store_true")
    evidence = commands.add_parser("evidence", help="read public evidence")
    evidence_actions = evidence.add_subparsers(dest="action", required=True)
    read = evidence_actions.add_parser("read", help="read a bounded public evidence range")
    read.add_argument("digest", metavar="DIGEST")
    read.add_argument("--offset", type=int, required=True)
    read.add_argument("--length", type=int, required=True)
    operation = commands.add_parser("operation", help="durable operation records")
    operation_actions = operation.add_subparsers(dest="action", required=True)
    status = operation_actions.add_parser("status", help="show queued, running, waiting or terminal state")
    status.add_argument("operation_id", metavar="ID")
    status.add_argument("--json", action="store_true")
    backup = commands.add_parser("backup", help="guided backup with a completeness report")
    backup.add_argument("destination", type=Path, nargs="?", help="legacy positional output alias")
    backup.add_argument("--output", type=Path, metavar="PATH")
    restore = commands.add_parser("restore", help="restore paused, then reconcile outstanding execution")
    restore.add_argument("backup", type=Path)
    return root
