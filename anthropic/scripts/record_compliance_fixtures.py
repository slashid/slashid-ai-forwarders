"""Re-record the compliance fixtures under ``tests/fixtures/compliance``.

The reader tests run against recorded responses rather than a live tenant,
so this script is how those recordings are made. Run it when an endpoint's
shape changes or when new traffic is needed; it overwrites every file it
writes and prints what it recorded.

Each fixture is one call: ``{request: {method, path, params}, status, body}``,
so a test can assert the query vocabulary from the same file that carries
the response. The rejections in ``filters_rejected.json`` are recorded
deliberately: the three feeds disagree on how to filter and order, and a
400 is the only proof of which spelling each one wants. Its last case is
a 200, kept as the control the rejections are read against.
``organizations.json`` is the odd one out and the only fixture whose cases
carry a ``base``, because the two bases answer inverted paths.

Two rules the fixtures depend on. Nothing from the working session is ever
recorded: it is a real conversation, and it is excluded by uuid below.
And every identifier is replaced, including the ones inside base64 — a
``clls_`` session id decodes to JSON carrying the organization, project
and session uuids, so scrubbing the text alone would leak all three.
``tests/test_fixtures_scrubbed.py`` re-checks every byte and is the thing
that actually enforces this.

Needs ``gcloud`` authenticated against the project below, for the key.
"""

import base64
import json
import pathlib
import re
import subprocess

KEY = subprocess.run(
    [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret=anthropic_compliance_key",
        "--project",
        "strong-hue-507702-k7",
    ],
    capture_output=True,
    text=True,
).stdout.strip()
BASE = "https://api.anthropic.com/v1/compliance"
OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "compliance"
# The working session. Real conversation, never recorded, excluded by uuid.
WORKING = "2fe4f004-d4ca-4dd8-a630-85cb8089a518"


def call(path, params=""):
    """One recorded GET: the request that made it, its status, and its body."""
    url = f"{BASE}{path}" + (("?" + params) if params else "")
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            url,
            "-H",
            f"x-api-key: {KEY}",
            "-H",
            "anthropic-version: 2023-06-01",
        ],
        capture_output=True,
        text=True,
    )
    body, code = r.stdout.rsplit("\n", 1)
    return {
        "request": {"method": "GET", "path": path, "params": params},
        "status": int(code),
        "body": json.loads(body),
    }


# Applied to every string, in order. The path rules come first so the
# username rule cannot mangle a path it is part of.
SUB = [
    (r"/home/paulo/\.claude/jobs/[0-9a-f]+/tmp/fixtures-ws", "/workspace"),
    (r"/home/paulo/\.claude/jobs/[0-9a-f]+", "/workspace"),
    (r"2fe4f004[0-9a-f-]*", "00000000-0000-4000-8000-000000000000"),
    (r"34936bc5-3c79-4380-9a6a-c3ada9ae6608", "11111111-1111-1111-1111-111111111111"),
    (r"org_01[A-Za-z0-9]{18,}", "org_01AAAAAAAAAAAAAAAAAAAAAA"),
    (r"user_01[A-Za-z0-9]{18,}", "user_01AbCdEfGhIjKlMnOpQrStUv"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "alice@example.com"),
    (r"/home/paulo", "/home/user"),
    (r"\bpaulo\b", "user"),
    (r"4d621dc4-bee0-4c32-a825-f9770f17db47", "22222222-2222-2222-2222-222222222222"),
]
# Real session uuid -> synthetic. Filled below, in listing order, so the
# same conversation gets the same placeholder across every fixture.
SESS: dict[str, str] = {}


def scrub_text(t):
    for pat, rep in SUB:
        t = re.sub(pat, rep, t)
    for real, fake in SESS.items():
        t = t.replace(real, fake)
    return t


