"""Job-board discovery: size bands, staffing filter, and the HIRING_ROLE signal."""

import pandas as pd
import pytest

from harvey.collectors import discover
from harvey.collectors.discover import (
    Business, DiscoveryQuery, JobBoards, _observations, build_job_queries,
    parse_size_band, size_in_band,
)


def test_parse_size_band():
    assert parse_size_band("1,001 to 5,000") == (1001, 5000)
    assert parse_size_band("11 to 50") == (11, 50)
    assert parse_size_band("10,000+") == (10000, 10**9)
    assert parse_size_band("") is None


def test_size_in_band_overlap_and_unknown():
    assert size_in_band((201, 500), 100, 1000) is True
    assert size_in_band((1001, 5000), 100, 1000) is False
    assert size_in_band((11, 50), 100, 1000) is False
    assert size_in_band(None, 100, 1000) is None        # unknown is kept, not guessed


def test_cohort_queries_carry_bands_and_reject_parked_cohort():
    qs = build_job_queries(["A", "B"])
    assert {q.cohort for q in qs} == {"A", "B"}
    a = next(q for q in qs if q.cohort == "A")
    assert (a.min_employees, a.max_employees) == (50, 1500)
    with pytest.raises(ValueError):
        build_job_queries(["C"])


ROWS = [
    {"company": "Acme Bank", "title": "VP of Engineering", "location": "Austin, TX, USA",
     "company_url_direct": "https://www.acmebank.com/", "company_num_employees": "201 to 500",
     "company_industry": None, "job_url": "https://indeed.com/viewjob?jk=1"},
    {"company": "Tiny Co", "title": "CTO", "location": "Remote", "company_url_direct": "http://tiny.co",
     "company_num_employees": "11 to 50", "company_industry": None, "job_url": "https://indeed.com/viewjob?jk=2"},
    {"company": "Robert Half", "title": "CTO", "location": "Remote", "company_url_direct": None,
     "company_num_employees": None, "company_industry": None, "job_url": "https://indeed.com/viewjob?jk=3"},
    {"company": "Mystery Corp", "title": "Head of Engineering", "location": "Denver, CO, USA",
     "company_url_direct": None, "company_num_employees": None, "company_industry": None,
     "job_url": "https://indeed.com/viewjob?jk=4"},
]


@pytest.mark.asyncio
async def test_fetch_filters_size_and_staffing_and_keeps_unknown(monkeypatch):
    provider = JobBoards()
    monkeypatch.setattr(provider, "_scrape", lambda q: pd.DataFrame(ROWS))
    query = DiscoveryQuery(term="x", cohort="B", min_employees=100, max_employees=1000)
    found = await provider.fetch(None, {}, query)

    assert [b.name for b in found] == ["Acme Bank", "Mystery Corp"]   # Tiny Co too small, Robert Half staffing
    acme, mystery = found
    assert acme.domain == "acmebank.com" and acme.job_title == "VP of Engineering"
    assert acme.cohort == "B" and acme.headcount == "201 to 500"
    assert mystery.domain == "" and mystery.headcount == ""            # unknown stays unknown


def test_job_title_becomes_a_hiring_role_observation():
    biz = Business(name="Acme Bank", domain="acmebank.com", job_title="VP of Engineering",
                   source_url="https://indeed.com/viewjob?jk=1", provider="jobboards")
    rows = _observations(biz, "c1", {"HIRING_ROLE", "FOUND_IN_SERP"}, "run1")
    hiring = [r for r in rows if r["signal_code"] == "HIRING_ROLE"]
    assert hiring and hiring[0]["value_text"] == "VP of Engineering"
    assert hiring[0]["evidence_url"].startswith("https://indeed.com/")
    assert _observations(biz, "c1", {"NO_WEBSITE"}, "run1") == []      # unconfirmed signals are not recorded


def test_provider_is_registered_free_and_needs_no_key():
    p = discover.PROVIDERS["jobboards"]
    assert p.configured({}) and p.estimate([DiscoveryQuery(term="x")]) == 0.0


@pytest.mark.asyncio
async def test_title_filter_drops_noise_and_cohort_a_is_industry_agnostic(monkeypatch):
    rows = [
        {"company": "First Bank", "title": "Head of AI", "location": "NY", "company_url_direct": "https://firstbank.com",
         "company_num_employees": "501 to 1,000", "description": "A regional bank hiring...", "job_url": "u1"},
        {"company": "Arts Center", "title": "Membership Program Manager", "location": "NY",
         "company_url_direct": "https://arts.org", "company_num_employees": None, "description": "bank of ideas", "job_url": "u2"},
        {"company": "Widget Works", "title": "Director of AI", "location": "NY", "company_url_direct": "https://widgets.com",
         "company_num_employees": "501 to 1,000", "description": "We make widgets", "job_url": "u3"},
    ]
    provider = JobBoards()
    monkeypatch.setattr(provider, "_scrape", lambda q: pd.DataFrame(rows))
    a = build_job_queries(["A"])[0]
    found = await provider.fetch(None, {}, a)
    assert [b.name for b in found] == ["First Bank", "Widget Works"]   # wrong title dropped; cohort A is no longer limited to regulated industries


def test_cohort_b_title_pattern():
    import re
    pat = build_job_queries(["B"])[0].title_pattern
    for ok in ("Chief Technology Officer", "CTO", "VP of Engineering", "Vice President, Engineering",
               "Head of Technology", "Chief Information Officer"):
        assert re.search(pat, ok, re.I), ok
    for bad in ("Assistant Controller", "Project Manager", "Compliance Analyst", "Manufacturing / Process Engineer"):
        assert not re.search(pat, bad, re.I), bad


@pytest.mark.asyncio
async def test_scout_strategies_can_be_limited_to_none(monkeypatch):
    from types import SimpleNamespace
    from harvey.agents.scout import Scout
    called = []
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(scout=SimpleNamespace(strategies=[]), channels=SimpleNamespace(
        linkedin=SimpleNamespace(enabled=False, mode="off")))
    scout.env = SimpleNamespace(linkedin_email="")
    scout.brain = SimpleNamespace(load_skills_for_agent=lambda n: "")
    async def log_action(**k): called.append(("log", k))
    scout.state = SimpleNamespace(log_action=log_action)
    for name in ("_prospect_via_hunter", "_prospect_via_known_companies", "_prospect_via_hiring_boards",
                 "_prospect_via_profile_search", "_prospect_via_company_discovery"):
        async def boom(self=None, _n=name): called.append(_n); return 1
        monkeypatch.setattr(scout, name, boom)
    await scout.run()
    assert [c for c in called if isinstance(c, str)] == []      # no strategy ran


def test_cohort_a_title_pattern_covers_unfamiliar_ai_titles():
    import re
    pat = build_job_queries(["A"])[0].title_pattern
    for ok in ("Chief AI Officer", "Head of AI", "AI Lead", "AI Enablement Lead", "Director of Data and AI",
               "AI Governance Manager"):
        assert re.search(pat, ok, re.I), ok
    for bad in ("Membership Program Manager", "Sales Development Representative", "Warehouse Associate"):
        assert not re.search(pat, bad, re.I), bad
