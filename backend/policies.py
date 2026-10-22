def evaluate(before, new_status):
    """Deterministic policy for a customer status change. No I/O."""
    if before["protected"]:
        return {
            "allowed": False, "risk": "high", "http_status": 403,
            "reason": "Protected customer: update blocked",
        }
    if before["status"] == new_status:
        return {
            "allowed": False, "risk": "low", "http_status": 409,
            "reason": "Customer already has the requested status",
        }
    return {
        "allowed": True,
        "risk": "medium" if new_status == "Archived" else "low",
        "http_status": 200,
        "reason": None,
    }
