"""The built-in catalog of known official remote MCP servers.

Curated and provider-hosted only (direct-first: no aggregators — a third
party in the data path is a fence violation, not a convenience). Each entry
is reachable through the one generic connect flow: streamable HTTP +
spec-standard OAuth. URLs are the providers' published endpoints; every
entry was live-probed (unauthenticated initialize → 401 + OAuth discovery)
on 2026-07-23; `vetromar sources test <name>` verifies one live.

Most providers support dynamic client registration, so connecting is a
single browser consent click. A few (`needs_client_registration`) run
OAuth but require a pre-registered app: the customer creates their own app
once at `setup_url` and pastes its client ID + secret — after which the
same one-click consent flow runs. Still zero per-source code: the seeded
client info just replaces the DCR step (see sources/auth.py).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CatalogEntry(BaseModel):
    name: str
    url: str
    source_kind: str
    description: str
    # Provider's OAuth server has no dynamic client registration — connect
    # needs a customer-registered app's client ID (+ secret), created at
    # `setup_url`. Everything else about the flow stays generic.
    needs_client_registration: bool = False
    setup_url: Optional[str] = None


# One guide covers the whole customer-side setup for every Google Workspace
# entry: Cloud project → enable APIs + MCP services → consent screen →
# OAuth web client (the bare console credentials page would strand them).
_GOOGLE_WORKSPACE_SETUP_URL = (
    "https://developers.google.com/workspace/guides/configure-mcp-servers"
)


CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        name="notion",
        url="https://mcp.notion.com/mcp",
        source_kind="document",
        description="Notion pages and databases",
    ),
    CatalogEntry(
        name="linear",
        url="https://mcp.linear.app/mcp",
        source_kind="ticket",
        description="Linear issues and projects",
    ),
    CatalogEntry(
        name="atlassian",
        url="https://mcp.atlassian.com/v1/mcp",
        source_kind="ticket",
        description="Jira issues and Confluence pages",
    ),
    CatalogEntry(
        name="asana",
        url="https://mcp.asana.com/mcp",
        source_kind="ticket",
        description="Asana tasks and projects",
    ),
    CatalogEntry(
        name="sentry",
        url="https://mcp.sentry.dev/mcp",
        source_kind="metric_pull",
        description="Sentry issues and errors",
    ),
    CatalogEntry(
        name="intercom",
        url="https://mcp.intercom.com/mcp",
        source_kind="chat",
        description="Intercom customer conversations",
    ),
    CatalogEntry(
        name="slack",
        url="https://mcp.slack.com/mcp",
        source_kind="chat",
        description="Slack channels and messages",
        needs_client_registration=True,
        setup_url="https://api.slack.com/apps",
    ),
    CatalogEntry(
        name="posthog",
        url="https://mcp.posthog.com/mcp",
        source_kind="metric_pull",
        description="PostHog analytics, insights and feature flags",
    ),
    CatalogEntry(
        name="github",
        url="https://api.githubcopilot.com/mcp/",
        source_kind="ticket",
        description="GitHub issues, pull requests and repos",
        needs_client_registration=True,
        setup_url="https://github.com/settings/applications/new",
    ),
    CatalogEntry(
        name="hubspot",
        url="https://mcp.hubspot.com/anthropic",
        source_kind="document",
        description="HubSpot CRM contacts, companies and deals",
        needs_client_registration=True,
        setup_url="https://developers.hubspot.com",
    ),
    CatalogEntry(
        name="figma",
        url="https://mcp.figma.com/mcp",
        source_kind="document",
        description="Figma files and design context",
    ),
    CatalogEntry(
        name="monday",
        url="https://mcp.monday.com/mcp",
        source_kind="ticket",
        description="monday.com boards and items",
    ),
    CatalogEntry(
        name="clickup",
        url="https://mcp.clickup.com/mcp",
        source_kind="ticket",
        description="ClickUp tasks, docs and spaces",
    ),
    CatalogEntry(
        name="box",
        url="https://mcp.box.com/mcp",
        source_kind="document",
        description="Box files and folders",
        needs_client_registration=True,
        setup_url="https://app.box.com/developers/console",
    ),
    CatalogEntry(
        name="canva",
        url="https://mcp.canva.com/mcp",
        source_kind="document",
        description="Canva designs",
    ),
    CatalogEntry(
        name="webflow",
        url="https://mcp.webflow.com/mcp",
        source_kind="document",
        description="Webflow sites and CMS content",
    ),
    CatalogEntry(
        name="stripe",
        url="https://mcp.stripe.com/",
        source_kind="metric_pull",
        description="Stripe customers, subscriptions and payments",
    ),
    CatalogEntry(
        name="square",
        url="https://mcp.squareup.com/mcp",
        source_kind="metric_pull",
        description="Square payments, orders and customers",
    ),
    CatalogEntry(
        name="paypal",
        url="https://mcp.paypal.com/mcp",
        source_kind="metric_pull",
        description="PayPal transactions and invoices",
    ),
    CatalogEntry(
        name="vercel",
        url="https://mcp.vercel.com/",
        source_kind="metric_pull",
        description="Vercel projects and deployments",
    ),
    # Google Workspace: official per-product remote MCP servers (rolling out
    # since 2026-05). accounts.google.com has no DCR, so all six are
    # credential-class; one customer-created Google OAuth client (Internal
    # consent screen, relay redirect URI) backs every entry.
    CatalogEntry(
        name="gmail",
        url="https://gmailmcp.googleapis.com/mcp/v1",
        source_kind="email",
        description="Gmail messages and threads",
        needs_client_registration=True,
        setup_url=_GOOGLE_WORKSPACE_SETUP_URL,
    ),
    CatalogEntry(
        name="google-drive",
        url="https://drivemcp.googleapis.com/mcp/v1",
        source_kind="document",
        description="Google Drive files and folders",
        needs_client_registration=True,
        setup_url=_GOOGLE_WORKSPACE_SETUP_URL,
    ),
    CatalogEntry(
        name="google-calendar",
        url="https://calendarmcp.googleapis.com/mcp/v1",
        source_kind="document",
        description="Google Calendar events and availability",
        needs_client_registration=True,
        setup_url=_GOOGLE_WORKSPACE_SETUP_URL,
    ),
    CatalogEntry(
        name="google-docs",
        url="https://docsmcp.googleapis.com/mcp/v1",
        source_kind="document",
        description="Google Docs documents",
        needs_client_registration=True,
        setup_url=_GOOGLE_WORKSPACE_SETUP_URL,
    ),
    CatalogEntry(
        name="google-sheets",
        url="https://sheetsmcp.googleapis.com/mcp/v1",
        source_kind="document",
        description="Google Sheets spreadsheets",
        needs_client_registration=True,
        setup_url=_GOOGLE_WORKSPACE_SETUP_URL,
    ),
    CatalogEntry(
        name="google-chat",
        url="https://chatmcp.googleapis.com/mcp/v1",
        source_kind="chat",
        description="Google Chat spaces and messages",
        needs_client_registration=True,
        setup_url=_GOOGLE_WORKSPACE_SETUP_URL,
    ),
]


def catalog_entry(name: str) -> CatalogEntry | None:
    for entry in CATALOG:
        if entry.name == name:
            return entry
    return None
