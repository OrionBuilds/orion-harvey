"""Harvey CLI — simple commands to install, setup, run, and manage Harvey."""

import argparse
import asyncio
import subprocess
import json
import sys
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    """Refuse to install on a Python that cannot run Harvey.

    `python3 -m venv .venv` on macOS builds the venv from /usr/bin/python3,
    which is 3.9 — old enough that Harvey's `X | Y` type syntax fails at
    import. The install itself appears to succeed and the failure surfaces
    later as an unrelated-looking ImportError, so check up front and name the
    fix.
    """
    if sys.version_info >= MIN_PYTHON:
        return

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Harvey needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    print("  On macOS, `python3` is usually the system 3.9, so a venv built")
    print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
    print("    brew install python@3.13")
    print("    rm -rf .venv")
    print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
    print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Harvey dependencies...\n")

    # Install Python packages
    print("  [1/2] Installing Python packages...")
    requirements = Path(args._project_root) / "requirements.txt"
    if requirements.exists():
        pip_args = ["-r", "requirements.txt"]
    else:
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-e", "."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_args],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        print("  Tip: make sure you're inside a virtualenv "
              "(python3 -m venv .venv && source .venv/bin/activate).")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    _ensure_importable(args._project_root)
    print("  Harvey is installed. Run 'harvey setup' next.\n")


def _ensure_importable(project_root: str):
    """Make sure `import harvey` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import harvey"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "harvey"
        target = Path(project_root) / "harvey"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import harvey"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import harvey' fails outside the project directory.")
    print("  Run harvey commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from harvey.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Harvey's heartbeat loop."""
    from harvey.main import main

    main()


def cmd_train(args):
    """Train Harvey on a website."""
    from harvey.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    from pydantic import ValidationError
    from harvey.config import BrainConfig, ConfigError, load_brain_config

    try:
        brain_config = load_brain_config()
        overrides = {
            k: v for k, v in (
                ("provider", args.provider),
                ("model", args.model),
                ("reasoning_effort", args.reasoning_effort),
            ) if v is not None
        }
        if overrides:
            if "provider" in overrides and overrides["provider"] != brain_config.provider:
                # A different provider never inherits the other one's model.
                brain_config = BrainConfig(**overrides)
            else:
                brain_config = BrainConfig(**{**brain_config.model_dump(), **overrides})
    except (ConfigError, ValidationError) as e:
        print(f"  {e}")
        sys.exit(2)

    trainer = Trainer(brain_config=brain_config)
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from harvey.dashboard import start_dashboard

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    start_dashboard(host=args.host, port=args.port)


def cmd_status(args):
    """Show current pipeline status."""
    from harvey.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Harvey Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        by_provider = summary.get("usage_today_by_provider", {})
        print(f"  Claude calls today:   {by_provider.get('claude', summary['usage_today'])}")
        print(f"  Codex calls today:    {by_provider.get('codex', 0)}")
        from harvey.brain import BRAIN_STATUS_KEY, Brain, active_brain_status
        from harvey.config import ConfigError, load_brain_config
        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            brain_config, config_error = None, e
        # Only the configured provider's health: an old Codex pause says
        # nothing once harvey.yaml has switched to Claude.
        brain_status = active_brain_status(
            await state.get_setting(BRAIN_STATUS_KEY),
            brain_config.provider if brain_config else "")
        healthy = True
        if getattr(args, "check", False):
            if brain_config is None:
                print(f"  Brain:                config error: {config_error}")
                healthy = False
            else:
                brain = Brain(state, brain_config)
                problem = await brain.preflight()
                label = brain.provider_label + (f" ({brain.model})" if brain.model else "")
                if problem:
                    healthy = False
                    print(f"  Brain:                {label} NOT READY")
                    print(f"                        {problem}")
                elif brain.provider == "codex":
                    print(f"  Brain:                {label} ready (CLI, isolation, login, "
                          "model and account checked; no prompt sent)")
                else:
                    print(f"  Brain:                {label} (not checked: Claude is checked "
                          "by its first call)")
        elif brain_status.get("status") == "paused":
            label = {"claude": "Claude", "codex": "Codex"}.get(brain_status.get("provider"), "Brain")
            print(f"  Brain:                {label} PAUSED since {brain_status.get('since', '?')}")
            print(f"                        {brain_status.get('reason', '')}")
        print()
        return healthy

    if not asyncio.run(_status()):
        # `harvey status --check` failing is something scripts can act on.
        raise SystemExit(1)


