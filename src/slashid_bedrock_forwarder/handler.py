"""Lambda entry point.

CloudWatch Logs subscription filters deliver events shaped like:

    {"awslogs": {"data": "<base64-encoded gzipped JSON>"}}

The decoded payload contains `logEvents[]`, each holding one MIL record in
the `message` field. This handler decodes, normalizes, and forwards each
record to SlashID's NHI AI invocations endpoint.
"""

from __future__ import annotations


def lambda_handler(event: dict, context: object) -> dict:
    """Lambda entry point — placeholder until the real pipeline lands."""
    del event, context
    raise NotImplementedError("forwarder pipeline coming in subsequent commits")
