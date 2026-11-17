import pytest

from harvey.collectors.leadership import extract_people, find_leaders, leadership_links, split_name

CARDS = """
<html><body><nav><a href="/about">About</a></nav>
<div class="grid">
  <div class="c"><h3>Jane Q. Smith</h3><p>Chief Technology Officer</p></div>
  <div class="c"><strong>Rahul Patel, MBA</strong><span>VP Operations</span></div>
  <div class="c"><h3>Our Team</h3><p>Meet the leadership</p></div>
</div>
<ul><li>Maria Gonzalez - Co-Founder &amp; CEO</li></ul>
<h4>Who Are</h4><p>CIO-SP3 SB</p>
<p>Our leadership team has over 100 years of combined experience as a chief officer.</p>
</body></html>"""

LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Person","name":"Dr. Ana Lopez","jobTitle":"Chief Information Officer"},
{"@type":"Person","name":"Tim Ng","jobTitle":"Intern"}]}</script></head><body></body></html>"""


def names(people):
    return {(p["first_name"], p["last_name"]): p["title"] for p in people}


def test_cards_and_name_title_lines_are_read_and_junk_is_not():
    got = names(extract_people(CARDS))
    assert got[("Jane", "Smith")] == "Chief Technology Officer"
    assert got[("Rahul", "Patel")] == "VP Operations"
    assert got[("Maria", "Gonzalez")].startswith("Co-Founder")
    assert not any(k[0] in ("Our", "Who") for k in got)
    assert not any("CIO-SP3" in t for t in got.values())


def test_json_ld_person_markup_needs_a_leadership_title():
    got = names(extract_people(LD))
    assert got == {("Ana", "Lopez"): "Chief Information Officer"}


def test_split_name_strips_credentials_and_rejects_non_names():
    assert split_name("Dr. Jane Q. Smith, MBA") == ("Jane", "Smith")
    assert split_name("Our Team") is None
    assert split_name("Learn More") is None
    assert split_name("Cher") is None


def test_leadership_links_prefers_leadership_pages_on_the_same_site():
    html = ('<a href="/about">About</a><a href="/company/leadership">Leadership</a>'
            '<a href="https://other.com/team">Team</a><a href="/blog">Blog</a>')
    assert leadership_links(html, "https://acme.com")[0] == "https://acme.com/company/leadership"
    assert all("other.com" not in u for u in leadership_links(html, "https://acme.com"))


class _Resp:
    def __init__(self, text, url):
        self.text, self.url = text, url


@pytest.mark.asyncio
async def test_find_leaders_follows_the_link_from_the_homepage():
    pages = {
        "https://acme.com": _Resp('<a href="/who-we-are/leadership">Leadership</a>', "https://acme.com"),
        "https://acme.com/who-we-are/leadership": _Resp(CARDS, "https://acme.com/who-we-are/leadership"),
    }

    async def fetch(url):
        return pages.get(url)

    people = await find_leaders("https://acme.com", fetch)
    assert ("Jane", "Smith") in names(people)


@pytest.mark.asyncio
async def test_find_leaders_returns_nothing_when_no_page_answers():
    async def fetch(url):
        return None

    assert await find_leaders("https://acme.com", fetch) == []


def test_contract_vehicles_are_not_people():
    html = "<div><h4>GSA List</h4><p>CIO•SP3 SB IDIQ</p></div><div><h4>Jane Roe</h4><p>CIO</p></div>"
    assert names(extract_people(html)) == {("Jane", "Roe"): "CIO"}