def scrub_clls(cid):
    """Rewrite a ``clls_`` session id, which is base64 JSON carrying the
    organization, project and session uuids. Scrubbing the surrounding
    text would leave all three readable inside it."""
    raw = cid[5:]
    d = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    d["o"] = "11111111-1111-1111-1111-111111111111"
    d["p"] = "22222222-2222-2222-2222-222222222222"
    d["s"] = SESS.get(d["s"], d["s"])
    return "clls_" + base64.urlsafe_b64encode(
        json.dumps(d, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def walk(o):
    # ip_address is scrubbed by key, never by pattern: a global IP regex also
    # rewrites the Chrome version in user_agent, and the real client agent is
    # the one field the denial activity carries that no frame does.
    if isinstance(o, dict):
        return {k: ("2001:db8::1" if k == "ip_address" else walk(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v) for v in o]
    if isinstance(o, str):
        o = re.sub(r"clls_[A-Za-z0-9_-]+", lambda m: scrub_clls(m.group(0)), o)
        return scrub_text(o)
    return o


# Assign placeholders first: every later fixture is scrubbed against this
# map, so it has to be complete before anything is written.
raw = call("/apps/sessions/local", "limit=30")
keep = []
n = 0
for s in raw["body"]["data"]:
    d = json.loads(base64.urlsafe_b64decode(s["id"][5:] + "=" * (-len(s["id"][5:]) % 4)))
    if d["s"] == WORKING:
        continue
    n += 1
    SESS[d["s"]] = f"0000000{n}-0000-4000-8000-000000000000"
    keep.append(s)
raw["body"]["data"] = keep
print(f"sessions kept: {len(keep)} (working session excluded)")
(OUT / "sessions_list.json").write_text(json.dumps(walk(raw), indent=2) + "\n")

for i, s in enumerate(keep, 1):
    m = call(
        f"/apps/sessions/local/{s['id']}/messages",
        "limit=1000&tool_result_max_bytes=-1&tool_use_input_max_bytes=-1",
    )
    (OUT / f"session_messages_{i}.json").write_text(json.dumps(walk(m), indent=2) + "\n")
    print(f"  session_messages_{i}.json  {len(m['body'].get('data', []))} messages")

for name, path, params in [
    ("chats_list", "/apps/chats", "limit=100"),
    ("activities", "/activities", "limit=1000&order=asc"),
]:
    d = call(path, params)
    if name == "activities":
        d["body"]["data"] = [
            a for a in d["body"]["data"] if a["type"] != "compliance_api_accessed"
        ][:40]
    (OUT / f"{name}.json").write_text(json.dumps(walk(d), indent=2) + "\n")
    print(f"  {name}.json")

for i, c in enumerate(call("/apps/chats", "limit=100")["body"]["data"], 1):
    m = call(f"/apps/chats/{c['id']}/messages", "")
    (OUT / f"chat_messages_{i}.json").write_text(json.dumps(walk(m), indent=2) + "\n")
    print(f"  chat_messages_{i}.json  {len(m['body'].get('chat_messages', []))} messages")

rej = [
    call("/apps/chats", "limit=3&updated_at.gte=2026-09-20T00:00:00Z"),
    call("/apps/sessions/local", "limit=3&order=asc"),
    call("/apps/sessions/local", "limit=3&order_by=updated_at"),
    call("/activities", "limit=3&created_at%5Bgte%5D=2026-09-21T04:00:00Z"),
    call("/apps/chats", "limit=3&organization_uuid=11111111-1111-1111-1111-111111111111"),
    call("/organizations", ""),
]
(OUT / "filters_rejected.json").write_text(json.dumps(walk({"cases": rej}), indent=2) + "\n")
print("  filters_rejected.json:", [r["status"] for r in rej])


def call_at(base, path):
    """Like ``call``, but records which base answered — the only fixture
    where that matters."""
    out = subprocess.run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            f"https://api.anthropic.com/{base}{path}",
            "-H",
            f"x-api-key: {KEY}",
            "-H",
            "anthropic-version: 2023-06-01",
        ],
        capture_output=True,
        text=True,
    )
    body, code = out.stdout.rsplit("\n", 1)
    return {
        "request": {"method": "GET", "base": base, "path": path},
        "status": int(code),
        "body": json.loads(body),
    }


# The two bases answer inverted paths, so all four are recorded and none is
# guessed: under v1/compliance it is /organizations that answers and
# /organizations/me that 404s, and under plain v1 it is the other way round.
organizations = {
    "note": "The two bases answer inverted paths; both recorded so neither is guessed.",
    "cases": [
        call_at("v1/compliance", "/organizations"),
        call_at("v1/compliance", "/organizations/me"),
        call_at("v1", "/organizations"),
        call_at("v1", "/organizations/me"),
    ],
}
(OUT / "organizations.json").write_text(json.dumps(walk(organizations), indent=2) + "\n")
print("  organizations.json:", [c["status"] for c in organizations["cases"]])
