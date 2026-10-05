"""MiniMax OAuth arm of the auxiliary registry branch (``auth_type == "oauth_minimax"``).

MiniMax OAuth resolves to an Anthropic-compatible inference endpoint with a callable
bearer: tokens live ~15 minutes and the Anthropic SDK re-invokes the provider on each
request, so a static string would 401 mid-session. ``is_oauth`` comes from
``anthropic_route_is_oauth`` — the MiniMax host is a third-party Anthropic-protocol
endpoint, so the wrapper must NOT carry the native Claude Code OAuth identity (mcp__
tool-name wire transforms, identity rewrites, response prefix stripping); those are
api.anthropic.com-only (#114967).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.auxiliary_client import _ResolveRequest, _ResolveResult

logger = logging.getLogger(__name__)


def resolve_minimax_oauth_client(req: _ResolveRequest) -> _ResolveResult:
    """``minimax-oauth`` → AnthropicAuxiliaryClient over a callable bearer; (None, None) when absent."""
    from agent.auxiliary_client import (
        AnthropicAuxiliaryClient, _AuxProbeClientStub, _aux_probe_active,
        _get_aux_model_for_provider, _normalize_resolved_model, _route_client,
    )
    try:
        from agent.anthropic_adapter import build_anthropic_client
        from agent.anthropic_credentials import anthropic_route_is_oauth
        from hermes_cli.auth import resolve_minimax_oauth_runtime_credentials
    except ImportError:
        return None, None
    try:
        credentials = resolve_minimax_oauth_runtime_credentials(as_token_provider=True)
    except Exception as exc:
        # Deliberate boundary: the resolver contract is "absent → (None, None), never an
        # exception" (the aux ladder then falls through to its Step-2 chain), so every
        # failure ends the arm here; exc_info keeps the traceback diagnosable.
        logger.warning(
            "resolve_provider_client: minimax-oauth runtime resolution failed: %s", exc,
            exc_info=True)
        return None, None
    token_provider = credentials.get("api_key")
    base_url = str(credentials.get("base_url") or "").strip().rstrip("/")
    if not callable(token_provider) or not base_url:
        return None, None
    final_model = _normalize_resolved_model(
        req.model or _get_aux_model_for_provider(req.provider) or "MiniMax-M3", req.provider,
    )
    if _aux_probe_active():
        return _AuxProbeClientStub(api_key="", base_url=base_url), final_model
    try:
        real_client = build_anthropic_client(token_provider, base_url)
    except ImportError:
        return None, None
    client = AnthropicAuxiliaryClient(
        real_client, final_model, token_provider, base_url,
        is_oauth=anthropic_route_is_oauth(base_url, token_provider))
    return _route_client(req, client, final_model)
