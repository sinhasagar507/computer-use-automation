from pathlib import Path

import pytest

from cua.policy.guard import Policy, PolicyViolation
from cua.policy.redact import Redactor

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def policy():
    return Policy.load(ROOT / "config" / "policy.yaml")


def test_url_allowlist(policy):
    assert policy.url_allowed("http://127.0.0.1:5050/members/search")
    assert policy.url_allowed("http://localhost:5050/")
    assert not policy.url_allowed("http://127.0.0.1:5050/admin")
    assert not policy.url_allowed("https://example.com/members")
    with pytest.raises(PolicyViolation):
        policy.check_url("http://evil.test/members")


def test_action_allowlist(policy):
    policy.check_action("click")
    with pytest.raises(PolicyViolation):
        policy.check_action("shell")


def test_risk_classification(policy):
    assert policy.classify("click", "Search") == "read"
    assert policy.classify("click", "Open account") == "reversible_write"
    assert policy.classify("click", "Post transaction") == "irreversible"
    assert policy.classify("type", "Wire amount") == "read"   # typing never commits


def test_redaction(policy, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abcdefabcdef0123")
    r = Redactor(policy.redact_patterns, policy.redact_param_names)
    r.add_sensitive("password", "demo-pass")
    s = r.text("key sk-or-v1-abcdefabcdef0123 ssn 123-45-6789 pw demo-pass")
    assert "sk-or-v1-abcdef" not in s and "123-45-6789" not in s and "demo-pass" not in s
    d = r.obj({"password": "x", "member_id": "12345", "nested": {"ssn": "y", "note": "123-45-6789"}})
    assert d["password"] == "<redacted>" and d["member_id"] == "12345"
    assert d["nested"]["ssn"] == "<redacted>" and "<redacted>" in d["nested"]["note"]
