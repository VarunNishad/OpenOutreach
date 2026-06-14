# openoutreach/contacts/service.py
"""Daemon-facing orchestration over the contacts-service HTTP client.

Two jobs, both **best-effort** — a contacts-service outage or an inaccessible
profile degrades to a no-op, never breaking outreach:

- ``resolve_lead`` — the free first rung of the resolution waterfall, called
  before the paid finder spends a BetterContact credit.
- ``contribute_lead`` — the give-back at the two moments a real contact comes
  into existence (a paid finder hit, a scraped LinkedIn connection). The first
  contribution mints the per-operator token via the folded ``/register``.

The token lives only in ``SiteConfig`` (the instance's own DB), never in the
repo. Contribution is client-side geo-pre-filtered as a bandwidth optimization;
the service re-gates server-side — it is the only trusted boundary.
"""
from __future__ import annotations

import logging

from openoutreach.contacts import client
from openoutreach.contacts.client import ContactsUnavailable
from openoutreach.core.models import SiteConfig
from openoutreach.linkedin.setup.geo import is_eea_located

logger = logging.getLogger(__name__)

# Default home of the central contacts service; overridable per instance via
# SiteConfig.contacts_api_url (e.g. to self-host the store).
DEFAULT_CONTACTS_API_URL = "https://hub.openoutreach.app"


# ── Resolve — the free first rung ────────────────────────────────────


def resolve_lead(lead) -> str | None:
    """Free read-back: a stored work email for *lead*, or ``None``.

    ``None`` covers every fall-through-to-paid-finder case identically: a miss,
    an exhausted give-to-get balance, no token yet (the first contribution mints
    it), or a service outage.
    """
    config = SiteConfig.load()
    if not config.contacts_api_token:
        return None
    try:
        email = client.resolve(_base_url(config), config.contacts_api_token, lead.public_identifier)
    except ContactsUnavailable as exc:
        logger.info("contacts: resolve unavailable for %s: %s", lead.public_identifier, exc)
        return None
    if email:
        logger.info("contacts: resolved %s for %s (saved a paid lookup)", email, lead.public_identifier)
    return email


# ── Contribute — the two give-back moments ───────────────────────────


def contribute_lead(session, lead, email: str) -> None:
    """Give one resolved contact back to the central store — best-effort.

    No-ops when the operator opted out, the email is empty, the profile can't be
    read, or the lead's location is EEA/UK/CH / unknown (client-side bandwidth
    pre-filter; the service re-gates). Mints + stores the token on the first
    contribution.
    """
    if not session.linkedin_profile.contribute_leads or not email:
        return

    country_code, embedding = _lead_signals(session, lead)
    if is_eea_located(country_code):
        logger.debug(
            "contacts: skip give-back for %s (EEA/UK/CH or unknown location: %s)",
            lead.public_identifier, country_code,
        )
        return

    record = {
        "public_identifier": lead.public_identifier,
        "country_code": country_code,
        "email": email,
        "embedding": embedding,
    }
    _send(session, record)


def _send(session, record: dict) -> None:
    """Dispatch a record: ``/contribute`` on an existing token, else the folded
    ``/register`` that mints one. Swallows an outage (best-effort)."""
    config = SiteConfig.load()
    public_id = record["public_identifier"]
    try:
        if config.contacts_api_token:
            client.contribute(_base_url(config), config.contacts_api_token, record)
        elif not _register(config, session, record):
            return
    except ContactsUnavailable as exc:
        logger.info("contacts: give-back unavailable for %s: %s", public_id, exc)
        return
    logger.info("contacts: contributed %s (%s) to the central store", public_id, record["country_code"])


def _register(config: SiteConfig, session, record: dict) -> bool:
    """Mint-or-reuse the operator's token via the folded ``/register`` and
    persist it. Returns whether a token was obtained. The operator is keyed by
    their own LinkedIn account; ``email`` is the provenance / revocation handle.
    """
    profile = session.self_profile
    subscriber_email = session.django_user.email or session.linkedin_profile.linkedin_username
    token = client.register(
        _base_url(config),
        linkedin_public_id=profile.get("public_identifier"),
        subscriber_email=subscriber_email,
        record=record,
    )
    if not token:
        return False
    config.contacts_api_token = token
    config.save(update_fields=["contacts_api_token"])
    logger.info("contacts: registered — API token earned and stored")
    return True


# ── Helpers ──────────────────────────────────────────────────────────


def _base_url(config: SiteConfig) -> str:
    return config.contacts_api_url or DEFAULT_CONTACTS_API_URL


def _lead_signals(session, lead) -> tuple[str | None, list[float] | None]:
    """The two profile-derived fields a contribution needs: the lead's
    ``country_code`` (for the geo-gate) and the 384-dim embedding, computed
    locally so raw profile text never leaves the instance.

    Returns ``(None, None)`` if the profile is inaccessible — which the geo-gate
    treats as unknown → drop, the safe default. ``AuthenticationError`` still
    propagates (the daemon's reauth handler owns a dead session).
    """
    from linkedin_cli.exceptions import ProfileInaccessibleError

    try:
        profile = lead.get_profile(session)
    except ProfileInaccessibleError:
        return None, None
    if not profile:
        return None, None
    if lead.embedding is None:
        lead.embed_from_profile(profile)
    emb = lead.embedding_array
    embedding = emb.tolist() if emb is not None else None
    return profile.get("country_code"), embedding
