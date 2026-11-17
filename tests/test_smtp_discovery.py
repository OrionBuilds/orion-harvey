import pytest

from harvey.integrations import email_finder as ef


def test_rcpt_replies_are_classified_honestly():
    assert ef.classify_rcpt_reply(250, "OK") is True
    assert ef.classify_rcpt_reply(550, "5.1.1 The email account does not exist") is False
    assert ef.classify_rcpt_reply(550, "5.4.1 Recipient address rejected: Access denied") is False   # Microsoft 365
    assert ef.classify_rcpt_reply(550, "5.7.1 Service unavailable, client host blocked by Spamhaus") is None
    assert ef.classify_rcpt_reply(450, "4.2.0 greylisted, try later") is None
    assert ef.classify_rcpt_reply(550, "no") is None


@pytest.fixture
def probe(monkeypatch):
    """Fake mail server: `accepts` is the set of addresses it accepts; `catch_all` accepts anything."""
    state = {"accepts": set(), "catch_all": False, "down": False, "asked": []}

    async def mx(domain):
        return "mx.example.net"

    async def rcpt(host, email):
        state["asked"].append(email)
        if state["down"]:
            return None
        return True if state["catch_all"] or email in state["accepts"] else False

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_smtp_rcpt", rcpt)
    ef._catch_all_cache.clear()
    return state


@pytest.mark.asyncio
async def test_finds_the_pattern_the_server_accepts(probe):
    probe["accepts"] = {"jsmith@acme.com"}
    assert await ef.smtp_find_mailbox("Jane", "Smith", "acme.com") == ("jsmith@acme.com", "verified", "{fi}{l}")


@pytest.mark.asyncio
async def test_catch_all_domains_are_risky_not_verified(probe):
    probe["catch_all"] = True
    email, status, _ = await ef.smtp_find_mailbox("Jane", "Smith", "acme.com")
    assert status == "risky" and email == "jane.smith@acme.com"


@pytest.mark.asyncio
async def test_unreachable_server_is_undecided_so_api_verifiers_take_over(probe):
    probe["down"] = True
    assert await ef.smtp_find_mailbox("Jane", "Smith", "acme.com") is None


@pytest.mark.asyncio
async def test_no_pattern_matches_is_invalid(probe):
    email, status, _ = await ef.smtp_find_mailbox("Jane", "Smith", "acme.com")
    assert status == "invalid"


@pytest.mark.asyncio
async def test_find_email_uses_smtp_before_spending_credits(probe, monkeypatch):
    monkeypatch.setenv("HARVEY_SMTP_VERIFY", "1")
    probe["accepts"] = {"jane.smith@acme.com"}
    monkeypatch.setattr(ef, "classify_mx", lambda host: "google")

    async def boom(*a, **k):
        raise AssertionError("a paid verifier was called")

    monkeypatch.setattr(ef, "verify_reoon", boom)
    monkeypatch.setattr(ef, "verify_zerobounce", boom)
    result = await ef.find_email("Jane", "Smith", "acme.com", env=type("E", (), {"reoon_api_key": "k", "zerobounce_api_key": "k"})())
    assert (result.email, result.status) == ("jane.smith@acme.com", "verified")