def cmd_usage(args):
    """Show brain usage: active provider's quota, totals, breakdowns."""
    from harvey.state import StateManager
    from harvey.config import ConfigError, load_brain_config

    async def _usage():
        state = StateManager()
        await state.init_db()

        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            print(f"  {e}")
            brain_config = None

        # Live quota for the ACTIVE provider only (best-effort). Each
        # subscription's gauge is read from its own source and labelled as
        # such; one is never shown as the other.
        windows = None
        provider = brain_config.provider if brain_config else None
        try:
            if provider == "codex":
                from harvey.codex import CodexClient, CodexQuotaClient
                windows = await CodexQuotaClient(
                    CodexClient(model=brain_config.model)).get_utilization()
            elif provider == "claude":
                from harvey.integrations.quota import QuotaClient
                windows = await QuotaClient().get_utilization()
        except Exception:
            pass

        label = {"claude": "Claude", "codex": "Codex"}.get(provider, "Brain")
        print(f"\n  {label} Usage")
        print("  " + "=" * 52)
        if windows:
            labels = {"five_hour": "5-hour window", "seven_day": "Weekly",
                      "limit_reached": "Usage limit"}
            for key, w in windows.items():
                resets = f"  (resets {w['resets_at']})" if w.get("resets_at") else ""
                print(f"  {labels.get(key, key):<16} {w['utilization']:5.1f}% used{resets}")
        elif provider == "codex":
            print("  Quota gauge unavailable (check 'codex login', the Codex CLI "
                  "version, and network).")
        elif provider == "claude":
            print("  Quota gauge unavailable (run 'claude login' or check network).")

        totals = await state.usage_totals()
        print()
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,}"
            )

        by_provider = await state.usage_by_provider(days=args.days)
        if len(by_provider) > 1 or (by_provider and by_provider[0]["provider"] != "claude"):
            print(f"\n  By provider (last {args.days} days):")
            for row in by_provider:
                print(
                    f"    {row['provider']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )
        print(
            "\n  Subscription plans aren't billed per token — these are usage"
            "\n  counts, not costs.\n"
        )

    asyncio.run(_usage())


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_prospects_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all prospects" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} prospect(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Harvey can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def cmd_gmail(args):
    """Gmail provider utilities (auth / test)."""
    from harvey.config import load_env

    env = load_env()
    if args.gmail_action == "auth":
        from harvey.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret)
        sys.exit(0 if ok else 1)

    if args.gmail_action == "test":
        from harvey.config import load_config
        from harvey.integrations.gmail import GmailProvider

        async def _test():
            provider = GmailProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_sms(args):
    """Consent-gated texting: record a person's consent (consent), stage a text for approval (queue), see the setup (status)."""
    import asyncio
    from datetime import datetime, timezone

    from harvey.channels.sms import CONSENT_BASES, E164, SmsChannel
    from harvey.config import load_config, load_env
    from harvey.state import StateManager

    config, env = load_config(), load_env()
    sms_cfg = config.channels.sms

    async def go():
        state = StateManager()
        await state.init_db()
        if args.sms_action == "status":
            channel = SmsChannel(config, env, state)
            print(f"\n  Enabled:        {sms_cfg.enabled}")
            print(f"  Transport:      {sms_cfg.transport} (from {sms_cfg.from_number or 'no number set'})")
            print(f"  Replies/STOP:   {'handled by Orion (Clara)' if sms_cfg.transport == 'os' and sms_cfg.inbound_ready else 'NOT ready: no texting'}")
            print(f"  Ready to send:  {channel.is_configured()}")
            print(f"  Approval:       {'required' if sms_cfg.require_approval else 'off'}; cap {sms_cfg.max_daily_sms}/day; "
                  f"{sms_cfg.window_start}-{sms_cfg.window_end} {sms_cfg.timezone}, Mon-Fri\n")
            return
        prospect = await state.get_prospect_by_email(args.email)
        if prospect is None:
            print(f"\n  No prospect with email {args.email}.\n")
            sys.exit(1)
        if args.sms_action == "consent":
            if not E164.match(args.phone or ""):
                print("\n  --phone must be E.164, like +18135550142.\n")
                sys.exit(1)
            if args.basis not in CONSENT_BASES - {"inbound_contact"}:
                print("\n  --basis must be opted_in (they agreed to texts) or booked (they booked with this number).\n")
                sys.exit(1)
            if len((args.note or "").strip()) < 10:
                print("\n  --note is required: where and when they agreed, in your words (kept as the evidence).\n")
                sys.exit(1)
            await state.set_prospect_phone(prospect.id, args.phone, "verified")
            consent = prospect.consent()
            consent["sms"] = {"basis": args.basis, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "note": args.note.strip(), "recorded_by": "owner"}
            await state.set_prospect_consent(prospect.id, consent)
            print(f"\n  Recorded: {prospect.full_name()} agreed to texts at {args.phone} ({args.basis}).\n")
            return
        # queue
        prospect = await state.get_prospect(prospect.id)
        number = prospect.phone
        item = {"body": args.text, "to_ref": number}
        channel = SmsChannel(config, env, state)
        subject, body = await channel.prepare(item, prospect)
        problems = []
        if not E164.match(number or ""):
            problems.append("no phone number on record (run: harvey sms consent)")
        if (prospect.consent().get("sms") or {}).get("basis") not in CONSENT_BASES:
            problems.append("no SMS consent on record (run: harvey sms consent)")
        gate = channel.content_gate(item, subject, body, prospect)
        if not gate:
            problems += gate.reasons
        if problems:
            print("\n  Not queued:\n  - " + "\n  - ".join(problems) + "\n")
            sys.exit(1)
        item_id = await state.add_outbox_item(
            prospect_id=prospect.id, kind="reply", channel="sms", to_email="", to_ref=number, subject="", body=args.text,
            send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), provider=sms_cfg.transport,
            status="pending_review" if sms_cfg.require_approval else "approved")
        print(f"\n  Queued ({'waiting for your approval in the Orion Approvals page' if sms_cfg.require_approval else 'approved'}):"
              f"\n  {body}\n  Item {item_id}\n")
    asyncio.run(go())


