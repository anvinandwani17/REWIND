"""Demo role gate: X-Role + X-Role-Key headers checked against configured keys.

This is NOT an authentication system: keys are shared demo strings, there are
no users or sessions. It only separates what each demo role may do.
"""
import hmac
import os

from fastapi import Header

import demo

ROLES = ("operator", "approver", "auditor")

# Public demo values, not secrets: accepted only in demo mode. Outside it,
# REWIND_ROLE_KEYS must be set or every role-gated route fails closed.
DEFAULT_DEMO_KEYS = {
    "operator": "demo-operator-key",
    "approver": "demo-approver-key",
    "auditor": "demo-auditor-key",
}


class RoleError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def role_keys():
    """REWIND_ROLE_KEYS="operator=k1,approver=k2,auditor=k3"; read per request."""
    raw = os.environ.get("REWIND_ROLE_KEYS")
    if not raw:
        return DEFAULT_DEMO_KEYS if demo.demo_mode_enabled() else {}
    pairs = (part.split("=", 1) for part in raw.split(",") if "=" in part)
    return {role.strip(): key.strip() for role, key in pairs if role.strip() in ROLES}


def require(*allowed):
    """FastAPI dependency: returns the caller's role or raises RoleError."""
    def dependency(x_role: str | None = Header(None), x_role_key: str | None = Header(None)):
        if not x_role or not x_role_key:
            raise RoleError(401, "role_required", "Send X-Role and X-Role-Key headers",
                            {"roles": list(ROLES)})
        keys = role_keys()
        if not keys:
            raise RoleError(401, "role_keys_not_configured",
                            "No role keys configured: set REWIND_ROLE_KEYS "
                            "(the demo keys only work with REWIND_DEMO_MODE=1)")
        expected = keys.get(x_role)
        if expected is None or not hmac.compare_digest(expected, x_role_key):
            raise RoleError(401, "invalid_role_key", "Unknown role or wrong key for that role")
        if x_role not in allowed:
            raise RoleError(403, "role_forbidden", f"Role {x_role} may not do this",
                            {"required": list(allowed), "role": x_role})
        return x_role
    return dependency
