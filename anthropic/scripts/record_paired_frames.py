"""Record captured hook frames for the same conversations as the compliance fixtures.

A frame and a transcript of one conversation are what the cross-source address
test needs: the hook and the reader must derive the same key for the same run.
"""

import base64
import glob
import json
import os
import pathlib
import re
import subprocess

K = subprocess.run(
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
WORKING = "2fe4f004-d4ca-4dd8-a630-85cb8089a518"
# Frames pulled from the capture bucket beforehand; see the module docstring.
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "./frames")
OUT = pathlib.Path("/home/paulo/slashid/slashid-ai-forwarder/anthropic/tests/fixtures/paired")
OUT.mkdir(parents=True, exist_ok=True)

sessions = json.loads(
    subprocess.run(
        [
            "curl",
            "-s",
            "https://api.anthropic.com/v1/compliance/apps/sessions/local?limit=30",
            "-H",
            f"x-api-key: {K}",
            "-H",
            "anthropic-version: 2023-06-01",
        ],
        capture_output=True,
        text=True,
    ).stdout
)["data"]

SESS: dict[str, str] = {}
order: list[str] = []
for s in sessions:
    raw = s["id"][5:]
    d = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    if d["s"] == WORKING:
        continue
    SESS[d["s"]] = f"0000000{len(SESS) + 1}-0000-4000-8000-000000000000"
    order.append(d["s"])

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
]


def scrub(o):
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    if isinstance(o, str):
        for pat, rep in SUB:
            o = re.sub(pat, rep, o)
        for real, fake in SESS.items():
            o = o.replace(real, fake)
        return o
    return o


written = 0
for i, sid in enumerate(order, 1):
    found = []
    for f in glob.glob(f"{CAPTURE_DIR}/**/*.json", recursive=True):
        with open(f) as handle:
            env = json.load(handle)
        body = json.loads(env["body"])
        if body.get("session_id") != sid:
            continue
        found.append((env["headers"].get("webhook-timestamp") or "", body))
    found.sort(key=lambda p: p[0])
    for seq, (_, body) in enumerate(found, 1):
        dest = OUT / f"session_{i}_frame_{seq}.json"
        dest.write_text(json.dumps(scrub(body), indent=2) + "\n")
        written += 1
    print(f"  session_messages_{i}.json  <->  {len(found)} frame(s)")
print(f"wrote {written} paired frames")