def cmd_vault(args):
    """Harvey's Obsidian vault: init (create + register with Obsidian) / sync / status."""
    import asyncio
    from pathlib import Path

    from harvey.config import load_config
    from harvey.state import StateManager
    from harvey.vault import Vault, register_with_obsidian

    config = load_config()
    path = args.path or config.vault.path
    if not path:
        print("\n  Set vault.path in harvey.local.yaml (or pass --path).\n")
        sys.exit(1)

    async def go():
        state = StateManager()
        await state.init_db()
        vault = Vault(state, config, Path(path))
        if args.vault_action == "status":
            existing = sorted(p.name for p in Path(path).glob("*") if not p.name.startswith("."))
            print(f"\n  Vault: {path}\n  Contents: {', '.join(existing) or 'empty'}\n")
            return
        result = await vault.sync()
        print(f"\n  Vault synced: {result}")
        if args.vault_action == "init":
            print(f"  Obsidian: {register_with_obsidian(Path(path))}")
            print("  If Obsidian is open, restart it (or Open another vault > Harvey) to see it.\n")
    asyncio.run(go())


def cmd_os(args):
    """Harvey's own sign-in to Orion, the business OS (login / status / sync / logout)."""
    import asyncio
    import getpass

    from harvey.config import load_config, load_env
    from harvey.integrations.os_auth import OsAuth, OsAuthError

    env = load_env()
    auth = OsAuth.from_env(env)
    action = args.os_action

    if action == "login":
        if not auth.is_configured():
            print("\n  Set ORION_SUPABASE_URL and ORION_SUPABASE_ANON_KEY (and ORION_EXPECTED_EMAIL) in .env first.\n")
            sys.exit(1)
        email = env.os_expected_email or input("  Harvey's Orion email: ").strip()
        password = getpass.getpass(f"  Password for {email} (not stored): ")
        try:
            auth.login_password(email, password)
        except OsAuthError as e:
            print(f"\n  ✗ {e}\n")
            sys.exit(1)
        print(f"\n  ✓ Signed in to Orion as {auth.email()}. Only the refresh token is kept: {auth.session_file}")
        print("  Restart Harvey to start reporting: systemctl --user restart harvey\n")
        return

    if action == "logout":
        print("\n  Signed out of Orion.\n" if auth.logout() else "\n  Not signed in.\n")
        return

    if action == "status":
        print(f"\n  Orion configured: {'yes' if auth.is_configured() else 'NO (ORION_SUPABASE_URL / ORION_SUPABASE_ANON_KEY)'}")
        print(f"  Signed in:     {auth.email() or ('yes' if auth.is_authorized() else 'no')}")
        if auth.is_authorized():
            from harvey.integrations.os_client import OsClient, OsError

            async def who():
                return await OsClient(auth).whoami()
            try:
                info = asyncio.run(who())
                print(f"  Identity:      {info.get('email')} ({info.get('role')}, slug {info.get('agent_slug')})")
            except OsError as e:
                print(f"  Identity:      ✗ {e}")
        print()
        return

    # sync: run one bridge cycle by hand
    from harvey.os_bridge import OsBridge
    from harvey.state import StateManager

    async def once():
        state = StateManager()
        await state.init_db()
        return await OsBridge(state, load_config(), env).cycle()
    print(json.dumps(asyncio.run(once()), indent=2, default=str))


