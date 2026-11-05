#!/usr/bin/env python3
"""Local, model-free revision and review ledger (Python 3.9+)."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


GATES = {
    "simple": ["editorial", "facts"],
    "written": ["editorial", "facts", "reader"],
    "video": ["editorial", "facts", "gusar", "reader"],
    "audit": ["editorial"],
}


class LoopError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_text(path):
    data = path.read_bytes()
    data.decode("utf-8")
    return data


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LoopError("Duplicate JSON field: " + key)
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def create(path, data):
    # Exclusive creation prevents silent replacement of records or user files.
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def atomic(path, value):
    fd, name = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(task):
    if not task.is_dir():
        raise LoopError("Task directory does not exist: " + str(task))
    lock = task / ".lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise LoopError("Task is locked; if no process is running, remove .lock manually")
    try:
        yield
    finally:
        lock.rmdir()


def load(task):
    state = read_json(task / "state.json")
    if (not isinstance(state, dict) or state.get("format") != 1
            or state.get("profile") not in GATES
            or type(state.get("revision")) is not int
            or type(state.get("max_rounds")) is not int
            or not 1 <= state["revision"] <= state["max_rounds"]):
        raise LoopError("Invalid state.json")
    snapshot = read_text(task / "snapshots" / ("%03d.md" % state["revision"]))
    if digest(snapshot) != state.get("text_hash"):
        raise LoopError("Snapshot does not match state.json")
    return state


def reviews(task, state):
    latest = {}
    for path in sorted((task / "reviews").glob("*.json")):
        review = read_json(path)
        validate_review(review, state, check_hash=False)
        if review["text_hash"] == state["text_hash"] and review.get("revision") == state["revision"]:
            latest[review["gate"]] = review
    return latest


def validate_review(review, state, check_hash=True):
    required = {"gate", "text_hash", "reviewer", "verdict", "explanation", "blockers"}
    allowed = required if check_hash else required | {"revision"}
    if not isinstance(review, dict) or set(review) != allowed:
        raise LoopError("Review fields must be: " + ", ".join(sorted(allowed)))
    for field in required - {"blockers"}:
        if not isinstance(review[field], str) or not review[field].strip():
            raise LoopError("Review field must be a nonempty string: " + field)
    if review["gate"] not in GATES[state["profile"]]:
        raise LoopError("Gate is not used by this profile")
    if review["verdict"] not in ("pass", "revise", "blocked"):
        raise LoopError("Verdict must be pass, revise or blocked")
    if (not isinstance(review["blockers"], list)
            or any(not isinstance(x, str) or not x.strip() for x in review["blockers"])):
        raise LoopError("blockers must be a list of nonempty strings")
    if review["verdict"] == "pass" and review["blockers"]:
        raise LoopError("A pass cannot contain blockers")
    if check_hash and review["text_hash"] != state["text_hash"]:
        raise LoopError("Review has a stale or foreign text_hash")


def current(task, state):
    if digest(read_text(task / "text.md")) != state["text_hash"]:
        raise LoopError("text.md changed: run advance before submitting reviews or packets")
    if state.get("stopped"):
        raise LoopError("Task stopped: " + state["stopped"])


def prerequisites(state, latest, gate):
    needed = []
    if gate == "gusar":
        needed = ["facts"]
    elif gate == "reader":
        needed = ["editorial", "facts"]
        if state["profile"] == "video":
            needed.append("gusar")
    if any(latest.get(x, {}).get("verdict") != "pass" for x in needed):
        raise LoopError("Gate requires current passes: " + ", ".join(needed))


def status(task, state):
    changed = digest(read_text(task / "text.md")) != state["text_hash"]
    latest = reviews(task, state)
    gates = {g: latest.get(g, {}).get("verdict", "pending") for g in GATES[state["profile"]]}
    reader = latest.get("reader")
    independent = not reader or all(
        reader["reviewer"].strip().casefold() != latest.get(g, {}).get("reviewer", "").strip().casefold()
        for g in ("editorial", "gusar"))
    ready = (not changed and not state.get("stopped") and independent
             and all(v == "pass" for v in gates.values()))
    exhausted = state["revision"] >= state["max_rounds"] and (
        changed or any(v in ("revise", "blocked") for v in gates.values()))
    result = "ready" if ready else "in_progress"
    if exhausted or state.get("stopped"):
        result = "needs_author"
    return {"status": result, "profile": state["profile"], "revision": state["revision"],
            "max_rounds": state["max_rounds"], "text_hash": state["text_hash"],
            "text_changed": changed, "gates": gates, "stop_reason": state.get("stopped")}


def init(args):
    data = read_text(args.source)
    if args.max_rounds < 1:
        raise LoopError("max-rounds must be positive")
    # mkdir is deliberately exclusive, even for an existing empty directory.
    args.task.mkdir(parents=True, exist_ok=False)
    (args.task / "snapshots").mkdir()
    (args.task / "reviews").mkdir()
    (args.task / "events").mkdir()
    create(args.task / "text.md", data)
    create(args.task / "snapshots" / "001.md", data)
    state = {"format": 1, "profile": args.profile, "revision": 1,
             "max_rounds": args.max_rounds, "text_hash": digest(data)}
    atomic(args.task / "state.json", state)
    return status(args.task, state)


def event(task, value):
    paths = list((task / "events").glob("*.json"))
    number = max([int(p.stem) for p in paths] + [0]) + 1
    create(task / "events" / ("%06d.json" % number), json_bytes(value))


def execute(args):
    if args.command == "init":
        return init(args)
    with locked(args.task):
        state = load(args.task)
        if args.command == "status":
            return status(args.task, state)
        if args.command in ("advance", "snapshot"):
            if state.get("stopped"):
                raise LoopError("Task stopped; initialize a new task")
            data = read_text(args.task / "text.md")
            if digest(data) == state["text_hash"]:
                return status(args.task, state)
            if state["revision"] >= state["max_rounds"]:
                raise LoopError("Revision limit reached: needs_author; extend explicitly with a reason")
            revision = state["revision"] + 1
            create(args.task / "snapshots" / ("%03d.md" % revision), data)
            state.update(revision=revision, text_hash=digest(data))
            atomic(args.task / "state.json", state)
        elif args.command == "extend":
            if args.max_rounds <= state["max_rounds"]:
                raise LoopError("New max-rounds must exceed the current limit")
            if not args.reason.strip():
                raise LoopError("A nonempty user-authorized reason is required")
            event(args.task, {"action": "extend", "old_limit": state["max_rounds"],
                              "new_limit": args.max_rounds, "reason": args.reason})
            state["max_rounds"] = args.max_rounds
            atomic(args.task / "state.json", state)
        elif args.command == "stop":
            if not args.reason.strip():
                raise LoopError("A nonempty stop reason is required")
            event(args.task, {"action": "stop", "reason": args.reason})
            state["stopped"] = args.reason
            atomic(args.task / "state.json", state)
        elif args.command == "review":
            current(args.task, state)
            review = read_json(args.review)
            validate_review(review, state)
            latest = reviews(args.task, state)
            prerequisites(state, latest, review["gate"])
            if review["gate"] == "reader":
                identity = review["reviewer"].strip().casefold()
                if any(identity == latest.get(g, {}).get("reviewer", "").strip().casefold()
                       for g in ("editorial", "gusar")):
                    raise LoopError("Reader identity must differ from editorial and gusar reviewers")
            review["revision"] = state["revision"]
            paths = list((args.task / "reviews").glob("*.json"))
            number = max([int(p.stem) for p in paths] + [0]) + 1
            create(args.task / "reviews" / ("%06d.json" % number), json_bytes(review))
        elif args.command == "reader-packet":
            current(args.task, state)
            if "reader" not in GATES[state["profile"]]:
                raise LoopError("This profile does not use a reader")
            prerequisites(state, reviews(args.task, state), "reader")
            metadata = Path(str(args.output) + ".json")
            if args.output.exists() or metadata.exists():
                raise LoopError("Reader packet output or metadata already exists")
            create(args.output, read_text(args.task / "text.md"))
            create(metadata, json_bytes({"revision": state["revision"], "text_hash": state["text_hash"]}))
        return status(args.task, state)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    new = commands.add_parser("init")
    new.add_argument("task", type=Path)
    new.add_argument("--source", type=Path, required=True)
    new.add_argument("--profile", choices=GATES, required=True)
    new.add_argument("--max-rounds", type=int, default=3)
    for name in ("status", "advance", "snapshot", "review", "reader-packet", "extend", "stop"):
        cmd = commands.add_parser(name)
        cmd.add_argument("task", type=Path)
        if name == "review":
            cmd.add_argument("review", type=Path)
        if name == "reader-packet":
            cmd.add_argument("--output", type=Path, required=True)
        if name in ("extend", "stop"):
            cmd.add_argument("--reason", required=True)
        if name == "extend":
            cmd.add_argument("--max-rounds", type=int, required=True)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(execute(args), ensure_ascii=False, indent=2))
        return 0
    except (LoopError, OSError, ValueError, KeyError, TypeError) as exc:
        print("loop: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
