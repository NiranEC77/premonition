"""publish/supabase_rest.py — thin PostgREST client for publishing to Supabase.

Everything in publish/ runs on the agents laptop and writes with the
service_role key, per CLAUDE.md: "The service_role key never enters the
Vercel environment, the repo, or the browser — writes come exclusively from
the agents laptop." No supabase-py dependency — PostgREST is plain REST, and
the surface area we need (upsert, select) is small enough that a dependency
buys nothing.
"""

from __future__ import annotations

import os
from typing import Any

import requests

HTTP_TIMEOUT_SECS = 20


class SupabaseConfigError(RuntimeError):
    pass


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SupabaseConfigError(f"{name} is not set")
    return val


def _base_url() -> str:
    url = _env("SUPABASE_URL").rstrip("/")
    if url in ("https://supabase.com", "https://supabase.com/"):
        raise SupabaseConfigError(
            "SUPABASE_URL is set to the Supabase marketing site, not a project URL "
            "(expected https://<ref>.supabase.co). Fix /etc/premonition/env."
        )
    return url


def _service_role_headers() -> dict:
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def upsert(table: str, rows: list[dict], on_conflict: str) -> requests.Response:
    """Upsert rows into `table`, keyed on the comma-separated `on_conflict` columns.
    Uses the service_role key — never call this from anything that runs on Vercel."""
    if not rows:
        return None  # nothing to do; do not send an empty PostgREST request
    url = f"{_base_url()}/rest/v1/{table}"
    headers = _service_role_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    resp = requests.post(
        url,
        params={"on_conflict": on_conflict},
        json=rows,
        headers=headers,
        timeout=HTTP_TIMEOUT_SECS,
    )
    resp.raise_for_status()
    return resp


def select(table: str, params: dict[str, Any] | None = None, use_anon: bool = False) -> requests.Response:
    """Read rows. use_anon=True exercises the SAME path the browser dashboard
    uses (anon key, RLS-governed) — for verifying read access independent of
    service_role."""
    url = f"{_base_url()}/rest/v1/{table}"
    if use_anon:
        key = _env("SUPABASE_ANON_KEY")
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    else:
        headers = _service_role_headers()
    resp = requests.get(url, params=params or {"select": "*"}, headers=headers, timeout=HTTP_TIMEOUT_SECS)
    return resp