def cmd_microsoft(args):
    """Microsoft 365 sign-in for mail, calendar and To Do (login / status / test / logout)."""
    from harvey.config import load_config, load_env
    from harvey.integrations.ms_auth import FEATURE_SCOPES, MicrosoftAuth, MicrosoftAuthError

    env = load_env()
    auth = MicrosoftAuth.from_env(env)
    action = args.microsoft_action

    if action == "login":
        if not auth.is_configured():
            print("\n  Set MS_CLIENT_ID (and MS_TENANT_ID) in .env first. The app registration"
                  "\n  steps are at the top of harvey/integrations/ms_auth.py.\n")
            sys.exit(1)
        try:
            hint = load_config().persona.email
        except Exception:
            hint = ""
        try:
            if args.device:
                auth.login_device_code()
            else:
                auth.login_interactive(login_hint=hint)
        except MicrosoftAuthError as e:
            print(f"\n  ✗ {e}\n")
            sys.exit(1)
        print(f"\n  ✓ Signed in as {auth.account() or 'the mailbox owner'}. Token stored at {auth.token_file}")
        missing = [f for f in FEATURE_SCOPES if not auth.has_feature(f)]
        if missing:
            print(f"  ! Not granted: {', '.join(missing)}. Add those permissions to the app and sign in again.")
        print("  Next: harvey microsoft test\n")
        return

    if action == "logout":
        print("\n  Signed out; token file removed.\n" if auth.logout() else "\n  Not signed in.\n")
        return

    if action == "status":
        print(f"\n  Client ID:  {'set' if auth.is_configured() else 'MISSING (MS_CLIENT_ID)'}")
        print(f"  Tenant:     {auth.tenant_id}")
        print(f"  Signed in:  {auth.account() or ('yes' if auth.is_authorized() else 'no')}")
        if auth.is_authorized():
            for feature in FEATURE_SCOPES:
                print(f"  {feature:<10}  {'granted' if auth.has_feature(feature) else 'NOT granted'}")
        print()
        return

    # test: an end-to-end read against each granted service
    from harvey.integrations.calendar import CalendarClient
    from harvey.integrations.ms_graph import GraphClient
    from harvey.integrations.ms_mail import MsGraphMailProvider
    from harvey.integrations.ms_todo import TodoClient

    async def _test():
        config = load_config()
        graph = GraphClient(auth)
        ok_all = True

        def show(name, ok, detail):
            nonlocal ok_all
            ok_all = ok_all and ok
            print(f"  {'✓' if ok else '✗'} {name:<9} {detail}")

        print()
        ok, detail = await MsGraphMailProvider(config, env, graph=graph).test_connection()
        show("mail", ok, detail)

        if auth.is_authorized() and auth.has_feature("calendar"):
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            events = await CalendarClient(graph, tz=config.usage.quiet_hours.timezone).list_events(
                now, now + timedelta(days=7))
            show("calendar", True, f"{len(events)} event(s) in the next 7 days")
        else:
            show("calendar", False, "Calendars.ReadWrite not granted" if auth.is_authorized() else "not signed in")

        if auth.is_authorized() and auth.has_feature("todo"):
            todo = TodoClient(graph, config.integrations.microsoft.todo_list)
            try:
                await todo.ensure_list()
                show("to do", True, f"list '{todo.list_name}' ready")
            except Exception as e:  # noqa: BLE001
                show("to do", False, str(e)[:200])
        else:
            show("to do", False, "Tasks.ReadWrite not granted" if auth.is_authorized() else "not signed in")
        print()
        sys.exit(0 if ok_all else 1)
    asyncio.run(_test())


