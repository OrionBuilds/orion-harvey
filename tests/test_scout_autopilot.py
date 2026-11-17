"""Autopilot prospecting: cohort discovery runs on a schedule, and the daily cap on new people holds."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.scout import Scout
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


def make_scout(state, **scout_cfg):
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(
        scout=SimpleNamespace(**scout_cfg),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False, mode="off")),
        icp=SimpleNamespace(industries=["x"]))
    scout.env = SimpleNamespace(linkedin_email="")
    scout.brain = SimpleNamespace(load_skills_for_agent=lambda n: "")
    scout.state = state
    return scout


def stub_strategies(scout, monkeypatch, calls, found=0):
    for name in ("_prospect_via_hunter", "_prospect_via_known_companies", "_prospect_via_hiring_boards",
                 "_prospect_via_profile_search", "_prospect_via_company_discovery"):
        async def fake(_n=name):
            calls.append(_n)
            return found
        monkeypatch.setattr(scout, name, fake)


@pytest.mark.asyncio
async def test_cohort_discovery_runs_once_per_interval_and_only_when_configured(state, monkeypatch):
    runs = []

    async def fake_run_prospecting(state_, config, provider, queries, **kw):
        runs.append((provider, sorted({q.cohort for q in queries})))
        return SimpleNamespace(discover={"new_companies": 3, "observations": 9}, errors=[],
                               summary_line=lambda: "3 new companies")
    monkeypatch.setattr("harvey.pipeline.run_prospecting", fake_run_prospecting)

    off = make_scout(state, strategies=["cohort_discovery"], cohorts=[], discovery_every_hours=24,
                     job_limit=10, max_new_prospects_per_day=0)
    await off.run()
    assert runs == []                                   # no cohorts configured: nothing searched

    on = make_scout(state, strategies=["cohort_discovery"], cohorts=["A", "B"], discovery_every_hours=24,
                    job_limit=10, max_new_prospects_per_day=0)
    await on.run()
    await on.run()
    assert runs == [("jobboards", ["A", "B"])]          # searched once, not on the second cycle


@pytest.mark.asyncio
async def test_daily_cap_stops_new_strategies_but_not_the_owners_hand_added_people(state, monkeypatch):
    calls = []
    for i in range(3):
        await state.add_prospect(Prospect(first_name=f"A{i}", last_name="Auto", company=f"Co{i}",
                                          email=f"a{i}@co{i}.com", source="known_company_backfill", email_status="verified"))
    await state.add_prospect(Prospect(first_name="Bad", last_name="Email", company="BadCo",
                                      email="b@badco.com", source="known_company_backfill", email_status="invalid"))
    await state.add_prospect(Prospect(first_name="Hand", last_name="Added", company="HandCo",
                                      email="h@handco.com", source="manual"))
    scout = make_scout(state, strategies=["hunter", "known_companies"], cohorts=[], discovery_every_hours=24,
                       job_limit=10, max_new_prospects_per_day=3)
    stub_strategies(scout, monkeypatch, calls)
    await scout.run()
    assert calls == []                                  # 3 verified today = the cap; the manual and the invalid one do not count

    roomy = make_scout(state, strategies=["hunter", "known_companies"], cohorts=[], discovery_every_hours=24,
                       job_limit=10, max_new_prospects_per_day=4)
    stub_strategies(roomy, monkeypatch, calls)
    await roomy.run()
    assert sorted(calls) == ["_prospect_via_hunter", "_prospect_via_known_companies"]


def test_icp_titles_match_whole_words_so_every_director_is_not_a_cto(state):
    scout = make_scout(state)
    scout.config.icp = SimpleNamespace(titles=["CIO", "CTO", "COO", "Head of Data", "Chief Executive Officer"], industries=["x"])
    for good in ("CTO", "Chief Technology Officer / CTO", "Co-Founder and COO", "Head of Data & Analytics",
                 "Chief Executive Officer"):
        assert scout._title_matches_icp(good), good
    for bad in ("Quality Assurance Director", "Family Services Director", "Director of Facilities",
                "Program Director", "Associate Director", "Cooperative Manager"):
        assert not scout._title_matches_icp(bad), bad


@pytest.mark.asyncio
async def test_one_buyer_per_company_by_default_and_the_most_senior_first(state):
    scout = make_scout(state, max_prospects_per_company=1)
    cid = "co1"
    assert await scout._company_room(cid) == 1
    await state.add_prospect(Prospect(first_name="A", last_name="One", company="Co", company_id=cid,
                                      email="a@co.com", status="new"))
    assert await scout._company_room(cid) == 0
    lost = (await state.get_prospect_by_email("a@co.com")).id
    await state.update_prospect_status(lost, "lost")
    assert await scout._company_room(cid) == 1                 # a dropped person frees the slot
    assert scout._seniority_rank("Chief Executive Officer") < scout._seniority_rank("VP of IT") \
        < scout._seniority_rank("Director of IT") < scout._seniority_rank("Manager")
    assert await make_scout(state, max_prospects_per_company=0)._company_room(cid) > 100


@pytest.mark.asyncio
async def test_excluded_domains_and_nonprofit_keywords_keep_a_company_off_the_list(state):
    from harvey.config import ScoutConfig
    cfg = ScoutConfig(exclude_domains=["outreach.example.org"])
    scout = make_scout(state, exclude_domains=cfg.exclude_domains, exclude_keywords=cfg.exclude_keywords)
    assert "excluded domain" in await scout._company_excluded({"name": "Example Outreach", "domain": "www.outreach.example.org"})
    assert await scout._company_excluded({"name": "Example Pet Health Foundation", "domain": "pethealth.example.org"})
    assert await scout._company_excluded({"name": "Valley Services", "industry": "A 501(c)(3) nonprofit"})
    assert await scout._company_excluded({"name": "Rio Migrant Services", "domain": "rio.com"})
    for fine in ({"name": "Example Mutual Benefit Administrators", "domain": "emba.example"},
                 {"name": "Contoso Health Administrative Office"},
                 {"name": "Example Mutual Insurance", "industry": "insurance"},
                 {"name": "Acme Electric", "domain": "acmeelectric.example"}):
        assert await scout._company_excluded(fine) == "", fine


@pytest.mark.asyncio
async def test_name_only_companies_get_a_website_or_rotate_out(tmp_path, monkeypatch):
    from harvey.models.company import Company
    from harvey.state import StateManager
    import harvey.collectors.domain_resolver as dr

    st = StateManager(db_path=str(tmp_path / "s.db"))
    await st.init_db()
    a = await st.add_company(Company(name="Fabrikam Health", domain="", source="discover:jobboards", external_id="jobboards:fabrikam"))
    b = await st.add_company(Company(name="Nowhere Co", domain="", source="discover:jobboards", external_id="jobboards:nowhere"))

    async def fake_resolve(names, **kw):
        return {a: "fabrikamhealth.com"}
    monkeypatch.setattr(dr, "resolve_many", fake_resolve)

    from harvey.agents.scout import Scout
    scout = Scout.__new__(Scout)
    scout.state = st
    await scout._resolve_company_domains()

    assert (await st.get_company(a)).domain == "fabrikamhealth.com"
    ready = [c["id"] for c in await st.companies_needing_prospects(limit=10)]
    assert ready == [a]                                   # resolved company is now workable
    assert await st.companies_needing_domain() == []      # the failure rotated out, no retry loop


@pytest.mark.asyncio
async def test_contacts_work_the_most_senior_person_first_and_stop_at_one_verified(state, monkeypatch):
    scout = make_scout(state, max_prospects_per_company=1)
    scout.config.channels.email = SimpleNamespace(send_to_risky=False)
    scout._seen_prospect_keys, scout._seen_domains = set(), set()

    async def team(domain):
        return [{"first_name": "Dan", "last_name": "Dir", "title": "VP Operations"},
                {"first_name": "Cee", "last_name": "Oh", "title": "CTO"},
                {"first_name": "Ann", "last_name": "Ceo", "title": "Chief Executive Officer"},
                {"first_name": "Bo", "last_name": "Coo", "title": "COO"}]
    resolved = []

    async def resolve(first, last, domain):
        resolved.append(first)
        return f"{first.lower()}@{domain}", "verified" if first == "Cee" else "invalid"
    monkeypatch.setattr(scout, "_scrape_team_page", team)
    monkeypatch.setattr(scout, "_resolve_email", resolve)
    scout.config.icp.titles = ["CTO", "COO", "Chief Executive Officer", "VP Operations"]

    got = await scout._contacts_from_company("c1", "acme.com", "Acme", "", max_deliverable=1)
    assert [c.first_name for c in got if c.email_status == "verified"] == ["Cee"]
    assert "Dan" not in resolved                       # stopped once a verified address was found
    assert len(resolved) <= 3


@pytest.mark.asyncio
async def test_backfill_keeps_the_verified_person_over_a_more_senior_invalid_one(state, monkeypatch):
    scout = make_scout(state, max_prospects_per_company=1, max_new_prospects_per_day=5)
    scout.config.channels.email = SimpleNamespace(send_to_risky=False)
    scout._remaining_today = 5

    async def companies(limit=10, **k):
        return [{"id": "c1", "name": "Acme", "domain": "acme.com", "industry": ""}]

    async def contacts(**k):
        return [Prospect(first_name="Ann", last_name="Ceo", email="ann@acme.com", email_status="invalid",
                         company="Acme", title="Chief Executive Officer", seniority="c_suite", source="t"),
                Prospect(first_name="Cee", last_name="Oh", email="cee@acme.com", email_status="verified",
                         company="Acme", title="CTO", seniority="c_suite", source="t")]

    async def excluded(company):
        return ""

    async def room(cid):
        return 1
    added = []

    async def add(p):
        added.append(p)
    monkeypatch.setattr(state, "companies_needing_prospects", companies)
    monkeypatch.setattr(state, "mark_prospects_checked", lambda cid: _noop())
    monkeypatch.setattr(state, "add_prospect", add)
    monkeypatch.setattr(scout, "_contacts_from_company", contacts)
    monkeypatch.setattr(scout, "_company_excluded", excluded)
    monkeypatch.setattr(scout, "_company_room", room)

    found = await scout._prospect_via_known_companies()
    assert [p.first_name for p in added] == ["Cee"] and found == 1


async def _noop():
    return None
