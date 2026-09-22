"""
Minimal HS256 JWT — mint and verify.

Deliberately stdlib-only so the mock runs with no pip install. It mirrors the
shape TVS actually uses (per the Job Card payload doc): a Bearer token whose
claims carry DealerId, BranchId and UserId, which every endpoint then validates
AGAINST THE REQUEST. A mismatch is a 401.

In the real DMS this is issued by Login/TokenGeneration and signed with a key
held on the TVS side. Do not copy this file into production — copy the SHAPE.
"""
import base64, hmac, hashlib, json, time

# Mock signing key. The real one lives in TVS's secret store.
SECRET = b"tvs-mock-dms-signing-key-not-for-production"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(dealer_id, branch_id, user_id=None, ttl_seconds: int = 8 * 3600) -> str:
    """8 hours by default — the DMS session length quoted on the 8 Sep call."""
    header  = {"alg": "HS256", "typ": "JWT"}
    now     = int(time.time())
    payload = {
        "DealerId": str(dealer_id),
        "BranchId": str(branch_id),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    # The DMS user id is a STRING in the real system (e.g. DLR13111.BILL01), and a
    # caller that does not know it still gets a token bound to the dealer and branch
    # — which is what separates one dealer from another. Binding a user we were never
    # given would be inventing a claim.
    if user_id not in (None, "", 0, "0"):
        payload["UserId"] = str(user_id)
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = hmac.new(SECRET, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


def verify(token: str):
    """Returns (claims, None) or (None, reason)."""
    try:
        h, p, s = token.split(".")
    except ValueError:
        return None, "Malformed token"

    expected = _b64(hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, s):
        return None, "Signature validation failed"

    claims = json.loads(_unb64(p))
    if claims.get("exp", 0) < int(time.time()):
        return None, "Token expired"
    return claims, None


def validate_against_request(token: str, dealer_id, branch_id, user_id):
    """
    THE RULE THAT MATTERS.

    TVS: "every endpoint validates the JWT claims (DealerId, BranchId, UserId)
    against the request via sc.ValidateToken(...). Mismatch -> 401."

    This is what stops dealer A asking for dealer B's invoice, and it is why a
    single shared 'master token' cannot work — the token has to be the dealer's.
    """
    claims, err = verify(token)
    if err:
        return None, err
    for name, sent in (("DealerId", dealer_id), ("BranchId", branch_id), ("UserId", user_id)):
        if sent is None or name not in claims:
            continue
        if str(claims.get(name)) != str(sent):
            return None, f"Unauthorized Access — {name} in token does not match the request"
    return claims, None