def cmd_linkedin(args):
    """LinkedIn session and safety controls (login / status / resume / reenable)."""
    from harvey.channels.linkedin import (
        DISABLED, INBOX_CHECKED_AT, TRIPPED_UNTIL, TRIPS, WARMUP_START)
    from harvey.config import load_config
    from harvey.integrations import linkedin_policy as policy
    from harvey.integrations.linkedin import COOKIES_PATH
    from harvey.state import StateManager

    if args.linkedin_action == "login":
        from harvey.integrations.linkedin import interactive_login

        async def _login():
            if not await interactive_login():
                print("\n  \u2717 Did not reach your LinkedIn feed in time. Nothing saved.\n")
                sys.exit(1)
            state = StateManager()
            await state.init_db()
            await state.set_setting("linkedin_paused", "")
            print(f"\n  \u2713 Session saved to {COOKIES_PATH} (owner-only).\n")
        asyncio.run(_login())
        return

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.linkedin_action == "resume":
            await state.set_setting("linkedin_paused", "")
            await state.set_setting(TRIPPED_UNTIL, "")
            print("\n  LinkedIn resumed. (A disabled channel stays off: use `reenable`.)\n")
            return
        if args.linkedin_action == "reenable":
            await state.set_setting(DISABLED, "")
            await state.set_setting(TRIPS, "[]")
            print("\n  LinkedIn re-enabled and trip history cleared. Be sure you know what "
                  "triggered the last trips before leaving it on.\n")
            return

        li = load_config().channels.linkedin
        paused = await state.get_setting("linkedin_paused")
        disabled = await state.get_setting(DISABLED)
        until = await state.get_setting(TRIPPED_UNTIL)
        sent_c = await state.count_kind_since("linkedin", ("connect",), today_only=True)
        week_c = await state.count_kind_since("linkedin", ("connect",), days=7)
        sent_m = await state.count_kind_since("linkedin", ("message", "reply"), today_only=True)
        lim = policy.Limits(li.max_daily_connections, li.max_daily_messages,
                            li.max_weekly_connections).clamped()
        print(f"\n  LinkedIn: mode={li.mode}, enabled={li.enabled}, "
              f"session {'saved' if COOKIES_PATH.exists() else 'MISSING (run `harvey linkedin login`)'}")
        print(f"  State:    {'DISABLED - ' + disabled if disabled else ('PAUSED - ' + paused if paused else 'active')}"
              + (f"; cooling off until {until}Z" if until else ""))
        print(f"  Today:    {sent_c}/{lim.connects_per_day} connects, {sent_m}/{lim.messages_per_day} messages; "
              f"{week_c}/{lim.connects_per_week} connects this week")
        print(f"  Warm-up:  started {li.warmup_start_date or await state.get_setting(WARMUP_START) or 'on first send'}")
        inbox = await state.get_setting(INBOX_CHECKED_AT)
        print(f"  Inbox:    last read {inbox + 'Z' if inbox else 'never'}")
        for st in ("staged", "requested", "connected", "message_staged", "messaged", "replied", "declined"):
            n = len(await state.list_linkedin_threads(st, limit=10000))
            if n:
                print(f"            {st}: {n}")
        print()

    asyncio.run(_run())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from harvey.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.\n")
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            await state.update_outbox_item(args.reject, status="rejected")
            print("\n  Rejected.\n")
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: harvey sending resume")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, "
              f"{len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: harvey outbox --approve <id>   |   all: harvey outbox --approve-all")
            print("  Reject:  harvey outbox --reject <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


def cmd_profile(args):
    """Read what companies' own websites say about them. Free, no Claude calls."""
    from harvey.state import StateManager
    from harvey.pipeline import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_companies_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return

        print(f"\n  {pending} companies need profiling. Reading up to "
              f"{args.limit}...\n")
        companies, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days)
        print(f"  Read {companies} sites → {observations} observations (free)")
        print("\n  See what turned up: harvey dashboard → Signals\n")

    asyncio.run(_profile())


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from harvey.state import StateManager
    from harvey.config import load_config
    from harvey.collectors.discover import (
        PROVIDERS, build_queries, provider_menu, run_discovery,
    )

    async def _discover():
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu():
                mark = "ready" if m["configured"] else "needs setup"
                print(f"\n  {m['label']}  [{m['key']}]  ({mark})")
                print(f"    {m['blurb']}")
                print(f"    Cost: {m['cost_note']}")
                print(f"    Free: {m['free_tier']}")
                if m["env_keys"]:
                    print(f"    Needs: {', '.join(m['env_keys'])} in .env  —  {m['signup_url']}")
                if m["caveat"]:
                    print(f"    Note: {m['caveat']}")
            print("\n  Run one with: harvey discover --provider <key> --estimate\n")
            return

        if args.provider not in PROVIDERS:
            print(f"\n  Unknown provider {args.provider!r}. "
                  f"See: harvey discover --providers\n")
            return

        config = load_config()
        state = StateManager()
        await state.init_db()

        # Semicolons, not commas: "Denver, CO" is one city, not two.
        cities = [c.strip() for c in args.city.split(";") if c.strip()] or None
        if args.cohort:
            from harvey.collectors.discover import build_job_queries
            queries = build_job_queries(
                [c for c in args.cohort.split(",") if c.strip()],
                limit=args.limit if args.limit != 100 else 25,
                location=cities[0] if cities else "United States")
        else:
            queries = build_queries(config, cities=cities,
                                    depth=args.depth, limit=args.limit)

        report = await run_discovery(state, config, args.provider, queries,
                                     max_spend=args.max_spend, dry_run=True)
        print(f"\n  {len(queries)} queries via {PROVIDERS[args.provider].label}")
        for q in queries[:8]:
            print(f"    - {q.keyword()}" + ("" if q.coordinate else
                  "   (no coordinates — add icp.geo_coordinates for radius search)"))
        if len(queries) > 8:
            print(f"    ... and {len(queries) - 8} more")
        print(f"\n  Estimated cost: ${report.estimated_cost:.4f}"
              f"   (cap: ${args.max_spend:.2f})")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        print("\n  Running...\n")
        from harvey.pipeline import run_prospecting

        result = await run_prospecting(
            state, config, args.provider, queries,
            max_spend=args.max_spend, profile=not args.no_profile,
        )
        r = result.discover or {}
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{r.get('queries', 0)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")

        if args.no_profile:
            print("\n  PROFILE — skipped (--no-profile)")
        else:
            print(f"\n  PROFILE — {result.profiled_companies} sites read, "
                  f"{result.profile_observations} observations  (free)")

        for err in result.errors[:5]:
            print(f"    ! {err}")
        print("\n  Next: harvey dashboard → Signals → cohort builder\n")

    asyncio.run(_discover())


def cmd_signals(args):
    """Review the signal vocabulary — Harvey proposes, you confirm.

    Nothing is collected until a signal is confirmed, so a fresh install
    prospects against nothing until someone makes these decisions.
    """
    from harvey.state import StateManager
    from harvey.signals import seed_signal_catalog

    async def _signals():
        state = StateManager()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",")
                 if a.strip()]
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in await state.get_signal_codes()]
            elif codes == ["FREE"]:
                # Only signals whose cost note STARTS with free/included. A
                # substring match would sweep in "1 credit (free tiers
                # available)", which is not free.
                codes = [c["code"] for c in await state.get_signal_codes()
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            changed = sum(
                [1 for c in codes if await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            seen = counts.get(r["code"], {}).get("companies", 0)
            seen_txt = f"  ({seen} companies)" if seen else ""
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<22} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: harvey signals --confirm CODE[,CODE...]")
            print("  Everything free: harvey signals --confirm free")
            print("  Details and descriptions: harvey dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
            print("\n  Sending resumed (bounce counter reset).\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())


def main():
    from harvey.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="harvey",
        description="Harvey — Autonomous AI Sales Agent. Always Be Closing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # harvey install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # harvey setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # harvey run
    sub = subparsers.add_parser("run", help="Start Harvey's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # harvey train <url>
    sub = subparsers.add_parser("train", help="Train Harvey on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.add_argument(
        "--provider", choices=["claude", "codex"], default=None,
        help="Subscription to think with (default: the one in your config, else claude)",
    )
    sub.add_argument("--model", default=None, help="Model for this provider (default: provider default)")
    sub.add_argument(
        "--reasoning-effort", default=None,
        help="Codex reasoning effort (minimal/low/medium/high/xhigh)",
    )
    sub.set_defaults(func=cmd_train)

    # harvey dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.set_defaults(func=cmd_dashboard)

    # harvey status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.add_argument(
        "--check", action="store_true",
        help="Also check the configured brain now (Codex: CLI version, isolation "
             "and login; no model call)",
    )
    sub.set_defaults(func=cmd_status)

    # harvey export
    sub = subparsers.add_parser(
        "export", help="Export prospects as a sequencer-ready CSV"
    )
    sub.add_argument("--out", default="prospects.csv", help="Output file (default: prospects.csv)")
    sub.add_argument(
        "--email-status", default="",
        help="Comma-separated statuses to include (default: verified,risky)",
    )
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses (e.g. new,queued)")
    sub.add_argument("--all", action="store_true", help="Export everything, no filters")
    sub.set_defaults(func=cmd_export)

    # harvey gmail auth|test
    sub = subparsers.add_parser("linkedin", help="LinkedIn session and safety controls")
    sub.add_argument("linkedin_action", choices=["login", "status", "resume", "reenable"],
                     help="login: sign in once in a visible browser; status; "
                          "resume: lift a pause/cool-off; reenable: after repeated trips")
    sub.set_defaults(func=cmd_linkedin)

    sub = subparsers.add_parser("microsoft", help="Microsoft 365 sign-in: mail, calendar, To Do")
    sub.add_argument("microsoft_action", choices=["login", "status", "test", "logout"],
                     help="login: one-time browser sign-in; status; test: read each service; logout")
    sub.add_argument("--device", action="store_true",
                     help="login with a device code (for a machine with no browser)")
    sub.set_defaults(func=cmd_microsoft)

    sub = subparsers.add_parser("sms", help="Consent-gated texting (from Clara's number through Orion)")
    sub.add_argument("sms_action", choices=["status", "consent", "queue"])
    sub.add_argument("email", nargs="?", default="", help="the prospect's email")
    sub.add_argument("text", nargs="?", default="", help="queue: the text to send")
    sub.add_argument("--phone", default="", help="consent: their number, E.164")
    sub.add_argument("--basis", default="opted_in", help="consent: opted_in | booked")
    sub.add_argument("--note", default="", help="consent: where and when they agreed (evidence)")
    sub.set_defaults(func=cmd_sms)

    sub = subparsers.add_parser("vault", help="Harvey's Obsidian vault")
    sub.add_argument("vault_action", choices=["init", "sync", "status"],
                     help="init: create + register with Obsidian; sync: refresh now; status")
    sub.add_argument("--path", default="", help="vault folder (default: vault.path in the config)")
    sub.set_defaults(func=cmd_vault)

    sub = subparsers.add_parser("os", help="Harvey's sign-in to Orion, the business OS")
    sub.add_argument("os_action", choices=["login", "status", "sync", "logout"],
                     help="login: one-time password sign-in; status; sync: one report cycle now; logout")
    sub.set_defaults(func=cmd_os)

    sub = subparsers.add_parser("gmail", help="Gmail provider setup")
    sub.add_argument("gmail_action", choices=["auth", "test"],
                     help="auth: one-time OAuth; test: verify connection")
    sub.set_defaults(func=cmd_gmail)

    # harvey outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.set_defaults(func=cmd_outbox)

    # harvey sending pause|resume|status
    sub = subparsers.add_parser("discover", help="Find businesses matching your ICP")
    sub.add_argument("--provider", default="osm",
                     help="Discovery provider (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=30,
                     help="SERP depth (default: 30 — depth 100 costs 10x since Sept 2025)")
    sub.add_argument("--limit", type=int, default=100,
                     help="Max records per query (default: 100)")
    sub.add_argument("--max-spend", type=float, default=1.0,
                     help="Hard cap in dollars (default: 1.00)")
    sub.add_argument("--cohort", default="",
                     help="Job-board discovery for ICP cohort(s), e.g. A or A,B "
                          "(use with --provider jobboards)")
    sub.add_argument("--no-profile", action="store_true",
                     help="Don't profile what was found (profiling is free)")
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max sites to read (default: 200)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "signals", help="Review/confirm which signals Harvey prospects against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    # harvey usage
    sub = subparsers.add_parser("usage", help="Show brain usage and quota (Claude or Codex)")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=cmd_usage)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    harvey install   — Install dependencies")
        print("    harvey setup     — Configure Harvey (first time)")
        print("    harvey run       — Start closing deals")
        print("    harvey dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from harvey.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'harvey {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
