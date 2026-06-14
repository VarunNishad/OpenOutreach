# openoutreach/emails/nudge.py
"""Per-launch email-setup nudge.

Runs every `rundaemon` start after onboarding. Until both a finder key and a
working mailbox exist, it prompts (on a TTY) or logs (headless) the next setup
step — copy drawn from the GLF angle in marketing/email-sequence.md, filled with
the user's own pipeline numbers. Never blocks: email is a deferrable upgrade.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from openoutreach.core.conf import DEFAULT_CONNECT_DAILY_LIMIT, DEFAULT_EMAIL_DAILY_LIMIT
from openoutreach.core.models import SiteConfig
from openoutreach.core.onboarding_wizard import _BACK, IntText, MultilineText, Password
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.emails.icemail import parse_mailboxes
from openoutreach.emails.models import Mailbox
from openoutreach.emails.smtp import verify_auth
from openoutreach.linkedin.models import LinkedInProfile

logger = logging.getLogger(__name__)

FINDER_AFFILIATE_URL = "https://bettercontact.rocks?fpr=openoutreach"
SENDER_AFFILIATE_URL = "https://icemail.ai?via=openoutreach"

NO_FINDER = "no_finder"
NO_MAILBOX = "no_mailbox"
CONFIGURED = "configured"


# ── Setup state ──────────────────────────────────────────────────

def email_state() -> str:
    """Which setup step is next: NO_FINDER, NO_MAILBOX, or CONFIGURED."""
    if not SiteConfig.load().finder_api_key:
        return NO_FINDER
    if not Mailbox.objects.exists():
        return NO_MAILBOX
    return CONFIGURED


# ── Nudge copy ───────────────────────────────────────────────────

# GAIN — the discovery engine already worked; email is the reach you're missing.
NO_FINDER_NUDGE = """
📧  LinkedIn finds the right people; email is how you reach them.
    Your model qualified {qualified} leads, but LinkedIn sends only ~{connect_cap}/day
    and most never accept. Email reaches the whole list — automatically, as they qualify.
    Turn on email finding (a paid finder; the affiliate fee keeps OpenOutreach free):
      {finder_url}
"""

# URGENCY — the ~2-week warmup clock (always true); a loss-aversion line only
# when the pipeline numbers are real (they're zero right after the finder is set).
NO_MAILBOX_NUDGE = """
📧  Set up email sending. IceMail mailboxes need a ~2-week warmup, and the clock
    only starts once you add them — so the sooner they're warming, the sooner you
    reach the leads who never accept a LinkedIn connection.
{waiting_line}    Add your sending mailboxes (IceMail — paid; warmup is hands-off):
      {sender_url}
"""


def _hyperlink(url: str) -> str:
    """Wrap *url* in an OSC 8 terminal hyperlink so the whole address is clickable.

    Terminals' bare-URL detection often stops at the ``?``, leaving affiliate
    query params (``?fpr=...``) unclickable. OSC 8 marks the entire URL as one
    link explicitly. The visible text stays the URL itself, so terminals without
    OSC 8 support still show a copyable address.
    """
    esc = "\033"
    return f"{esc}]8;;{url}{esc}\\{url}{esc}]8;;{esc}\\"


def render(state: str, stats: dict, *, hyperlink: bool = False) -> str:
    """The nudge copy for *state*, filled with the user's pipeline numbers.

    ``hyperlink=True`` wraps the affiliate URLs in OSC 8 escapes for an
    interactive TTY; leave it False for headless logging (no escape codes).
    """
    template = NO_FINDER_NUDGE if state == NO_FINDER else NO_MAILBOX_NUDGE
    wrap = _hyperlink if hyperlink else (lambda u: u)
    return template.format(
        finder_url=wrap(FINDER_AFFILIATE_URL),
        sender_url=wrap(SENDER_AFFILIATE_URL),
        waiting_line=_waiting_line(stats),
        **stats,
    )


def _waiting_line(stats: dict) -> str:
    """The mailbox nudge's loss-aversion line — shown only when its number is real.

    Right after the finder is enabled nothing has resolved yet, so both counts are
    zero and the line is omitted; the warmup urgency carries the message. Returns a
    full indented line ending in a newline, or '' to collapse it out of the copy.
    """
    if stats.get("resolved_emails"):
        return f"    {stats['resolved_emails']} leads already have an email resolved, waiting to be reached.\n"
    if stats.get("pending"):
        return f"    {stats['pending']} leads sit behind connection requests that may never be accepted.\n"
    return ""


def pipeline_stats() -> dict:
    """The user's own numbers — what makes the nudge land instead of nag."""
    profile = LinkedInProfile.objects.filter(active=True).first()
    return {
        "qualified": Deal.objects.filter(state=DealState.QUALIFIED).count(),
        "pending": Deal.objects.filter(state=DealState.PENDING).count(),
        "resolved_emails": Lead.objects.filter(api_email__isnull=False).count(),
        "connect_cap": profile.connect_daily_limit if profile else DEFAULT_CONNECT_DAILY_LIMIT,
    }


