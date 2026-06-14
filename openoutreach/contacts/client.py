# openoutreach/contacts/client.py
"""HTTP client for the central contacts service (the outbound register /
resolve / contribute calls).

Thin transport only — no business logic, no config reads. Mirrors the
``emails/bettercontact.py`` house style: a ``requests.Session`` per call,
a fixed timeout, and a single ``ContactsUnavailable`` for any transport
failure (so callers can distinguish "service down" from "resolve miss").
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 30


class ContactsUnavailable(Exception):
    """The contacts service could not be reached (network drop, HTTP error,
    bad status). Distinct from a resolve *miss* — the service answering 404
    with no email is a normal miss, returned as ``None``, not an error."""


def _session(token: str | None = None) -> requests.Session:
    session = requests.Session()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def _endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/api/{path}/"


def resolve(base_url: str, token: str, public_identifier: str) -> str | None:
    """GET /api/resolve/ — return the stored email on a hit, ``None`` on a miss.

    A 404 (no row, or balance exhausted) is a miss → ``None``; any other
    non-200 or a transport failure raises ``ContactsUnavailable``.
    """
    url = _endpoint(base_url, "resolve")
    try:
        with _session(token) as session:
            resp = session.get(url, params={"id": public_identifier}, timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise ContactsUnavailable(f"contacts service unreachable: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code == 200:
        return resp.json().get("email") or None
    raise ContactsUnavailable(f"resolve returned HTTP {resp.status_code}")


def contribute(base_url: str, token: str, record: dict) -> dict:
    """POST /api/contribute/ — give back one record on an existing token.

    Returns the service's counter dict
    (``{accepted, dropped_geo, dropped_suppressed, dropped_invalid}``).
    """
    url = _endpoint(base_url, "contribute")
    try:
        with _session(token) as session:
            resp = session.post(url, json=record, timeout=_HTTP_TIMEOUT_S)
            resp.raise_for_status()
    except requests.RequestException as exc:
        raise ContactsUnavailable(f"contribute failed: {exc}") from exc
    return resp.json()


def register(base_url: str, linkedin_public_id: str, subscriber_email: str, record: dict) -> str | None:
    """POST /api/register/ — mint-or-reuse the operator's token, folding in the
    first contribution record. Returns the token key (``None`` if absent)."""
    url = _endpoint(base_url, "register")
    body = {
        "linkedin_public_id": linkedin_public_id,
        "subscriber_email": subscriber_email,
        **record,
    }
    try:
        with _session() as session:
            resp = session.post(url, json=body, timeout=_HTTP_TIMEOUT_S)
            resp.raise_for_status()
    except requests.RequestException as exc:
        raise ContactsUnavailable(f"register failed: {exc}") from exc
    return resp.json().get("token") or None
