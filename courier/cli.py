"""Courier CLI: user-control verbs over the router core.

Verbs: route | inspect | topics | show | correct | merge | split |
pause | resume | forget | approvals | doctor | status.
Exit codes: 0 ok | 2 usage | 3 conflict | 4 lease | 5 integrity.
"""

import argparse
import json
import os
import sys

from . import (EXIT_CONFLICT, EXIT_INTEGRITY, EXIT_LEASE, EXIT_OK,
               EXIT_USAGE)
from . import approvals as approvals_mod
from . import dispatcher as dispatcher_mod
from . import intake as intake_mod
from . import policy as policy_mod
from . import redact as redact_mod
from . import router as router_mod
from .adapters.local import LocalBackend
from .catalog import ConflictError, SeatError, SessionCatalog
from .ledger import DeliveryLedger

HOLDER = "courier-cli"


# -- state ---------------------------------------------------------------
class State:
    def __init__(self, home):
        self.home = os.path.abspath(os.path.expanduser(home))
        os.makedirs(self.home, exist_ok=True)
        self.projection = os.path.join(self.home, "routing_projection.json")
        self.sqlite = os.path.join(self.home, "ledger.sqlite")
        self.approvals_path = os.path.join(self.home, "approvals.json")
        self.config_path = os.path.join(self.home, "courier.config.yaml")

    def catalog(self):
        return SessionCatalog(self.projection)

    def ledger(self):
        return DeliveryLedger(self.sqlite)

    def backend(self):
        b = LocalBackend()
        try:
            for s in self.catalog().list_sessions():
                b.seed(s["slug"], s.get("summary", ""),
                       session_id=s.get("backend_session_id") or
                       s["session_id"])
        except Exception:  # noqa: BLE001 — projection may not exist yet
            pass
        return b

    def load_config(self):
        cfg = _load_config_file(self.config_path)
        return policy_mod.ModelConfig.from_dict(cfg)

    def load_pending(self):
        if not os.path.exists(self.approvals_path):
            return []
        with open(self.approvals_path, encoding="utf-8") as f:
            return json.load(f).get("pending", [])

    def save_pending(self, pending):
        tmp = self.approvals_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pending": pending}, f, indent=2, sort_keys=True)
        os.replace(tmp, self.approvals_path)


def default_state_home():
    return os.environ.get("COURIER_STATE_HOME",
                          os.path.join(os.path.expanduser("~"),
                                       ".hermes", "courier"))


