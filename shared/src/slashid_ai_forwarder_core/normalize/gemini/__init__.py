"""Google Vertex AI Gemini ``generateContent`` API — vendor package.

Mirrors ``converse/`` and ``anthropic/``: ``schema.py`` (typed pydantic
wire shapes), ``stop_reasons.py`` (mapping table), ``normalize.py``
(joint ``to_normalized_invocation``), ``attachments.py`` (fresh-turn
attachment walker). Public entry point is
``normalize.to_normalized_invocation`` — conforms to the
``_ToInvocation[TIn, TOut]`` Protocol so the same dispatcher shape used
in ``bedrock/mil_normalize.py`` can host Gemini too.
"""
