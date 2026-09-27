from formal_agent_skill_redaction import redact


def test_usage_and_latency_are_not_mistaken_for_credentials():
    safe, count = redact({
        "input_tokens": 88,
        "output_tokens": 384,
        "first_token_latency_ms": 127,
        "token": "secret-token-value",
        "Authorization": "Bearer abcdefghijklmnop",
    })
    assert safe["input_tokens"] == 88
    assert safe["output_tokens"] == 384
    assert safe["first_token_latency_ms"] == 127
    assert safe["token"] == "[REDACTED]"
    assert safe["Authorization"] == "[REDACTED]"
    assert count == 2