def _load_config_file(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    return _parse_simple_yaml(raw)


def _parse_simple_yaml(raw):
    """Minimal indented-mapping YAML subset for courier.config.yaml
    (2-space nesting, scalars: bool/float/int/quoted str)."""
    root = {}
    stack = [(-1, root)]
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, val = line.strip().partition(":")
        key = key.strip()
        val = val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(val)
    return root


def _scalar(val):
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


def _emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def _fail(code, message):
    print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
    return code


# -- verbs ---------------------------------------------------------------
def verb_route(args, st):
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        return _fail(EXIT_USAGE, "empty message text")
    try:
        msg = intake_mod.make_message(text, channel=args.channel,
                                      sender=args.sender)
        redacted, report = redact_mod.redact_message(msg)
        sessions = [{"session_id": s["session_id"], "slug": s["slug"],
                     "summary": s.get("summary", "")}
                    for s in st.catalog().list_sessions()]
        decision, steps = router_mod.route_with_escalation(redacted, sessions)
        if args.dry_run:
            _emit({"ok": True, "decision": decision, "steps": steps,
                   "redaction": report})
            return EXIT_OK
        result = dispatcher_mod.dispatch(redacted, decision, st.catalog(),
                                         st.ledger(), st.backend(),
                                         holder=HOLDER,
                                         record_steps=steps)
        if result.get("result") == "held-for-approval":
            pending = st.load_pending()
            pending.append({
                "id": "apr-%s" % redacted["message_id"][:8],
                "message_id": redacted["message_id"],
                "action_class": result.get("action_class"),
                "reason": result.get("reason"),
                "target_session_id": result.get("target_session_id"),
                "ts_utc": result.get("ts_utc")})
            st.save_pending(pending)
        _emit({"ok": True, "decision": decision, "steps": steps,
               "dispatch": result, "redaction": report})
        return EXIT_OK
    except ConflictError as e:
        return _fail(EXIT_CONFLICT, str(e))
    except SeatError as e:
        return _fail(EXIT_LEASE, str(e))
    except ValueError as e:
        return _fail(EXIT_INTEGRITY, str(e))


def verb_inspect(args, st):
    led = st.ledger()
    rec = led.find_by_message(args.message_id)
    led.close()
    if rec is None:
        return _fail(EXIT_INTEGRITY,
                     "unknown message-id: %s" % args.message_id)
    _emit({"ok": True, "record": rec})
    return EXIT_OK


def verb_topics(args, st):
    sessions = st.catalog().list_sessions()
    _emit({"ok": True, "topics": [
        {"slug": s["slug"], "summary": s.get("summary", ""),
         "session_id": s["session_id"]} for s in sessions]})
    return EXIT_OK


def verb_show(args, st):
    cat = st.catalog()
    ref = cat.get(args.slug)
    if ref is None:
        return _fail(EXIT_INTEGRITY, "unknown session: %s" % args.slug)
    led = st.ledger()
    recent = led.recent_for_session(ref["session_id"], limit=10)
    led.close()
    _emit({"ok": True, "session": ref, "recent_decisions": recent,
           "events": cat.events(limit=10)})
    return EXIT_OK


def verb_correct(args, st):
    cat, led = st.catalog(), st.ledger()
    rec = led.find_by_message(args.message_id)
    if rec is None:
        led.close()
        return _fail(EXIT_INTEGRITY,
                     "unknown message-id: %s" % args.message_id)
    target = cat.get(args.to)
    if target is None:
        led.close()
        return _fail(EXIT_INTEGRITY, "unknown slug: %s" % args.to)
    cat.record_event({"type": "ledger-amend", "message_id": args.message_id,
                      "from_session": rec.get("target_session_id"),
                      "to_session": target["session_id"],
                      "to_slug": args.to})
    led.close()
    _emit({"ok": True, "message_id": args.message_id,
           "rerouted_to": target["session_id"], "slug": args.to})
    return EXIT_OK


def verb_merge(args, st):
    try:
        ref = st.catalog().merge(args.slug_a, args.slug_b, args.into)
    except ConflictError as e:
        return _fail(EXIT_CONFLICT, str(e))
    except (KeyError, ValueError) as e:
        return _fail(EXIT_INTEGRITY, str(e))
    _emit({"ok": True, "merged_into": ref})
    return EXIT_OK


def verb_split(args, st):
    try:
        ref = st.catalog().split(args.slug, args.new,
                                 at_message_id=args.at)
    except ConflictError as e:
        return _fail(EXIT_CONFLICT, str(e))
    except (KeyError, ValueError) as e:
        return _fail(EXIT_INTEGRITY, str(e))
    _emit({"ok": True, "created": ref})
    return EXIT_OK


def verb_pause(args, st):
    try:
        ref = st.catalog().set_paused(args.slug, True)
    except KeyError as e:
        return _fail(EXIT_INTEGRITY, str(e))
    _emit({"ok": True, "paused": ref["slug"]})
    return EXIT_OK


def verb_resume(args, st):
    try:
        ref = st.catalog().set_paused(args.slug, False)
    except KeyError as e:
        return _fail(EXIT_INTEGRITY, str(e))
    _emit({"ok": True, "resumed": ref["slug"]})
    return EXIT_OK


def verb_forget(args, st):
    if args.drop_ledger and not args.confirm:
        return _fail(EXIT_USAGE,
                     "--drop-ledger requires --confirm (explicit)")
    cat, led = st.catalog(), st.ledger()
    try:
        ref = cat.forget(args.slug)
    except KeyError as e:
        led.close()
        return _fail(EXIT_INTEGRITY, str(e))
    if args.drop_ledger:
        dropped = led.drop_session(ref["session_id"])
        led.close()
        _emit({"ok": True, "forgotten": args.slug,
               "ledger_rows_dropped": dropped})
    else:
        n = led.anonymize_session(ref["session_id"])
        led.close()
        _emit({"ok": True, "forgotten": args.slug,
               "ledger_rows_anonymized": n})
    return EXIT_OK


def verb_approvals(args, st):
    pending = st.load_pending()
    if args.approve:
        nxt = [p for p in pending if p["id"] != args.approve]
        if len(nxt) == len(pending):
            return _fail(EXIT_INTEGRITY,
                         "unknown approval: %s" % args.approve)
        st.catalog().record_event({"type": "approval-granted",
                                   "id": args.approve})
        st.save_pending(nxt)
        _emit({"ok": True, "approved": args.approve,
               "hint": "re-run `courier route` to deliver"})
        return EXIT_OK
    if args.deny:
        nxt = [p for p in pending if p["id"] != args.deny]
        if len(nxt) == len(pending):
            return _fail(EXIT_INTEGRITY, "unknown approval: %s" % args.deny)
        st.catalog().record_event({"type": "approval-denied", "id": args.deny})
        st.save_pending(nxt)
        _emit({"ok": True, "denied": args.deny})
        return EXIT_OK
    _emit({"ok": True, "pending": pending})
    return EXIT_OK


def verb_doctor(args, st):
    try:
        cfg = st.load_config()
    except ValueError as e:
        return _fail(EXIT_INTEGRITY, "bad config: %s" % e)
    chain = policy_mod.resolve_chain(cfg, "router")
    cat, led = st.catalog(), st.ledger()
    sessions = cat.list_sessions()
    live = sum(1 for s in sessions if s.get("seat_holder"))
    n_ledger = led.count()
    led.close()
    free_state = ("enabled" if policy_mod.free_tier_enabled(cfg)
                  else "disabled (explicit config + verification required)")
    _emit({"ok": True,
           "router_chain": [{"tier": t, "provider": p, "model": m}
                            for t, p, m in chain],
           "free_tier": free_state,
           "sessions": len(sessions), "live_seats": live,
           "ledger_rows": n_ledger,
           "projection": st.projection})
    return EXIT_OK


def verb_status(args, st):
    cat, led = st.catalog(), st.ledger()
    sessions = cat.list_sessions()
    n = led.count()
    led.close()
    _emit({"ok": True, "sessions": len(sessions),
           "pending_approvals": len(st.load_pending()),
           "ledger_rows": n})
    return EXIT_OK


# -- parser ---------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="courier",
                                description="Courier session-router core (v0.1)")
    p.add_argument("--state-home", default=None,
                   help="state dir (default: $COURIER_STATE_HOME or ~/.hermes/courier)")
    sub = p.add_subparsers(dest="verb", required=True)

    r = sub.add_parser("route", help="route (+dispatch) one message")
    r.add_argument("text", nargs="*", help="message text (or stdin)")
    r.add_argument("--channel", default="cli")
    r.add_argument("--sender", default="user")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=verb_route)

    i = sub.add_parser("inspect", help="show a ledger record by message-id")
    i.add_argument("message_id")
    i.set_defaults(fn=verb_inspect)

    sub.add_parser("topics", help="list slugs + summaries").set_defaults(
        fn=verb_topics)
    s = sub.add_parser("show", help="session + revision + seat + decisions")
    s.add_argument("slug")
    s.set_defaults(fn=verb_show)

    c = sub.add_parser("correct", help="re-route + ledger amend event")
    c.add_argument("message_id")
    c.add_argument("--to", required=True)
    c.set_defaults(fn=verb_correct)

    m = sub.add_parser("merge", help="CAS-guarded merge")
    m.add_argument("slug_a")
    m.add_argument("slug_b")
    m.add_argument("--into", required=True)
    m.set_defaults(fn=verb_merge)

    sp = sub.add_parser("split", help="split a session")
    sp.add_argument("slug")
    sp.add_argument("--at", default=None)
    sp.add_argument("--new", required=True)
    sp.set_defaults(fn=verb_split)

    pa = sub.add_parser("pause", help="hold new messages for a topic")
    pa.add_argument("slug")
    pa.set_defaults(fn=verb_pause)
    rs = sub.add_parser("resume", help="unhold a paused topic")
    rs.add_argument("slug")
    rs.set_defaults(fn=verb_resume)

    f = sub.add_parser("forget", help="remove a projection entry")
    f.add_argument("slug")
    f.add_argument("--drop-ledger", action="store_true")
    f.add_argument("--confirm", action="store_true")
    f.set_defaults(fn=verb_forget)

    a = sub.add_parser("approvals", help="pending approvals + approve/deny")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--approve", default=None)
    g.add_argument("--deny", default=None)
    a.set_defaults(fn=verb_approvals)

    sub.add_parser("doctor", help="tier + seat + ledger health").set_defaults(
        fn=verb_doctor)
    sub.add_parser("status", help="counts").set_defaults(fn=verb_status)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    st = State(args.state_home or default_state_home())
    try:
        code = args.fn(args, st)
    except ConflictError as e:
        code = _fail(EXIT_CONFLICT, str(e))
    except SeatError as e:
        code = _fail(EXIT_LEASE, str(e))
    except (KeyError, ValueError) as e:
        code = _fail(EXIT_INTEGRITY, str(e))
    return code or EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
