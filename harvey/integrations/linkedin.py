"""LinkedIn browser automation via Playwright.

Safety-first: humanlike randomized delays, hard daily activity caps
(persisted across restarts), and consistent browser fingerprint to
protect the user's LinkedIn account from restriction.
"""

import asyncio
import hashlib
import json
import logging
import platform
import random
import re
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from harvey.integrations import linkedin_selectors as sel

logger = logging.getLogger("harvey.linkedin")


START_TIMEOUT_SECONDS = 90


def reap_stale_playwright(max_age_seconds: int = 2400) -> int:
    """Kill Playwright drivers this process started more than `max_age_seconds` ago, with their
    browsers. A healthy session lasts well under that (25 minute session cap); anything older
    leaked when a cycle was cut off mid-session. Returns how many drivers were killed."""
    import os
    import signal
    tick = os.sysconf("SC_CLK_TCK")
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        return 0
    kids: dict[int, list[int]] = {}
    info: dict[int, tuple[int, float]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            rest = stat[stat.rindex(")") + 2:].split()
            ppid, start = int(rest[1]), int(rest[19]) / tick
        except (OSError, ValueError, IndexError):
            continue
        info[int(d.name)] = (ppid, start)
        kids.setdefault(ppid, []).append(int(d.name))

    def tree(pid: int) -> list[int]:
        out = [pid]
        for c in kids.get(pid, []):
            out += tree(c)
        return out

    killed = 0
    for pid in kids.get(os.getpid(), []):
        try:
            cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            continue
        if "playwright/driver" not in cmd or uptime - info[pid][1] < max_age_seconds:
            continue
        for victim in reversed(tree(pid)):
            try:
                os.kill(victim, signal.SIGKILL)
            except OSError:
                pass
        killed += 1
    if killed:
        logger.warning(f"Reaped {killed} stale Playwright driver(s) left by an interrupted session.")
    return killed


class LinkedInTripped(Exception):
    """LinkedIn showed a challenge, rate limit or restriction, or the page
    wasn't what it has to be. Stop everything; never retry blindly."""


class LinkedInSelectorMiss(LinkedInTripped):
    """An element that must exist after a step we just took is missing. That
    means the UI changed under us, so it is handled exactly like a trip."""


class LinkedInSessionExpired(Exception):
    """Logged out. Needs `harvey linkedin login`; not a reason to alarm."""


class LinkedInUnavailable(Exception):
    """This one prospect can't be actioned (restricted profile, email required
    to connect, not connected yet). Not a trip."""

from harvey.paths import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
COOKIES_PATH = DATA_DIR / "linkedin_cookies.json"
ACTIVITY_PATH = DATA_DIR / "linkedin_activity.json"

def _user_agent(browser_version: str = "120.0.0.0") -> str:
    """A user agent that matches the machine and browser actually running.
    Claiming macOS from a Linux box is itself a fingerprint mismatch."""
    system = platform.system()
    if system == "Darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    elif system == "Windows":
        os_part = "Windows NT 10.0; Win64; x64"
    else:
        os_part = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{browser_version} Safari/537.36")

# Hard daily caps — conservative on purpose. LinkedIn restricts accounts
# that view too many profiles or run too many searches per day.
MAX_DAILY_SEARCH_PAGES = 20
MAX_DAILY_PROFILE_VIEWS = 40
MAX_SEARCH_PAGES_PER_QUERY = 5


class LinkedInAutomation:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._activity = self._load_activity()

    # ── Daily activity caps ──

    @staticmethod
    def _load_activity() -> dict:
        """Load today's activity counters (persisted so restarts don't reset caps)."""
        today = date.today().isoformat()
        try:
            if ACTIVITY_PATH.exists():
                data = json.loads(ACTIVITY_PATH.read_text())
                if isinstance(data, dict) and data.get("date") == today:
                    return {
                        "date": today,
                        "search_pages": int(data.get("search_pages", 0)),
                        "profile_views": int(data.get("profile_views", 0)),
                    }
        except (ValueError, OSError) as e:
            logger.debug(f"Could not load LinkedIn activity log: {e}")
        return {"date": today, "search_pages": 0, "profile_views": 0}

    def _save_activity(self):
        try:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            ACTIVITY_PATH.write_text(json.dumps(self._activity))
        except OSError as e:
            logger.debug(f"Could not save LinkedIn activity log: {e}")

    def _record(self, kind: str):
        # Roll over if the date changed mid-run
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        self._activity[kind] = self._activity.get(kind, 0) + 1
        self._save_activity()

    def _cap_reached(self, kind: str, cap: int) -> bool:
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        if self._activity.get(kind, 0) >= cap:
            logger.warning(
                f"LinkedIn daily cap reached for {kind} "
                f"({cap}/day). Skipping to protect the account."
            )
            return True
        return False

    # ── Humanlike behavior ──

    async def _random_delay(self, min_s: float = 2.0, max_s: float = 6.0):
        """Human-like random delay between actions, with occasional long pauses."""
        delay = random.uniform(min_s, max_s)
        # ~10% of the time, take a noticeably longer "reading" pause
        if random.random() < 0.10:
            delay += random.uniform(4.0, 10.0)
        await asyncio.sleep(delay)

    async def _humanlike_scroll(self):
        """Scroll the page in a few small increments like a human reading."""
        if not self.page:
            return
        try:
            for _ in range(random.randint(2, 4)):
                await self.page.mouse.wheel(0, random.randint(250, 700))
                await asyncio.sleep(random.uniform(0.4, 1.4))
        except Exception as e:
            logger.debug(f"Scroll simulation failed: {e}")

    async def _wait_settled(self, timeout: int = 15000):
        """Wait for network idle without crashing on busy pages."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            # Feed-like pages never go fully idle; that's fine.
            pass

    # ── Lifecycle ──

    async def start(self):
        """Launch browser and set up context."""
        from playwright.async_api import async_playwright

        # Both calls can hang forever when the machine is thrashing; a hang here once froze the
        # whole heartbeat. Bound each, and let the caller's cleanup (stop + reaper) deal with it.
        self._pw = await asyncio.wait_for(async_playwright().start(), START_TIMEOUT_SECONDS)
        self.browser = await asyncio.wait_for(
            self._pw.chromium.launch(headless=True), START_TIMEOUT_SECONDS)

        # Always use the same fingerprint (viewport + UA), whether or not
        # we have saved cookies — a changing fingerprint is a red flag.
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_user_agent(self.browser.version),
        )

        # Load cookies if we have them (avoids re-login)
        if COOKIES_PATH.exists():
            try:
                cookies = json.loads(COOKIES_PATH.read_text())
                if isinstance(cookies, list):
                    await self.context.add_cookies(cookies)
                    logger.info("Loaded saved LinkedIn cookies.")
            except Exception as e:
                logger.warning(f"Could not load saved LinkedIn cookies: {e}")

        self.page = await self.context.new_page()

    def _save_cookies_sync(self, cookies: list):
        try:
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(cookies))
            try:
                COOKIES_PATH.chmod(0o600)  # session cookies are credentials
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"Could not save LinkedIn cookies: {e}")

    async def stop(self):
        """Save cookies and close browser."""
        try:
            if self.context:
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                logger.info("Saved LinkedIn cookies.")
        except Exception as e:
            logger.debug(f"Could not save cookies on shutdown: {e}")
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        finally:
            self.browser = None
            self.context = None
            self.page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.debug(f"Error stopping Playwright: {e}")
        finally:
            self._pw = None

    # ── Auth ──

    async def login(self) -> bool:
        """Log into LinkedIn."""
        if not self.page:
            await self.start()

        if not self.email or not self.password:
            logger.warning("LinkedIn credentials not configured; cannot log in.")
            return False

        try:
            await self.page.goto(
                "https://www.linkedin.com/login", timeout=30000
            )
            await self._wait_settled()
        except Exception as e:
            logger.error(f"Could not load LinkedIn login page: {e}")
            return False

        await self._random_delay(1, 3)

        # Check if already logged in (cookie session redirected us)
        if "feed" in self.page.url or "mynetwork" in self.page.url:
            logger.info("Already logged into LinkedIn.")
            return True

        try:
            await self.page.fill("#username", self.email)
            await self._random_delay(0.5, 1.5)
            await self.page.fill("#password", self.password)
            await self._random_delay(0.5, 1.5)
            await self.page.click('button[type="submit"]')
            await self._wait_settled()

            if "feed" in self.page.url or "mynetwork" in self.page.url:
                logger.info("Successfully logged into LinkedIn.")
                # Save cookies for future sessions
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                return True

            # Check for security challenge
            if "checkpoint" in self.page.url or "challenge" in self.page.url:
                logger.warning(
                    "LinkedIn security checkpoint detected. "
                    "Please log in manually and save cookies."
                )
                return False

            logger.warning(f"Login may have failed. Current URL: {self.page.url}")
            return False

        except Exception as e:
            # Never include credentials in logged errors.
            msg = str(e).replace(self.password, "***") if self.password else str(e)
            logger.error(f"LinkedIn login failed: {msg}")
            return False

    # ── Session (cookie-only: sending never types a password) ──

    async def ensure_session(self) -> None:
        """Open the saved session or say why we can't. Deliberately does not
        fall back to password login: a fresh login from a new machine is what
        triggers LinkedIn's checkpoint."""
        if not COOKIES_PATH.exists():
            raise LinkedInSessionExpired(
                "no saved LinkedIn session; run `harvey linkedin login`")
        if not self.page:
            await self.start()
        await self._goto(sel.FEED_URL)
        if "/feed" not in self.page.url:
            raise LinkedInSessionExpired(f"unexpected landing page: {self.page.url}")

    # ── Guarded navigation ──

    async def _goto(self, url: str) -> None:
        try:
            resp = await self.page.goto(url, timeout=30000)
        except Exception as e:
            raise LinkedInTripped(f"navigation to {url} failed: {e}") from e
        await self._wait_settled()
        await self._check_page(status=resp.status if resp else None)

    async def _alert_text(self) -> str:
        try:
            parts = await self.page.locator(sel.DIALOG_OR_ALERT).all_inner_texts()
            return " ".join(parts)[:3000]
        except Exception:
            return ""

    async def _check_page(self, status: int | None = None) -> None:
        verdict = sel.detect_trip(self.page.url, await self._alert_text(), status)
        if verdict.kind == "expired":
            raise LinkedInSessionExpired(verdict.reason)
        if verdict.kind == "trip":
            raise LinkedInTripped(verdict.reason)

    # ── UI primitives (overridden in tests) ──

    def _card(self):
        """The profile's top card: the <section> that holds the person's name.

        LinkedIn's profile page has no <h1> and the old "first <section> in <main>" now wraps the whole page (post
        buttons and all), so the name heading is the stable anchor (checked live 2026-11-12)."""
        return self.page.locator(sel.PROFILE_HEADING).first.locator("xpath=ancestor::section[1]")

    async def _scope_text(self) -> str:
        return (await self._card().inner_text())[:1500]

    async def _card_labels(self) -> list[str]:
        """Accessible names of the top card's actions. Connect and Message are links (<a>), Follow and More are
        buttons, so both are read."""
        return await self._card().evaluate(
            """el => Array.from(el.querySelectorAll('button, a'))
                 .filter(b => b.offsetParent !== null)
                 .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())
                 .filter(Boolean)""")

    async def _click_card_button(self, pattern) -> None:
        card = self._card()
        for role in ("button", "link"):
            loc = card.get_by_role(role, name=pattern)
            if await loc.count():
                await loc.first.click(timeout=8000)
                return
        raise LinkedInUnavailable("expected action not on the profile card")

    async def _open_more_menu_connect(self) -> bool:
        """Connect hidden under 'More'. Returns whether we clicked it.

        The menu fills in a moment after the click and a second attempt succeeds where the first sometimes does not
        (seen live), so wait for the item to be visible and retry the menu once before giving up."""
        for _ in range(2):
            await self._click_card_button(sel.MORE_BUTTON)
            await self._random_delay(0.8, 1.8)
            item = self.page.get_by_role("menuitem", name=sel.CONNECT_MENU_ITEM).first
            try:
                await item.wait_for(state="visible", timeout=6000)
                await item.click(timeout=4000)
                return True
            except Exception:
                await self.page.keyboard.press("Escape")
                await self._random_delay(0.6, 1.2)
        return False

    async def _dialog_text(self) -> str:
        dialog = self.page.get_by_role("dialog").first
        await dialog.wait_for(state="visible", timeout=8000)
        return await dialog.inner_text()

    async def _click_in_dialog(self, pattern) -> None:
        await self.page.get_by_role("dialog").first.get_by_role(
            "button", name=pattern).first.click(timeout=8000)

    async def _type_in_dialog(self, text: str) -> None:
        box = self.page.get_by_role("dialog").first.get_by_role("textbox").first
        await box.click(timeout=8000)
        await box.press_sequentially(text, delay=random.uniform(40, 110))

    async def _dialog_closed(self) -> bool:
        try:
            await self.page.get_by_role("dialog").first.wait_for(state="hidden", timeout=8000)
            return True
        except Exception:
            return False

    async def _compose_and_send(self, text: str) -> bool:
        """Type into the open message box and press Send. True if the box emptied."""
        box = self.page.get_by_role("textbox", name=sel.COMPOSE_BOX).last
        await box.wait_for(state="visible", timeout=10000)
        await box.click()
        await box.press_sequentially(text, delay=random.uniform(40, 110))
        await self._random_delay(1.0, 2.5)
        await self.page.get_by_role("button", name=re.compile(r"^send$", re.I)).last.click(timeout=8000)
        await asyncio.sleep(1.5)
        return not (await box.inner_text()).strip()

    # ── Profile state ──

    async def _open_profile(self, url: str) -> None:
        await self._goto(url)
        try:
            await self.page.locator(sel.PROFILE_HEADING).first.wait_for(
                state="visible", timeout=8000)
        except Exception as e:
            raise LinkedInUnavailable("profile did not load (removed or restricted)") from e
        self._record("profile_views")
        await self._random_delay(2.0, 4.5)
        await self._humanlike_scroll()

    async def _connection_state(self) -> str:
        return sel.classify_connection(
            await self._card_labels(), sel.parse_degree(await self._scope_text()))

    async def check_connection(self, profile_url: str) -> str:
        """connected | pending | none | unknown, for a profile we've invited."""
        await self._open_profile(profile_url)
        state = await self._connection_state()
        return {"connectable": "none", "more": "none"}.get(state, state)

    # ── Connect ──

    async def send_connection_request(self, profile_url: str, note: str = "") -> str:
        """Returns 'sent', 'already_connected' or 'already_pending'."""
        try:
            await self._open_profile(profile_url)
            state = await self._connection_state()
            if state == "connected":
                return "already_connected"
            if state == "pending":
                return "already_pending"
            if state == "unknown":
                raise LinkedInUnavailable("could not tell this profile's connection state")

            if state == "more":
                if not await self._open_more_menu_connect():
                    raise LinkedInUnavailable("Connect is not offered for this profile")
            else:
                await self._click_card_button(sel.CONNECT_BUTTON)
            await self._random_delay(1.0, 2.5)

            try:
                dialog = await self._dialog_text()
            except Exception as e:
                raise LinkedInSelectorMiss("no dialog after clicking Connect") from e
            await self._check_page()
            if re.search(r"email address", dialog, re.I):
                raise LinkedInUnavailable("LinkedIn asks for this person's email to connect")

            if note:
                await self._click_in_dialog(sel.ADD_NOTE_BUTTON)
                await self._random_delay(0.8, 1.8)
                await self._type_in_dialog(note)
                await self._random_delay(1.0, 2.5)
                await self._click_in_dialog(sel.SEND_BUTTON)
            else:
                await self._click_in_dialog(re.compile(
                    sel.SEND_WITHOUT_NOTE_BUTTON.pattern + "|" + sel.SEND_BUTTON.pattern, re.I))

            if not await self._dialog_closed():
                await self._check_page()
                raise LinkedInSelectorMiss("invite dialog did not close after Send")
            await self._check_page()
            return "sent"
        except (LinkedInTripped, LinkedInSessionExpired, LinkedInUnavailable):
            raise
        except Exception as e:  # a locator timeout etc: the page isn't what we expect
            raise LinkedInSelectorMiss(f"connect flow failed: {str(e)[:160]}") from e

    # ── Message ──

    async def send_message(self, target_url: str, text: str) -> str:
        """Send in an existing 1st-degree connection or an existing thread."""
        try:
            if "/messaging/thread/" in target_url:
                await self._goto(target_url)
            else:
                await self._open_profile(target_url)
                if await self._connection_state() != "connected":
                    raise LinkedInUnavailable("not connected to this person yet")
                await self._click_card_button(sel.MESSAGE_BUTTON)
                await self._random_delay(1.2, 2.8)
            if not await self._compose_and_send(text):
                raise LinkedInSelectorMiss("message box did not clear after Send")
            await self._check_page()
            return "sent"
        except (LinkedInTripped, LinkedInSessionExpired, LinkedInUnavailable):
            raise
        except Exception as e:
            raise LinkedInSelectorMiss(f"message flow failed: {str(e)[:160]}") from e

    # ── Inbox ──

    async def read_inbox(self, max_threads: int = 8) -> list[dict]:
        """Unread conversations whose last message is from the other person.

        Each: {thread_url, name, profile_url, text, provider_id}. Opening a
        thread marks it read on LinkedIn; that can't be avoided."""
        try:
            await self._goto(sel.INBOX_URL)
            await self._random_delay(2.0, 4.0)
            rows = await self.page.locator(sel.CONVERSATION_ITEM).evaluate_all(
                """(items, args) => items.map(li => {
                      const a = li.querySelector(args.link);
                      const name = (li.querySelector('h3, .msg-conversation-listitem__participant-names')
                                    || {}).innerText || '';
                      return {href: a ? a.href : '', name: name.trim(),
                              unread: !!li.querySelector(args.unread)};
                  })""", {"link": sel.CONVERSATION_LINK, "unread": sel.UNREAD_MARKER})
            out = []
            for row in [r for r in rows if r["unread"] and r["href"]][:max_threads]:
                await self._random_delay(3.0, 7.0)
                await self._goto(row["href"])
                item = await self._read_open_thread(row["name"])
                if item:
                    item["thread_url"] = row["href"]
                    item["provider_id"] = "li:" + row["href"] + ":" + hashlib.sha1(
                        item["text"].encode()).hexdigest()[:12]
                    out.append(item)
            return out
        except (LinkedInTripped, LinkedInSessionExpired):
            raise
        except Exception as e:
            raise LinkedInSelectorMiss(f"inbox read failed: {str(e)[:160]}") from e

    async def _read_open_thread(self, other_name: str) -> dict | None:
        data = await self.page.evaluate(
            """(a) => {
                const groups = Array.from(document.querySelectorAll(a.group));
                const last = groups[groups.length - 1];
                if (!last) return null;
                const who = (last.querySelector(a.name) || {}).innerText || '';
                const bodies = Array.from(last.querySelectorAll(a.body)).map(b => b.innerText.trim());
                const link = document.querySelector(a.profile);
                return {who: who.trim(), text: bodies.join('\n').trim(),
                        profile: link ? link.href.split('?')[0] : ''};
            }""", {"group": sel.MESSAGE_GROUP, "name": sel.MESSAGE_GROUP_NAME,
                   "body": sel.MESSAGE_BODY, "profile": sel.THREAD_HEADER_PROFILE_LINK})
        if not data or not data["text"] or not data["profile"]:
            return None
        # Only messages from them: the last group's author is the other person.
        if other_name and data["who"].split(" ")[0].lower() != other_name.split(" ")[0].lower():
            return None
        return {"name": other_name, "profile_url": data["profile"], "text": data["text"]}

    # ── Search ──

    async def search_people(
        self,
        keywords: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
        max_results: int = 25,
    ) -> list[dict]:
        """Search LinkedIn for people matching criteria.

        Returns list of dicts with: name, title, company, linkedin_url, location
        """
        if not self.page:
            await self.start()
            if not await self.login():
                return []

        # Fold all criteria into the keyword search (URL-encoded).
        terms = " ".join(t for t in [keywords, title, company, location] if t).strip()
        if not terms:
            logger.warning("search_people called with no criteria; skipping.")
            return []

        search_url = (
            "https://www.linkedin.com/search/results/people/"
            f"?keywords={quote_plus(terms)}"
        )

        results = []
        page_num = 1

        while len(results) < max_results:
            if self._cap_reached("search_pages", MAX_DAILY_SEARCH_PAGES):
                break

            url = f"{search_url}&page={page_num}"
            try:
                await self.page.goto(url, timeout=30000)
            except Exception as e:
                logger.warning(f"Failed to load search page {page_num}: {e}")
                break

            self._record("search_pages")
            await self._random_delay(3, 6)
            await self._wait_settled()
            await self._humanlike_scroll()

            # Extract search result cards
            cards = await self.page.query_selector_all("div.entity-result__item")

            if not cards:
                # Try alternate selector
                cards = await self.page.query_selector_all(
                    "li.reusable-search__result-container"
                )

            if not cards:
                logger.info(f"No results found on page {page_num}.")
                break

            for card in cards:
                if len(results) >= max_results:
                    break

                try:
                    profile = await self._extract_search_card(card)
                    if profile:
                        results.append(profile)
                except Exception as e:
                    logger.debug(f"Error extracting search card: {e}")
                    continue

            page_num += 1
            await self._random_delay(4, 8)

            # Safety: don't paginate deep on a single query
            if page_num > MAX_SEARCH_PAGES_PER_QUERY:
                break

        logger.info(f"Found {len(results)} LinkedIn profiles.")
        return results

    async def _extract_search_card(self, card) -> Optional[dict]:
        """Extract person info from a LinkedIn search result card."""
        # Get profile link
        link_el = await card.query_selector("a.app-aware-link")
        if not link_el:
            return None
        href = await link_el.get_attribute("href")
        if not href or "/in/" not in href:
            return None
        linkedin_url = href.split("?")[0]

        # Get name
        name_el = await card.query_selector("span.entity-result__title-text a span")
        name = await name_el.inner_text() if name_el else ""
        name = name.strip()

        # Get title/headline
        title_el = await card.query_selector("div.entity-result__primary-subtitle")
        title_text = await title_el.inner_text() if title_el else ""
        title_text = title_text.strip()

        # Get location
        loc_el = await card.query_selector("div.entity-result__secondary-subtitle")
        location = await loc_el.inner_text() if loc_el else ""
        location = location.strip()

        # Split name
        parts = name.split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        # Try to extract company from title (often "Title at Company")
        company = ""
        if " at " in title_text:
            title_text, company = (
                s.strip() for s in title_text.split(" at ", 1)
            )

        return {
            "first_name": first_name,
            "last_name": last_name,
            "title": title_text,
            "company": company,
            "linkedin_url": linkedin_url,
            "location": location,
        }

    # ── Profiles ──

    async def extract_profile(self, linkedin_url: str) -> Optional[dict]:
        """Scrape a LinkedIn profile page for detailed info."""
        if not self.page:
            await self.start()
            if not await self.login():
                return None

        if self._cap_reached("profile_views", MAX_DAILY_PROFILE_VIEWS):
            return None

        try:
            await self.page.goto(linkedin_url, timeout=30000)
        except Exception as e:
            logger.warning(f"Failed to load profile {linkedin_url}: {e}")
            return None

        self._record("profile_views")
        await self._random_delay(3, 5)
        await self._wait_settled()
        await self._humanlike_scroll()

        try:
            # Name
            name_el = await self.page.query_selector("h1")
            name = await name_el.inner_text() if name_el else ""

            # Headline
            headline_el = await self.page.query_selector("div.text-body-medium")
            headline = await headline_el.inner_text() if headline_el else ""

            # Location
            loc_el = await self.page.query_selector("span.text-body-small.inline")
            location = await loc_el.inner_text() if loc_el else ""

            # About section
            about_el = await self.page.query_selector(
                "section.pv-about-section div.inline-show-more-text"
            )
            about = await about_el.inner_text() if about_el else ""

            parts = name.strip().split(" ", 1)

            return {
                "first_name": parts[0] if parts else "",
                "last_name": parts[1] if len(parts) > 1 else "",
                "headline": headline.strip(),
                "location": location.strip(),
                "about": about.strip(),
                "linkedin_url": linkedin_url,
            }

        except Exception as e:
            logger.error(f"Error extracting profile {linkedin_url}: {e}")
            return None



async def interactive_login(timeout_seconds: int = 300) -> bool:
    """Open a visible browser so the owner signs in personally (2FA, CAPTCHA and all),
    then save the session cookies for Harvey. Needs a display."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=_user_agent(browser.version),
            )
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login", timeout=30000)
            print("  Sign in to LinkedIn in the window that just opened.")
            print(f"  Waiting up to {timeout_seconds // 60} minutes for you to reach your feed...")
            for _ in range(timeout_seconds // 2):
                if "/feed" in page.url:
                    break
                await asyncio.sleep(2)
            else:
                return False
            await asyncio.sleep(2)
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(await context.cookies()))
            try:
                COOKIES_PATH.chmod(0o600)
            except OSError:
                pass
            return True
        finally:
            await browser.close()
