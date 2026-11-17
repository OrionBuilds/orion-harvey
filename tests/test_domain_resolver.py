import httpx
import pytest

from harvey.collectors import domain_resolver as dr


def test_candidate_hosts_drop_legal_words_and_stay_bounded():
    hosts = dr.candidate_hosts("Riot Platforms, Inc.")
    assert hosts[0] == "riotplatforms.com"
    assert len(hosts) <= dr.MAX_CANDIDATES
    assert dr.candidate_hosts("Confidential Company") == []
    assert dr.candidate_hosts("") == []


def test_page_must_name_the_company_and_not_be_parked():
    assert dr.page_matches("Fabrikam Health", "<title>Fabrikam Health | Care</title><body>welcome</body>")
    assert not dr.page_matches("Fabrikam Health", "<title>Other</title><body>nothing</body>")
    assert not dr.page_matches("Fabrikam Health", "<title>Fabrikam Health</title><body>This domain is for sale</body>")
    # short names need to be in the title, not just somewhere in the page
    assert not dr.page_matches("POSH", "<title>Home</title><body>posh things posh</body>")
    assert dr.page_matches("POSH", "<title>POSH - events</title>")


def _transport(pages: dict[str, tuple[int, str, str | None]]):
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host not in pages:
            raise httpx.ConnectError("no such host", request=req)
        status, html, redirect = pages[host]
        if redirect:
            return httpx.Response(302, headers={"location": redirect}, request=req) \
                if req.url.host != httpx.URL(redirect).host else httpx.Response(200, text=html, request=req)
        return httpx.Response(status, text=html, headers={"content-type": "text/html"}, request=req)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_resolves_confirmed_site_and_rejects_redirect_to_marketplace():
    pages = {
        "fabrikamhealth.com": (200, "<title>Fabrikam Health</title>", None),
        "latent.com": (302, "", "https://namepros.com/"),
        "namepros.com": (200, "<title>Latent for sale</title>", None),
    }
    out = await dr.resolve_many({"1": "Fabrikam Health", "2": "LATENT"}, transport=_transport(pages))
    assert out == {"1": "fabrikamhealth.com"}
