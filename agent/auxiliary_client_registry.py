"""``auth_type`` dispatch table for the auxiliary registry branch.

``_resolve_registry_branch`` in ``agent/auxiliary_client`` routes every
``PROVIDER_REGISTRY`` provider here on its registered ``auth_type``; this table is the
registry twin of the facade's ``_EXPLICIT_PROVIDER_BRANCHES``. Arms late-import the
facade (siblings never form a module-level cycle) so the seam every test patches —
``agent.auxiliary_client.*`` — is the module production reads.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict

if TYPE_CHECKING:
    from agent.auxiliary_client import _ResolveRequest, _ResolveResult


def _resolve_vertex_arm(req: _ResolveRequest) -> _ResolveResult:
    from agent.auxiliary_client import _build_vertex_client, _route_client
    client, final_model = _build_vertex_client(req.provider, req.model)
    return _route_client(req, client, final_model) if client is not None else (None, None)


def _resolve_bedrock_arm(req: _ResolveRequest) -> _ResolveResult:
    from agent.auxiliary_client import _build_bedrock_client, _route_client
    client, final_model = _build_bedrock_client(req.provider, req.model, raw_codex=req.raw_codex)
    return _route_client(req, client, final_model) if client is not None else (None, None)


def _resolve_minimax_oauth_arm(req: _ResolveRequest) -> _ResolveResult:
    from agent.auxiliary_client_minimax import resolve_minimax_oauth_client
    return resolve_minimax_oauth_client(req)


# ``api_key`` / ``external_process`` arms stay on the facade (they share its credential
# resolvers); the table maps the auth types whose arms are build-and-route one-liners
# or topical siblings.
REGISTRY_AUTHTYPE_ARMS: Dict[str, Callable[[_ResolveRequest], _ResolveResult]] = {
    "vertex": _resolve_vertex_arm,
    "aws_sdk": _resolve_bedrock_arm,
    "oauth_minimax": _resolve_minimax_oauth_arm,
}
