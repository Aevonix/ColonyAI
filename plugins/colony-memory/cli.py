"""CLI commands for the Colony memory provider.

Exposes:
  hermes colony status     → Health + capabilities
  hermes colony goals      → List active goals
  hermes colony context    → Fetch current context assembly
  hermes colony sync       → Force a turn sync
"""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx
import typer

app = typer.Typer(help="Colony cognitive infrastructure commands")


def _sidecar_url() -> str:
    return os.environ.get("COLONY_URL", "http://127.0.0.1:7777")


def _api_key() -> str:
    return os.environ.get("COLONY_API_KEY", "")


def _headers() -> dict[str, str]:
    h = {}
    key = _api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _contact_id() -> str:
    return os.environ.get("COLONY_MCP_CONTACT_ID", "default")


@app.command()
def status(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Colony sidecar URL"),
) -> None:
    """Check Colony sidecar health and capabilities."""
    sidecar = url or _sidecar_url()
    try:
        resp = httpx.get(f"{sidecar}/v1/host/health", headers=_headers(), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        typer.echo(json.dumps(data, indent=2))
    except httpx.HTTPError as exc:
        typer.echo(f"Colony sidecar unreachable: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def goals(
    status_filter: str = typer.Option("active", "--status", "-s", help="Filter: active|completed|blocked|all"),
    url: Optional[str] = typer.Option(None, "--url", "-u"),
) -> None:
    """List Colony goals."""
    sidecar = url or _sidecar_url()
    try:
        resp = httpx.get(
            f"{sidecar}/v1/host/goals",
            headers=_headers(),
            params={"status_filter": status_filter},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        typer.echo(json.dumps(data, indent=2))
    except httpx.HTTPError as exc:
        typer.echo(f"Failed to fetch goals: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def context(
    query: str = typer.Option("", "--query", "-q", help="Incoming message for context assembly"),
    contact_id: Optional[str] = typer.Option(None, "--contact", "-c"),
    url: Optional[str] = typer.Option(None, "--url", "-u"),
) -> None:
    """Fetch Colony cognitive context for a contact."""
    sidecar = url or _sidecar_url()
    cid = contact_id or _contact_id()
    try:
        resp = httpx.post(
            f"{sidecar}/v1/host/context/assemble",
            headers=_headers(),
            json={
                "identity": {"host_id": "hermes"},
                "context": {
                    "session_id": "cli-manual",
                    "contact_id": cid,
                },
                "incoming_message": {"role": "user", "content": query or "context check"},
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        typer.echo(json.dumps(data, indent=2))
    except httpx.HTTPError as exc:
        typer.echo(f"Failed to fetch context: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def sync(
    user: str = typer.Option("", "--user", "-u", help="User message content"),
    assistant: str = typer.Option("", "--assistant", "-a", help="Assistant message content"),
    contact_id: Optional[str] = typer.Option(None, "--contact", "-c"),
    url: Optional[str] = typer.Option(None, "--url"),
) -> None:
    """Force a turn sync to Colony."""
    sidecar = url or _sidecar_url()
    cid = contact_id or _contact_id()
    if not user or not assistant:
        typer.echo("--user and --assistant are required", err=True)
        raise typer.Exit(code=1)
    try:
        resp = httpx.post(
            f"{sidecar}/v1/host/turns/sync",
            headers=_headers(),
            json={
                "identity": {"host_id": "hermes"},
                "context": {
                    "session_id": "cli-manual",
                    "contact_id": cid,
                },
                "user_message": {"role": "user", "content": user},
                "assistant_message": {"role": "assistant", "content": assistant},
            },
            timeout=8,
        )
        resp.raise_for_status()
        typer.echo(json.dumps(resp.json(), indent=2))
    except httpx.HTTPError as exc:
        typer.echo(f"Turn sync failed: {exc}", err=True)
        raise typer.Exit(code=1)
