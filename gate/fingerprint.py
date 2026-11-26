"""Canonical fingerprint of one Flagsmith environment, for the stale-state check.

Source: the SDK **edge environment document**
(GET https://edge.api.flagsmith.com/api/v1/environment-document/ with a server-side `ser.` key).

Why this source and not the admin/CLI document (`flagsmith environment document`): in the
Nov 26 spike the admin document kept serving the pre-write state for 26-68 s after a published
v2 write, while the edge document and the live version states were already current. A lagging
source would let a change made in the last minute pass the stale check. The edge document is
also exactly what the SDK evaluates, so the fingerprint and the verifier's read-back see the
same thing, and one call covers every flag, segment rule and segment override.
The admin and edge documents canonicalise to different hashes for the same state, so the gate
uses this one source only, at proposal time and right before the write.

What is hashed: feature states (name, enabled, value, multivariate), segment definitions
(name, rules) and segment overrides (feature name, enabled, value, priority). Keys sorted,
lists sorted, ids/uuids/timestamps dropped. Identity overrides and environment settings are
out of scope. Values keep their JSON type, so "6" and 6 differ.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

EDGE_DOCUMENT_URL = "https://edge.api.flagsmith.com/api/v1/environment-document/"

VOLATILE_KEYS = frozenset({
    "id", "django_id", "uuid", "featurestate_uuid", "mv_fs_value_uuid",
    "created_at", "updated_at", "api_key",
})


class FingerprintError(Exception):
    pass


def fetch_environment_document(server_key: str, *, client: httpx.Client | None = None,
                               timeout: float = 15.0) -> dict[str, Any]:
    """Fetch the edge environment document. The key is sent only as a header, never logged."""
    if not server_key.startswith("ser."):
        raise FingerprintError("the edge document needs a server-side (ser.) environment key")
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.get(EDGE_DOCUMENT_URL, headers={"X-Environment-Key": server_key})
    except httpx.HTTPError as exc:
        raise FingerprintError(f"edge document unreachable: {type(exc).__name__}") from None
    finally:
        if own:
            http.close()
    if response.status_code != 200:
        raise FingerprintError(f"edge document returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise FingerprintError("edge document was not JSON") from None


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in sorted(value.items()) if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        items = [_clean(v) for v in value]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    return value


def _feature_state(fs: dict[str, Any], *, with_priority: bool) -> tuple[str, dict[str, Any]]:
    view: dict[str, Any] = {
        "enabled": fs["enabled"],
        "value": fs.get("feature_state_value"),
        "multivariate": _clean(fs.get("multivariate_feature_state_values") or []),
    }
    if with_priority:
        view["priority"] = (fs.get("feature_segment") or {}).get("priority")
    return fs["feature"]["name"], view


def canonical_view(document: dict[str, Any]) -> dict[str, Any]:
    """The part of the document the fingerprint covers, in canonical form."""
    try:
        features = dict(_feature_state(fs, with_priority=False)
                        for fs in document["feature_states"])
        segments = {}
        for seg in (document.get("project") or {}).get("segments") or []:
            segments[seg["name"]] = {
                "rules": _clean(seg.get("rules") or []),
                "overrides": dict(_feature_state(fs, with_priority=True)
                                  for fs in seg.get("feature_states") or []),
            }
    except (KeyError, TypeError, AttributeError) as exc:
        raise FingerprintError(f"not an environment document ({type(exc).__name__}: {exc})") \
            from None
    return {"features": features, "segments": segments}


def fingerprint(document: dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical view."""
    blob = json.dumps(canonical_view(document), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