# ── Mailbox import (parse → store → auth-check; no console I/O) ───

@dataclass
class ImportReport:
    parsed: int = 0
    stored: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (email, reason)


def import_mailboxes(pasted: str, daily_limit: int = DEFAULT_EMAIL_DAILY_LIMIT) -> ImportReport:
    """Auth-check every mailbox in a pasted export block, storing only the ones
    whose credentials log in. A row exists iff it authenticated — there is no
    inactive state to carry. ``daily_limit`` is the warm-safe sends/day applied
    to each imported box (set once at onboarding, like the LinkedIn limit).

    Raises ValueError if the paste has no recognizable header row.
    """
    report = ImportReport()
    for email, password in parse_mailboxes(pasted):
        report.parsed += 1
        box = Mailbox(username=email, password=password, from_address=email)
        ok, reason = verify_auth(box.host, box.port, box.username, box.password)
        if not ok:
            report.failures.append((email, reason))
            continue
        Mailbox.objects.update_or_create(
            username=email,
            defaults={"password": password, "from_address": email, "daily_limit": daily_limit},
        )
        report.stored += 1
    return report


# ── Interactive prompt ───────────────────────────────────────────

def prompt_email_setup() -> None:
    """Drive the email-setup steps: finder key, then IceMail mailboxes.

    On a TTY, walks every remaining step in one session (set the finder and you
    are asked to paste mailboxes right after, not next launch). Headless, it can
    only log the next pending step. Never `sys.exit`s, so it can't block the
    LinkedIn discovery leg.
    """
    if sys.stdin.isatty():
        _walk_setup_steps()
    else:
        _log_pending_step()


def _log_pending_step() -> None:
    """Headless fallback: log the next pending step (no TTY to collect on)."""
    state = email_state()
    if state != CONFIGURED:
        logger.info(render(state, pipeline_stats()))


def _walk_setup_steps() -> None:
    """Prompt each remaining step in turn, stopping at the first one skipped.

    A skipped step (empty input / Ctrl+D) leaves the setup state unchanged, which
    ends the walk; the rest are re-asked next launch — no opt-out, by design.
    """
    while True:
        state = email_state()
        if state == CONFIGURED:
            return
        if not _prompt_step(state):
            return  # skipped — leave the remaining steps for next launch


def _prompt_step(state: str) -> bool:
    """Show one setup step and collect it. True if it advanced the setup state.

    Collectors handle their own failure modes (bad paste, auth reject) gracefully
    and simply leave the state unadvanced, so this never raises on user error.
    """
    print(render(state, pipeline_stats(), hyperlink=True))
    _COLLECT_BY_STATE[state]()
    return email_state() != state


def _collect_finder_key() -> None:
    key = Password("finder_api_key", "Finder API key (Enter to skip):", required=False).ask("")
    if not key or key == _BACK:
        return
    cfg = SiteConfig.load()
    cfg.finder_api_key = key
    cfg.save()
    logger.info("Finder key saved — enrichment is on; emails resolve as leads qualify.")


def _collect_mailboxes() -> None:
    pasted = _ask_for_paste()
    if pasted is None:
        return
    daily_limit = _ask_for_daily_limit()
    try:
        report = import_mailboxes(pasted, daily_limit=daily_limit)
    except ValueError as exc:
        print(f"  {exc}")
        return
    _print_report(report)


def _ask_for_daily_limit() -> int:
    """Per-mailbox warm-safe sends/day; Enter accepts the conservative default."""
    answer = IntText(
        "email_daily_limit",
        "Emails per mailbox per day (Enter for default):",
        default=DEFAULT_EMAIL_DAILY_LIMIT,
        required=False,
    ).ask(DEFAULT_EMAIL_DAILY_LIMIT)
    if not isinstance(answer, int) or answer <= 0:
        return DEFAULT_EMAIL_DAILY_LIMIT
    return answer


def _ask_for_paste() -> str | None:
    """Prompt for the pasted export; None if the user skips."""
    answer = MultilineText(
        "mailboxes",
        "Paste the IceMail Export Mailboxes sheet (with its header row)",
        required=False,
    ).ask("")
    return None if not answer or answer == _BACK else answer


def _print_report(report: ImportReport) -> None:
    for email, reason in report.failures:
        print(f"  ✗ {email}: {reason}")
    if not report.parsed:
        print("  No mailboxes found — include the header row (Email, Password, …).")
        return
    print(f"  Parsed {report.parsed} mailbox(es); {report.stored} authenticated and saved.")


_COLLECT_BY_STATE = {NO_FINDER: _collect_finder_key, NO_MAILBOX: _collect_mailboxes}
