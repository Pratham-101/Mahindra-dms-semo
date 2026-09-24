#!/usr/bin/env python3
"""
Mock OnlineDMS API — Vehicle Invoice slice.

Mirrors the conventions of the real TVS API as documented in
"Job Card APIs — Payload Reference for DevRev.docx":

  base path   /OnlineSalesAPI/
  auth        Authorization: Bearer <JWT>, claims validated against the request
  envelope    { "data": ..., "message": "...", "statusCode": 200, "OTP_Count": 0 }
  codes       200 success · 401 unauthorized · 500 server error

Run:  python3 api/server.py           (default port 8900)
"""
import datetime as dt, hashlib, json, os, sqlite3, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jwt_util

PORT = int(os.environ.get("MOCK_DMS_PORT", "8900"))
DB   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db", "onlinedms.db")
BASE = "/OnlineSalesAPI"


def q(sql, args=()):
    cx = sqlite3.connect(DB); cx.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in cx.execute(sql, args).fetchall()]
    finally:
        cx.close()


# The service credential the DevRev side holds. It can ONLY mint a dealer-scoped
# token — it can never read or write dealer data by itself. That separation is the
# whole point: a leaked service credential still cannot read an invoice.
SERVICE_CLIENTS = {"devrev-dealer-bot": "mock-service-secret-not-for-production"}


def audit(endpoint, outcome, client=None, dealer=None, branch=None, user=None, key=None, detail=None):
    exec_write("""INSERT INTO MDMS_API_AUDIT
                  (CALLED_AT, CLIENT_ID, DEALER_ID, BRANCH_ID, USER_ID, ENDPOINT, IDEMPOTENCY_KEY, OUTCOME, DETAIL)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (dt.datetime.now().isoformat(timespec="seconds"), client, dealer, branch, user,
                endpoint, key, outcome, (detail or "")[:400]))


def exec_write(sql, args=()):
    cx = sqlite3.connect(DB)
    try:
        cur = cx.execute(sql, args); cx.commit(); return cur.rowcount
    finally:
        cx.close()


# ─────────────────────────────── endpoints ───────────────────────────────────

def norm_date(v):
    """
    Accept the date the way a dealer actually types it.

    TVS screens show DD/MM/YYYY, so that is what a dealer reads out and what the
    agent echoes back. SQLite's DATE() only understands ISO, so an unnormalised
    '28/06/2026' silently matched nothing and the dealer was told their invoice
    does not exist. A format difference must never read as a missing invoice.
    """
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    v = v.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v          # hand it through unchanged rather than guessing


def get_invoice_diagnostics(p):
    """
    One call that walks the SOP's whole chain: invoice -> booking -> part ->
    model -> dealer state -> EMPS subsidy, plus the price-master and RTO checks.

    This is the call that replaces a DMS Support engineer running six queries
    by hand, and it is what lets the bot answer instead of escalating.
    """
    dealer   = p.get("DealerID")
    branch   = p.get("BranchID")
    inv_no   = (p.get("InvoiceNo") or "").strip()      # SOP literal had a leading space
    inv_date = norm_date(p.get("InvoiceDate"))

    # NETWORK-WIDE READ. A customer who moves state walks into a different dealer, and
    # that dealer has to be able to see the record — so the invoice is found by its own
    # number and date, NOT by who is asking. The caller still has to present a valid
    # dealer token; what changed is that the token no longer narrows WHICH records are
    # visible, only that the caller is a real dealer.
    inv = q("""SELECT * FROM MDMS_VEHICLE_INVOICE
                WHERE TRIM(INVOICE_NO)=? AND DATE(INVOICE_DATE)=DATE(?)""",
            (inv_no, inv_date))
    if not inv:
        return 200, "Invoice not found", {"Found": False, "InvoiceNo": inv_no, "InvoiceDate": inv_date}
    inv = inv[0]

    # Everything below resolves from the INVOICE's own dealer and branch, never the
    # caller's. The PM E-Drive entitlement is set by the state the vehicle was invoiced
    # in — read it against the caller's state and a Karnataka dealer asking about a
    # Maharashtra invoice would be quoted the wrong money.
    owner_dealer, owner_branch = inv["DEALER_ID"], inv["BRANCH_ID"]
    queried_by_other = (str(owner_dealer) != str(dealer) or str(owner_branch) != str(branch))

    out = {
        "Found": True,
        "QueriedByAnotherDealer": queried_by_other,
        "InvoicedBy": {"DEALER_ID": owner_dealer, "BRANCH_ID": owner_branch},
        "Invoice": {k: inv[k] for k in
                    ("VEH_INVOICE_ID","DEALER_ID","BRANCH_ID","INVOICE_NO","INVOICE_DATE",
                     "BOOKING_ID","CUSTOMER_ID","DISC_VALUE","CRM_REF_CUST_CODE","RTO_ID",
                     "GST_TYPE","INVOICE_STATUS")},
    }

    cust = q("SELECT CUSTOMER_ID, CUSTOMER_NAME, CUSTOMER_TYPE, MOBILE_NO FROM MDMS_CUSTOMER WHERE CUSTOMER_ID=?",
             (inv["CUSTOMER_ID"],))
    out["Customer"] = cust[0] if cust else None

    part = q("""SELECT bp.PART_ID, mp.MODEL_ID, mp.MODEL_DESC
                  FROM MDMS_BOOKING_PART bp
                  JOIN MDMS_MODEL_PART  mp ON mp.PART_ID = bp.PART_ID
                 WHERE bp.BOOKING_ID=? AND bp.DEALER_ID=? AND bp.BRANCH_ID=?""",
             (inv["BOOKING_ID"], owner_dealer, owner_branch))
    out["Model"] = part[0] if part else None

    dlr = q("SELECT STATE_ID, DEALER_NAME FROM MDMS_DEALER WHERE DEALER_ID=? AND BRANCH_ID=?",
            (owner_dealer, owner_branch))
    state = dlr[0]["STATE_ID"] if dlr else None
    out["Dealer"] = dlr[0] if dlr else None

    # PM E-Drive: eligibility is simply "an ACTIVE row exists". Never a flat amount.
    out["Subsidy"] = {"Eligible": False, "EMPS": None, "Reason": "No model resolved"}
    if out["Model"] and state:
        sub = q("SELECT EMPS FROM MDMS_MODEL_SUBSIDY WHERE MODEL_ID=? AND STATE_ID=? AND ACTIVE=1",
                (out["Model"]["MODEL_ID"], state))
        out["Subsidy"] = ({"Eligible": True,  "EMPS": sub[0]["EMPS"], "Reason": None} if sub else
                          {"Eligible": False, "EMPS": None,
                           "Reason": f"No ACTIVE EMPS row for model {out['Model']['MODEL_ID']} in state {state}"})
        out["DiscountAlreadyApplied"] = bool(inv["DISC_VALUE"])
        # Expose the applied figure and whether it agrees with the entitlement, so the
        # caller never has to infer the amount from EMPS.
        out["AppliedDiscount"] = inv["DISC_VALUE"]
        if out["Subsidy"]["Eligible"] and inv["DISC_VALUE"]:
            out["AppliedMatchesEntitlement"] = abs(inv["DISC_VALUE"] - out["Subsidy"]["EMPS"]) < 0.5

        # Scenario 6 — is a price defined for THIS customer type?
        ctype = (out["Customer"] or {}).get("CUSTOMER_TYPE")
        if ctype:
            pr = q("""SELECT PRICE FROM MDMS_VEHICLE_PRICE_MASTER
                       WHERE MODEL_ID=? AND STATE_ID=? AND CUSTOMER_TYPE=? AND ACTIVE=1""",
                   (out["Model"]["MODEL_ID"], state, ctype))
            # Name the DevRev part outright. Which part a Price Not Defined ticket
            # belongs on is decided by ONE fact the DMS already holds — the customer
            # type — so the DMS decides it rather than leaving the agent to infer it
            # from a sentence. Routing this wrong means the ticket sits Unassigned
            # with nobody asked to approve, and it went wrong intermittently while
            # the rule lived only in the agent's instructions.
            if not pr:
                out["TicketPart"] = ("don:core:dvrv-us-1:devo/11CBDUMr66:feature/89"
                                     if ctype == "CSD" else
                                     "don:core:dvrv-us-1:devo/11CBDUMr66:feature/38")
                out["TicketPartReason"] = (
                    f"Price Not Defined for a {ctype} customer. "
                    + ("CSD -> the Sales Institutional Team approval part."
                       if ctype == "CSD" else
                       "Not CSD -> the ordinary Price Not Defined part, no approval."))
            out["PriceMaster"] = {
                "CustomerType": ctype,
                "PriceDefined": bool(pr),
                "Price": pr[0]["PRICE"] if pr else None,
                "RoutingHint": None if pr else
                    ("CSD — requires Sales Institutional Team approval before a price can be created"
                     if ctype == "CSD" else
                     "Non-CSD — the dealer can create the price in Vehicle Price Master directly"),
            }

    # A ready-made line for the caller to relay verbatim.
    # Deliberate: the formatting decision lives with the system of record, not with
    # the bot. It also keeps raw rows out of the conversation layer.
    # Wrapped in markers so a caller can lift the sentence out of the raw JSON
    # without needing a JSON parser. DevRev's HTTP node returns the body as a
    # STRING, and its JSONata has no $eval — markers make the extraction trivial
    # and quote-safe.
    out["BotSummary"] = "[[S]]" + _bot_summary(out) + "[[/S]]"

    # Scenario 7 — the fk_rto_book conflict is an INACTIVE RTO row.
    if inv["RTO_ID"]:
        rto = q("SELECT RTO_ID, RTO_CODE, RTO_NAME, ACTIVE FROM MDMS_RTO WHERE RTO_ID=?", (inv["RTO_ID"],))
        if rto:
            out["RTO"] = dict(rto[0])
            out["RTO"]["WouldViolateFK"] = not bool(rto[0]["ACTIVE"])
    return 200, "Success", out


def _bot_summary(d):
    """One sentence a support bot can say to a dealer without interpreting anything."""
    m = d.get("Model") or {}
    sub = d.get("Subsidy") or {}
    pm = d.get("PriceMaster") or {}
    model = m.get("MODEL_DESC") or "the model on this booking"
    state = (d.get("Dealer") or {}).get("STATE_ID") or "this state"

    if pm and not pm.get("PriceDefined"):
        csd = pm.get("CustomerType") == "CSD"
        who = ("the Sales Institutional Team must approve before a CSD price can be created"
               if csd else "the dealer can create the price in Vehicle Price Master directly")
        # Name the article to read. The agent cannot set a part — DevRev files the
        # ticket against whichever article was retrieved — and left to infer it from
        # the sentence it read the Individual article for CSD customers every time.
        # Saying it in the DMS's own sentence is the one channel that is always relayed.
        art = ("Scenario 6 Path A: Price Not Defined for a CSD customer" if csd
               else "Scenario 6 Path B: Price Not Defined for an Individual customer")
        return (f"No {pm.get('CustomerType')} price is defined for {model} in {state}. "
                f"Per the SOP, {who}. "
                f"[[ACTION: before escalating, read the knowledge article \"{art}\" — "
                f"the ticket is filed against the article you read, and this is a "
                f"{pm.get('CustomerType')} customer.]]")

    if sub.get("Eligible"):
        # DISC_VALUE is what is ACTUALLY on the invoice; EMPS is only the entitlement.
        # Reporting EMPS as though it were the applied figure was a real defect: 69 of
        # the 89 applied invoices in this database carry a DISC_VALUE that differs from
        # EMPS, and every one of them was being described with the wrong number.
        applied = d.get("AppliedDiscount")
        if d.get("DiscountAlreadyApplied"):
            if applied is not None and abs(applied - sub["EMPS"]) >= 0.5:
                return (f"{model} in {state} carries a PM E-Drive subsidy of "
                        f"{sub['EMPS']:.0f}, but the amount on this invoice is "
                        f"{applied:.0f} — the two do NOT match. The invoice was not "
                        f"discounted by the subsidy figure, so this needs L1 Support to "
                        f"check why the applied amount differs.")
            return (f"{model} in {state} carries a PM E-Drive subsidy of "
                    f"{sub['EMPS']:.0f}, and that exact amount is ALREADY applied to "
                    f"this invoice.")
        return (f"{model} in {state} is eligible for a PM E-Drive subsidy of "
                f"{sub['EMPS']:.0f}, and it has NOT yet been applied to this invoice.")
    return (f"{model} has no active PM E-Drive subsidy in {state} — "
            f"the dealer should contact their EV Team. No amount is payable.")


def get_emps_subsidy(p):
    rows = q("SELECT MODEL_ID, STATE_ID, EMPS, ACTIVE FROM MDMS_MODEL_SUBSIDY WHERE MODEL_ID=? AND STATE_ID=?",
             (p.get("ModelId"), p.get("StateId")))
    if not rows:
        return 200, "Success", {"Eligible": False, "EMPS": None,
                                "Reason": "No subsidy row for this model and state — dealer should contact their EV Team"}
    r = rows[0]
    if not r["ACTIVE"]:
        return 200, "Success", {"Eligible": False, "EMPS": None,
                                "Reason": "Subsidy row exists but is INACTIVE for this model and state"}
    return 200, "Success", {"Eligible": True, "EMPS": r["EMPS"], "Reason": None}


def check_emr_usage(p):
    """The check the bot currently has to escalate for: is this EMR code already used?"""
    code = (p.get("EmrCode") or "").strip()
    rows = q("""SELECT DEALER_ID, BRANCH_ID, INVOICE_NO, DATE(INVOICE_DATE) AS INVOICE_DATE, CUSTOMER_ID
                  FROM MDMS_VEHICLE_INVOICE WHERE CRM_REF_CUST_CODE=? ORDER BY INVOICE_DATE""", (code,))
    return 200, "Success", {"EmrCode": code, "UsageCount": len(rows),
                            "AlreadyUsed": len(rows) > 0, "Usages": rows}


def update_discount_value(body, idem_key=None):
    """
    POST — the one write.

    Two layers of replay protection, because a network retry and a duplicate
    instruction look identical from here:
      1. Idempotency-Key — a replay returns the ORIGINAL response, no second write.
      2. value comparison — even with no key, writing the value it already has is a no-op.
    And the response is always RE-READ from the row, so the caller is told what is
    actually stored rather than what it asked for.
    """
    dealer, branch = body.get("DEALER_ID"), body.get("BRANCH_ID")
    inv_no  = str(body.get("INVOICE_NO", "")).strip()
    inv_dt  = body.get("INVOICE_DATE")
    new_val = body.get("DISC_VALUE")
    if new_val is None:
        return 500, "DISC_VALUE is required", None

    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    if idem_key:
        prior = q("SELECT REQUEST_HASH, RESPONSE_JSON FROM MDMS_API_IDEMPOTENCY WHERE IDEMPOTENCY_KEY=?", (idem_key,))
        if prior:
            if prior[0]["REQUEST_HASH"] != req_hash:
                return 500, "Idempotency-Key reused with a different body", None
            audit("VehicleInvoice/UpdateDiscountValue", "REPLAYED", dealer=dealer, branch=branch,
                  user=body.get("USER_ID"), key=idem_key, detail="returned the original response")
            out = json.loads(prior[0]["RESPONSE_JSON"]); out["Replayed"] = True
            return 200, "Success", out

    cur = q("""SELECT VEH_INVOICE_ID, DISC_VALUE FROM MDMS_VEHICLE_INVOICE
                WHERE DEALER_ID=? AND BRANCH_ID=? AND TRIM(INVOICE_NO)=? AND DATE(INVOICE_DATE)=DATE(?)""",
            (dealer, branch, inv_no, inv_dt))
    if not cur:
        return 500, "Invoice not found", None
    before = cur[0]["DISC_VALUE"]
    if before == new_val:
        out = {"Changed": False, "PreviousValue": before, "NewValue": new_val, "Replayed": False,
               "VerifiedFromDatabase": True,
               "Note": "Value already set — no write performed (idempotent)"}
    else:
        exec_write("UPDATE MDMS_VEHICLE_INVOICE SET DISC_VALUE=? WHERE VEH_INVOICE_ID=?",
                   (new_val, cur[0]["VEH_INVOICE_ID"]))
        # RE-READ. A successful UPDATE is not evidence the value landed.
        after = q("SELECT DISC_VALUE FROM MDMS_VEHICLE_INVOICE WHERE VEH_INVOICE_ID=?",
                  (cur[0]["VEH_INVOICE_ID"],))[0]["DISC_VALUE"]
        out = {"Changed": after == new_val, "PreviousValue": before, "NewValue": after,
               "Replayed": False, "VerifiedFromDatabase": True}
        if after != new_val:
            return 500, "Write did not land — the row still holds the previous value", out

    if idem_key:
        exec_write("""INSERT INTO MDMS_API_IDEMPOTENCY
                      (IDEMPOTENCY_KEY, ENDPOINT, DEALER_ID, REQUEST_HASH, RESPONSE_JSON, CREATED_AT)
                      VALUES (?,?,?,?,?,?)""",
                   (idem_key, "VehicleInvoice/UpdateDiscountValue", dealer, req_hash,
                    json.dumps(out), dt.datetime.now().isoformat(timespec="seconds")))
    audit("VehicleInvoice/UpdateDiscountValue", "OK", dealer=dealer, branch=branch,
          user=body.get("USER_ID"), key=idem_key, detail=f"{before} -> {out['NewValue']}")
    return 200, "Success", out


def service_token_for_dealer(headers, body):
    """
    THE FIX for "a shared master token cannot work".

    The DevRev side never holds a dealer's token and never holds a token that can
    read data. It holds a SERVICE CREDENTIAL whose only power is to ask TVS for a
    token scoped to ONE dealer — the dealer DevRev has already verified through the
    PluG session. TVS decides whether to issue it, and records that it did.

    So the chain is:
        DMS login  ->  DevRev verified session (we built this)
        ->  workflow presents service credential + that dealer's id
        ->  TVS mints a token good for that dealer only
        ->  every data call is scoped, and every mint is audited.
    """
    client = headers.get("X-Service-Client")
    secret = headers.get("X-Service-Secret")
    ep = "Login/ServiceTokenForDealer"

    if not client or SERVICE_CLIENTS.get(client) != secret:
        audit(ep, "UNAUTHORIZED", client=client, detail="bad service credential")
        return 401, "Unauthorized Access — service credential rejected", None

    dealer, branch, user = body.get("DealerId"), body.get("BranchId"), body.get("UserId")
    if not all([dealer, branch]):
        audit(ep, "ERROR", client=client, detail="missing dealer context")
        return 500, "DealerId and BranchId are required", None

    # The dealer must actually exist. A service credential cannot conjure one.
    if not q("SELECT 1 FROM MDMS_DEALER WHERE DEALER_ID=? AND BRANCH_ID=? AND ACTIVE=1", (dealer, branch)):
        audit(ep, "UNAUTHORIZED", client=client, dealer=dealer, branch=branch, user=user,
              detail="unknown or inactive dealer/branch")
        return 401, "Unauthorized Access — unknown or inactive dealer/branch", None

    tok = jwt_util.mint(dealer, branch, user, ttl_seconds=900)   # 15 min, not 8 hours
    audit(ep, "OK", client=client, dealer=dealer, branch=branch, user=user, detail="token issued")
    return 200, "Success", {"Token": tok, "ExpiresInSeconds": 900,
                            "ScopedTo": {"DealerId": dealer, "BranchId": branch, "UserId": user or None}}



def get_amc_diagnostics(p):
    """
    AMC Issues — one call that does what the entry step plus the scenario guards
    need: find the AMC by frame and AMC number, confirm they are the SAME vehicle,
    read STATUS, and resolve the dealership the AMC is open under together with
    whether that dealership is still active.

    That last fact is the whole of Scenario 4: the same request is handled three
    different ways depending on it.
    """
    frame  = (p.get("FrameNo") or "").strip()
    amc_no = (p.get("AmcNo") or "").strip()

    rows = q("SELECT * FROM MDMS_AMC WHERE TRIM(AMC_NO)=? AND ACTIVE=1", (amc_no,))
    if not rows:
        return 200, "AMC not found", {"Found": False, "AmcNo": amc_no, "FrameNo": frame}
    amc = rows[0]

    # The entry step exists to catch exactly this.
    if frame and amc["FRAME_NO"].strip().upper() != frame.upper():
        return 200, "AMC and frame do not match", {
            "Found": True, "FrameMatches": False,
            "AmcNo": amc_no, "FrameOnAmc": amc["FRAME_NO"], "FrameGiven": frame,
            "BotSummary": "[[S]]AMC " + amc_no + " is registered against frame " +
                          amc["FRAME_NO"] + ", not " + frame + ". Confirm which vehicle "
                          "this request is for before anything is changed.[[/S]]"}

    ds = q("SELECT * FROM MDMS_DEALERSHIP WHERE DEALERSHIP_CODE=?", (amc["DEALERSHIP_CODE"],))
    ds = ds[0] if ds else None
    STATUS = {0: "Open", 1: "Closed", 2: "Cancelled"}
    st = STATUS.get(amc["STATUS"], "Unknown")

    out = {"Found": True, "FrameMatches": True,
           "Amc": {k: amc[k] for k in ("AMC_NO","FRAME_NO","DEALER_ID","BRANCH_ID",
                                       "CUSTOMER_ID","STATUS","VALID_FROM","VALID_TILL")},
           "StatusLabel": st,
           "IsOpen": amc["STATUS"] == 0,
           "Dealership": ({"DEALERSHIP_CODE": ds["DEALERSHIP_CODE"], "DEALER_CODE": ds["DEALER_CODE"],
                           "DEALERSHIP_NAME": ds["DEALERSHIP_NAME"], "ACTIVE": ds["ACTIVE"]}
                          if ds else None)}

    caller = str(p.get("DealerID") or "")
    own = ds is not None and ds["DEALERSHIP_CODE"] == f"DS{caller}{p.get('BranchID')}"
    out["HeldByCallingDealership"] = own

    if not out["IsOpen"]:
        out["BotSummary"] = ("[[S]]AMC " + amc_no + " is " + st + ", not Open. The validity "
            "dates cannot be changed on an AMC in this state — a new AMC has to be created "
            "instead.[[/S]]")
    elif own:
        out["BotSummary"] = ("[[S]]AMC " + amc_no + " is Open under your own dealership, valid "
            + str(amc["VALID_FROM"])[:10] + " to " + str(amc["VALID_TILL"])[:10] +
            ". You can close or cancel it yourself with customer OTP consent.[[/S]]")
    elif ds and not ds["ACTIVE"]:
        out["BotSummary"] = ("[[S]]AMC " + amc_no + " is Open under " + ds["DEALERSHIP_NAME"] +
            ", which is INACTIVE. Support can proceed with the cancellation.[[/S]]")
    else:
        out["BotSummary"] = ("[[S]]AMC " + amc_no + " is Open under " +
            (ds["DEALERSHIP_NAME"] if ds else "another dealership") + ", which is active. "
            "The close or cancel has to be done by that dealership with customer consent — "
            "it cannot be done from here.[[/S]]")
    return 200, "Success", out


def get_jobtype_diagnostics(p):
    """
    Job Type Issues — reads the job card for the frame, then the model-level job
    types with their eligibility fields, and decides which of the three branches
    of "not listing" applies.

    PopulateJobCardDetailsAngular returns JOB_TYPE_ID but NOT model-level
    eligibility, which is the entire reason the EnabledJobTypes wrapper exists.
    """
    frame = (p.get("FrameNo") or "").strip()
    want  = p.get("RequestedJobTypeId")
    want  = int(want) if str(want or "").strip().isdigit() else None

    jc = q("SELECT * FROM MDMS_JOB_CARD WHERE TRIM(FRAME_NO)=? AND ACTIVE=1", (frame,))
    if not jc:
        return 200, "Job card not found", {"Found": False, "FrameNo": frame}
    jc = jc[0]

    types = q("""SELECT mjt.JOB_TYPE_ID, jt.JOB_TYPE_DESC, mjt.VALID_KM, mjt.GRACE_KM,
                        mjt.VALID_DAYS, mjt.GRACE_DAYS, mjt.ACTIVE
                   FROM MDMS_MODEL_JOB_TYPE mjt
                   JOIN MDMS_JOB_TYPE jt ON jt.JOB_TYPE_ID = mjt.JOB_TYPE_ID
                  WHERE mjt.MODEL_ID=?""", (jc["MODEL_ID"],))
    cur = q("SELECT JOB_TYPE_DESC FROM MDMS_JOB_TYPE WHERE JOB_TYPE_ID=?", (jc["JOB_TYPE_ID"],))

    out = {"Found": True,
           "JobCard": {k: jc[k] for k in ("JC_NO","FRAME_NO","MODEL_ID","JOB_TYPE_ID",
                                          "CURRENT_KM","SALE_DATE","STATUS")},
           "CurrentJobType": (cur[0]["JOB_TYPE_DESC"] if cur else None),
           "EnabledJobTypes": [t for t in types if t["ACTIVE"]],
           "AllModelJobTypes": types}

    # "already created" uses the source's own exclusion: STATUS not in (3,6)
    out["OpenJobCardExists"] = jc["STATUS"] not in (3, 6)

    if want is None:
        out["BotSummary"] = ("[[S]]The job card on frame " + frame + " is currently " +
            str(out["CurrentJobType"]) + ". Enabled job types for this model: " +
            ", ".join(t["JOB_TYPE_DESC"] for t in out["EnabledJobTypes"]) + ".[[/S]]")
        return 200, "Success", out

    row = next((t for t in types if t["JOB_TYPE_ID"] == want), None)
    label = next((t["JOB_TYPE_DESC"] for t in types if t["JOB_TYPE_ID"] == want),
                 (q("SELECT JOB_TYPE_DESC FROM MDMS_JOB_TYPE WHERE JOB_TYPE_ID=?", (want,)) or
                  [{"JOB_TYPE_DESC": str(want)}])[0]["JOB_TYPE_DESC"])

    if row is None or not row["ACTIVE"]:
        # Branch C — the model does not carry it, or carries it switched off.
        out["Branch"] = "C_MANUAL_VS_DMS"
        out["TicketPart"] = "don:core:dvrv-us-1:devo/11CBDUMr66:feature/60"
        out["TicketPartReason"] = "Job Type Not Listing — manual versus DMS conflict."
        out["BotSummary"] = ("[[S]]" + label + " is not enabled for this model in the DMS. If "
            "the dealer manual says it should be, that is a conflict between the manual and the "
            "DMS configuration and needs an L1 ticket to investigate — it is not something to "
            "decide here.[[/S]]")
        return 200, "Success", out

    km_limit = (row["VALID_KM"] or 0) + (row["GRACE_KM"] or 0)
    beyond_km = (jc["CURRENT_KM"] or 0) > km_limit
    day_limit = (row["VALID_DAYS"] or 0) + (row["GRACE_DAYS"] or 0)
    days = None
    try:
        sale = dt.date.fromisoformat(str(jc["SALE_DATE"])[:10])
        days = (dt.date.today() - sale).days
    except Exception:                                            # noqa: BLE001
        pass
    beyond_days = days is not None and days > day_limit
    out["Eligibility"] = {"JobType": label, "CurrentKM": jc["CURRENT_KM"],
                          "KmLimit": km_limit, "BeyondKm": beyond_km,
                          "DaysSinceSale": days, "DayLimit": day_limit,
                          "BeyondDays": beyond_days}

    if beyond_km or beyond_days:
        out["Branch"] = "B_BEYOND_ELIGIBILITY"
        out["TicketPart"] = "don:core:dvrv-us-1:devo/11CBDUMr66:feature/60"
        out["TicketPartReason"] = "Job Type Not Listing — beyond eligibility, normally no ticket."
        why = []
        if beyond_km:   why.append(f"{jc['CURRENT_KM']} km against a limit of {km_limit} km")
        if beyond_days: why.append(f"{days} days since sale against a limit of {day_limit} days")
        out["BotSummary"] = ("[[S]]" + label + " is not available on this vehicle because it is "
            "beyond eligibility: " + " and ".join(why) + ". This is the system behaving "
            "correctly, so no ticket is needed.[[/S]]")
    else:
        out["Branch"] = "A_WITHIN_ELIGIBILITY"
        out["TicketPart"] = "don:core:dvrv-us-1:devo/11CBDUMr66:feature/60"
        out["TicketPartReason"] = "Job Type Not Listing — within eligibility, needs an L1 ticket."
        out["BotSummary"] = ("[[S]]" + label + " IS enabled for this model and the vehicle is "
            "within eligibility (" + str(jc["CURRENT_KM"]) + " km against a limit of " +
            str(km_limit) + " km). If it still does not list, that needs an L1 ticket with "
            "these eligibility figures attached.[[/S]]")

    # A CHANGE REQUEST outranks the not-listing branch. The dealer asking to move
    # 31 -> 12 is Scenario 1, which is an ASM approval on feature/59; the branch
    # logic above also ran and set feature/60, so this has to come last.
    if want == 12 and jc["JOB_TYPE_ID"] == 31:
        out["TicketPart"] = "don:core:dvrv-us-1:devo/11CBDUMr66:feature/59"
        out["TicketPartReason"] = ("Job Type Change Request: Paid Service (31) to Running "
                                   "Repair (12). The one permitted change, gated on ASM approval.")
    return 200, "Success", out



def update_amc_validity_dates(body, idem_key=None):
    """
    AMC Scenario 1, step 4 — the ONE write in the AMC use case.

    Today this is a raw SQL UPDATE on the AMC table with no REST route, so the
    wrapper does what a raw UPDATE cannot: it refuses on a closed or cancelled
    AMC, captures a before/after snapshot for the audit trail, and reads the row
    back so the caller never has to trust that the write landed.

    VALID_FROM = today, VALID_TILL = today + one year, per the source.
    """
    amc_no = (body.get("AMC_NO") or "").strip()
    dealer, branch = body.get("DEALER_ID"), body.get("BRANCH_ID")

    rows = q("SELECT * FROM MDMS_AMC WHERE TRIM(AMC_NO)=? AND ACTIVE=1", (amc_no,))
    if not rows:
        audit("AMC/UpdateAMCValidityDates", "NOT_FOUND", dealer=dealer, branch=branch,
              key=idem_key, detail=f"no AMC {amc_no}")
        return 200, "AMC not found", {"Changed": False, "AmcNo": amc_no}
    amc = rows[0]

    # The guard the SOP puts before the write. A raw UPDATE has no such guard,
    # which is exactly why this is a wrapper and not a passthrough.
    if amc["STATUS"] != 0:
        label = {1: "Closed", 2: "Cancelled"}.get(amc["STATUS"], "not Open")
        audit("AMC/UpdateAMCValidityDates", "REFUSED", dealer=dealer, branch=branch,
              key=idem_key, detail=f"AMC {amc_no} is {label}")
        return 200, "AMC is not Open", {
            "Changed": False, "AmcNo": amc_no, "StatusLabel": label,
            "BotSummary": "[[S]]AMC " + amc_no + " is " + label + ", not Open. The validity "
                          "dates cannot be changed on an AMC in this state — a new AMC has to "
                          "be created instead.[[/S]]"}

    before = {"VALID_FROM": amc["VALID_FROM"], "VALID_TILL": amc["VALID_TILL"]}
    today = dt.date.today()
    new_from, new_till = str(today), str(today.replace(year=today.year + 1))

    if idem_key:
        seen = q("SELECT * FROM MDMS_API_IDEMPOTENCY WHERE IDEMPOTENCY_KEY=?", (idem_key,))
        if seen:
            return 200, "Replayed", {"Changed": False, "Replayed": True, "AmcNo": amc_no,
                                     "VALID_FROM": new_from, "VALID_TILL": new_till}
        exec_write("""INSERT INTO MDMS_API_IDEMPOTENCY
                      (IDEMPOTENCY_KEY, ENDPOINT, DEALER_ID, REQUEST_HASH, RESPONSE_JSON, CREATED_AT)
                      VALUES (?,?,?,?,?,?)""",
                   (idem_key, "AMC/UpdateAMCValidityDates", dealer,
                    hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(), "",
                    dt.datetime.now().isoformat(timespec="seconds")))

    exec_write("UPDATE MDMS_AMC SET VALID_FROM=?, VALID_TILL=? WHERE TRIM(AMC_NO)=?",
               (new_from, new_till, amc_no))

    # Read it BACK. A write that was accepted is not a write that landed.
    after = q("SELECT VALID_FROM, VALID_TILL FROM MDMS_AMC WHERE TRIM(AMC_NO)=?", (amc_no,))[0]
    ok = after["VALID_FROM"] == new_from and after["VALID_TILL"] == new_till
    audit("AMC/UpdateAMCValidityDates", "OK" if ok else "ERROR", dealer=dealer, branch=branch,
          key=idem_key, detail=f"{amc_no}: {before} -> {dict(after)}")
    return 200, "Success", {
        "Changed": ok, "VerifiedFromDatabase": ok, "AmcNo": amc_no,
        "Before": before, "After": dict(after),
        "BotSummary": ("[[S]]The validity period on AMC " + amc_no + " is now " + new_from +
                       " to " + new_till + ", confirmed by reading the record back. If a job "
                       "card is already open on this vehicle it must be refreshed so the new "
                       "benefits, parts and labour apply.[[/S]]") if ok else
                      ("[[S]]The update to AMC " + amc_no + " did not save. Nothing has been "
                       "changed.[[/S]]")}


# GET routes: path -> (handler, params whose value must match the token claims)
GET_ROUTES = {
    f"{BASE}/VehicleInvoice/GetInvoiceDiagnostics": get_invoice_diagnostics,
    f"{BASE}/VehicleInvoice/GetEmpsSubsidy":        get_emps_subsidy,
    f"{BASE}/VehicleInvoice/CheckEmrUsage":         check_emr_usage,
    f"{BASE}/AMC/GetAMCDiagnostics":                get_amc_diagnostics,
    f"{BASE}/JobType/GetJobTypeDiagnostics":        get_jobtype_diagnostics,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockOnlineDMS/1.0"

    def _reply(self, status_code, message, data):
        body = json.dumps({"data": data, "message": message,
                           "statusCode": status_code, "OTP_Count": 0}, default=str).encode()
        self.send_response(200 if status_code in (200, 401, 500) else status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _html(self, doc):
        b = doc.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _auth(self, dealer, branch, user):
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return None, "Unauthorized Access — missing Bearer token"
        return jwt_util.validate_against_request(hdr[7:], dealer, branch, user)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

        if u.path == f"{BASE}/Login/TokenGeneration":
            tok = jwt_util.mint(int(p.get("DealerID", 0)), int(p.get("BranchID", 0)), int(p.get("UserId", 0)))
            return self._reply(200, "Success", {"Token": tok, "ExpiresInSeconds": 8 * 3600})
        if u.path in ("/", "/index.html", BASE, BASE + "/"):
            return self._html(INDEX_HTML)
        if u.path in ("/db", "/db/"):
            return self._html(db_browser(p))
        if u.path == f"{BASE}/AuditTrail":            # demo/inspection helper
            rows = q("SELECT * FROM MDMS_API_AUDIT ORDER BY AUDIT_ID DESC LIMIT 25")
            return self._reply(200, "Success", {"Recent": rows})
        if u.path == f"{BASE}/health":
            return self._reply(200, "Success", {"status": "up"})

        fn = GET_ROUTES.get(u.path)
        if not fn:
            return self._reply(500, f"No such route: {u.path}", None)

        _, err = self._auth(p.get("DealerID"), p.get("BranchID"), p.get("UserId"))
        if err:
            audit(u.path.replace(BASE + "/", ""), "UNAUTHORIZED",
                  dealer=p.get("DealerID"), branch=p.get("BranchID"), user=p.get("UserId"), detail=err)
            return self._reply(401, err, None)
        try:
            res = fn(p)
            audit(u.path.replace(BASE + "/", ""), "OK",
                  dealer=p.get("DealerID"), branch=p.get("BranchID"), user=p.get("UserId"))
            return self._reply(*res)
        except Exception as e:                                   # noqa: BLE001
            return self._reply(500, f"Server error: {e}", None)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) or b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            # Fail LOUDLY. A malformed body used to 500 without a trace, which made a
            # caller that sends bad JSON look like a caller that never called at all.
            audit(u.path.replace(BASE + "/", ""), "BAD_REQUEST",
                  detail="body is not valid JSON: " + raw.decode("utf-8", "replace")[:180])
            return self._reply(500, "Body is not valid JSON", None)

        if u.path == f"{BASE}/Login/ServiceTokenForDealer":
            return self._reply(*service_token_for_dealer(self.headers, body))

        if u.path == f"{BASE}/AMC/UpdateAMCValidityDates":
            _, err = self._auth(body.get("DEALER_ID"), body.get("BRANCH_ID"), body.get("USER_ID"))
            if err:
                audit("AMC/UpdateAMCValidityDates", "UNAUTHORIZED", dealer=body.get("DEALER_ID"),
                      branch=body.get("BRANCH_ID"), user=body.get("USER_ID"), detail=err)
                return self._reply(401, err, None)
            try:
                return self._reply(*update_amc_validity_dates(body, self.headers.get("Idempotency-Key")))
            except Exception as e:                               # noqa: BLE001
                return self._reply(500, f"Server error: {e}", None)

        if u.path != f"{BASE}/VehicleInvoice/UpdateDiscountValue":
            return self._reply(500, f"No such route: {u.path}", None)

        _, err = self._auth(body.get("DEALER_ID"), body.get("BRANCH_ID"), body.get("USER_ID"))
        if err:
            audit("VehicleInvoice/UpdateDiscountValue", "UNAUTHORIZED", dealer=body.get("DEALER_ID"),
                  branch=body.get("BRANCH_ID"), user=body.get("USER_ID"), detail=err)
            return self._reply(401, err, None)
        try:
            return self._reply(*update_discount_value(body, self.headers.get("Idempotency-Key")))
        except Exception as e:                                   # noqa: BLE001
            return self._reply(500, f"Server error: {e}", None)

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s\n" % (fmt % a))


# Grouped by use case, then the two plumbing tables. The AMC and Job Type tables
# were added with those use cases; leaving them out of this list is what made them
# invisible in the browser even though the endpoints were reading them.
TABLES = ["MDMS_VEHICLE_INVOICE","MDMS_BOOKING_PART","MDMS_MODEL_PART","MDMS_MODEL_SUBSIDY",
          "MDMS_VEHICLE_PRICE_MASTER","MDMS_CUSTOMER","MDMS_DEALER","MDMS_RTO",
          "MDMS_AMC","MDMS_DEALERSHIP",
          "MDMS_JOB_CARD","MDMS_JOB_TYPE","MDMS_MODEL_JOB_TYPE",
          "MDMS_API_AUDIT","MDMS_API_IDEMPOTENCY"]

_CSS = """<style>
 body{font:14px/1.55 -apple-system,Helvetica,Arial;margin:0;background:#f2f4f7;color:#12171e}
 .w{max-width:1500px;margin:0 auto;padding:30px 24px 60px}
 h1{font-size:25px;margin:0 0 4px;letter-spacing:-.4px}
 .sub{color:#68737f;margin:0 0 20px;font-size:14px}
 .tabs{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:20px}
 .tabs a{font:600 12.5px ui-monospace,Menlo,monospace;text-decoration:none;padding:7px 11px;
   border-radius:7px;border:1px solid #d3dae2;background:#fff;color:#39434f}
 .tabs a.on{background:#0b5fc7;border-color:#0b5fc7;color:#fff}
 .tabs a span{color:#8b97a4;font-weight:400}
 .tabs a.on span{color:#cfe0f7}
 form{margin:0 0 18px;display:flex;gap:8px}
 input[name=q]{flex:1;padding:9px 12px;border:1px solid #cfd6de;border-radius:7px;
   font:13px ui-monospace,Menlo,monospace}
 button{padding:9px 16px;border:0;border-radius:7px;background:#0b5fc7;color:#fff;font-weight:600;cursor:pointer}
 .scroll{overflow-x:auto;background:#fff;border:1px solid #d3dae2;border-radius:10px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{text-align:left;padding:7px 12px;border-bottom:1px solid #eef1f5;white-space:nowrap}
 th{background:#e7ecf1;font:600 11px ui-monospace,Menlo,monospace;letter-spacing:.5px;
   text-transform:uppercase;color:#68737f;position:sticky;top:0}
 td{font-family:ui-monospace,Menlo,monospace}
 tr:nth-child(even) td{background:#fafbfc}
 .hit td{background:#fff8e1 !important}
 .err{background:#f8e4e4;color:#9c2a2a;padding:11px 14px;border-radius:8px;margin-bottom:14px}
 .note{color:#68737f;font-size:12.5px;margin-top:10px}
 a.back{color:#0b5fc7;text-decoration:none;font-size:13px}
</style>"""


def db_browser(p):
    table = p.get("t", "MDMS_VEHICLE_INVOICE")
    sql   = (p.get("q") or "").strip()
    limit = int(p.get("limit", "100"))
    counts = {t: q(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in TABLES}

    tabs = "".join(
        f'<a class="{"on" if t == table and not sql else ""}" href="/db?t={t}">{t} <span>{counts[t]}</span></a>'
        for t in TABLES)

    err = ""
    if sql:
        if not sql.lower().lstrip().startswith("select"):
            err = "Read-only — only SELECT is allowed here."
            rows, title = [], "refused"
        else:
            try:
                rows = q(sql); title = "query result"
            except Exception as e:                                  # noqa: BLE001
                err, rows, title = f"SQL error: {e}", [], "error"
    else:
        rows = q(f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT {limit}")
        title = f"{table} — newest {min(limit, counts[table])} of {counts[table]}"

    if rows:
        cols = list(rows[0].keys())
        head = "".join(f"<th>{c}</th>" for c in cols)
        body = ""
        for r in rows:
            hit = "hit" if (str(r.get("INVOICE_NO", "")).strip() == "1152344"
                            or r.get("CRM_REF_CUST_CODE") == "EMR150200007988RF4761") else ""
            body += f'<tr class="{hit}">' + "".join(
                f"<td>{'' if r[c] is None else str(r[c])[:70]}</td>" for c in cols) + "</tr>"
        grid = f'<div class="scroll"><table><tr>{head}</tr>{body}</table></div>'
    else:
        grid = '<div class="scroll"><table><tr><th>no rows</th></tr></table></div>'

    ph = "SELECT * FROM MDMS_VEHICLE_INVOICE WHERE DEALER_ID=13111"
    val = sql.replace('"', "&quot;")
    errhtml = ('<div class=err>' + err + '</div>') if err else ''
    return ("<!doctype html><meta charset=utf-8><title>OnlineDMS — tables</title>" + _CSS +
            "<div class=w><h1>OnlineDMS — the mock database</h1>"
            "<p class=sub>SQLite copy of the Vehicle Invoice, AMC Issues and Job Type Issues slices. "
            "<a class=back href='/'>&larr; API console</a></p>"
            "<div class=tabs>" + tabs + "</div>" + errhtml +
            "<form method=get action='/db'>"
            "<input name=q placeholder=\"" + ph + "\" value=\"" + val + "\">"
            "<button>Run</button></form>"
            "<div class=note><b>" + title + "</b> &nbsp;&middot;&nbsp; read-only, SELECT only "
            "&nbsp;&middot;&nbsp; highlighted rows are the SOP walkthrough</div>" + grid + "</div>")


INDEX_HTML = """<!doctype html><meta charset=utf-8><title>Mock OnlineDMS — TVS Dealer BOT</title>
<style>
 body{font:15px/1.6 -apple-system,Helvetica,Arial;margin:0;background:#f2f4f7;color:#12171e}
 .w{max-width:1000px;margin:0 auto;padding:44px 26px 70px}
 h1{font-size:31px;margin:0 0 6px;letter-spacing:-.5px}
 .sub{color:#68737f;margin:0 0 8px}
 .warn{background:#f8edd8;border-left:4px solid #8a5600;padding:11px 15px;border-radius:0 8px 8px 0;font-size:14px;margin:18px 0 26px}
 .ep{background:#fff;border:1px solid #d3dae2;border-radius:10px;padding:16px 20px;margin-bottom:12px}
 .m{display:inline-block;font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.5px;padding:4px 8px;border-radius:5px;color:#fff;margin-right:9px}
 .get{background:#0f6b47}.post{background:#0b5fc7}
 code{font-family:ui-monospace,Menlo,monospace;font-size:13.5px;background:#eef1f5;padding:2px 6px;border-radius:4px}
 .d{color:#39434f;font-size:14px;margin:8px 0 0}
 a.try{display:inline-block;margin-top:10px;font-size:13.5px;color:#0b5fc7;text-decoration:none;border:1px solid #b9d2f1;background:#e6effa;padding:5px 11px;border-radius:6px}
 h2{font-size:15px;letter-spacing:1.4px;text-transform:uppercase;color:#68737f;margin:30px 0 12px}
 table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d3dae2;border-radius:10px;overflow:hidden;font-size:14px}
 td,th{text-align:left;padding:9px 14px;border-bottom:1px solid #eef1f5}
 th{background:#e7ecf1;font-size:11.5px;letter-spacing:1px;text-transform:uppercase;color:#68737f}
</style>
<div class=w>
<h1>Mock OnlineDMS</h1>
<p class=sub>Stand-in for the TVS DMS backend, built so the API contract can be settled and tested before TVS writes any code. Three use cases are live here: Vehicle Invoice, AMC Issues and Job Type Issues.</p>
<div class=warn><b>Synthetic data.</b> Not a TVS dump. Table and column names are taken verbatim from the Vehicle Invoice SOP; the rows are generated and anchored on the SOP's own sample values.</div>

<h2>Try it</h2>
<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/Login/TokenGeneration</code>
 <p class=d>Mints a dealer JWT. Every other call needs it as <code>Authorization: Bearer &lt;token&gt;</code>.</p>
 <a class=try href="/OnlineSalesAPI/Login/TokenGeneration?DealerID=13111&BranchID=1&UserId=205406">get a token for dealer 13111 &rarr;</a></div>

<div class=ep><span class="m post">POST</span><code>/OnlineSalesAPI/Login/ServiceTokenForDealer</code>
 <p class=d>What DevRev uses. A <b>service credential</b> — headers <code>X-Service-Client</code> / <code>X-Service-Secret</code> — mints a token scoped to <b>one dealer</b>, valid 15 minutes. The credential itself can read nothing.</p></div>

<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/VehicleInvoice/GetInvoiceDiagnostics</code>
 <p class=d>The SOP's whole five-step chain in one call: invoice &rarr; booking &rarr; part &rarr; model &rarr; dealer state &rarr; EMPS subsidy, plus the price-master and RTO checks. Returns <code>BotSummary</code>, a ready-made sentence.</p></div>

<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/VehicleInvoice/GetEmpsSubsidy</code>
 <p class=d>EMPS for a model + state. Eligibility is "an ACTIVE row exists" — never a flat amount, never a hard-coded model list.</p></div>

<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/VehicleInvoice/CheckEmrUsage</code>
 <p class=d>Is this EMR code already used on another invoice? The one check the bot has to escalate for today.</p></div>

<div class=ep><span class="m post">POST</span><code>/OnlineSalesAPI/VehicleInvoice/UpdateDiscountValue</code>
 <p class=d>The only write. Idempotent via <code>Idempotency-Key</code>, and it re-reads the row before answering.</p></div>

<h2>AMC Issues</h2>
<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/AMC/GetAMCDiagnostics</code>
 <p class=d>The AMC entry step and every scenario guard in one call: finds the AMC by number,
 confirms the frame belongs to the same vehicle, reads <code>STATUS</code> (0 Open · 1 Closed ·
 2 Cancelled), and resolves the dealership holding it <b>together with whether that dealership is
 still active</b> &mdash; the single fact that decides all three branches of the close/cancel
 scenario. Returns <code>BotSummary</code>.</p>
 <p class=d>Params: <code>DealerID</code> · <code>BranchID</code> · <code>UserId</code> ·
 <code>AmcNo</code> · <code>FrameNo</code> (optional, but it is what catches an AMC number that
 belongs to a different vehicle).</p></div>

<div class=ep><span class="m post">POST</span><code>/OnlineSalesAPI/AMC/UpdateAMCValidityDates</code>
 <p class=d>The one write in the AMC use case. Sets <code>VALID_FROM</code> to today and
 <code>VALID_TILL</code> to today plus one year. <b>Refuses unless the AMC is Open</b>, snapshots
 before and after into the audit trail, honours <code>Idempotency-Key</code>, and reads the row
 back so the caller never has to trust that the write landed.</p>
 <p class=d>Body: <code>AMC_NO</code> · <code>DEALER_ID</code> · <code>BRANCH_ID</code> ·
 <code>USER_ID</code>. Today this is a raw SQL UPDATE with no REST route on the real DMS.</p></div>

<h2>Job Type Issues</h2>
<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/JobType/GetJobTypeDiagnostics</code>
 <p class=d>Reads the job card for a frame, then the job types enabled for that model with their
 eligibility fields (<code>VALID_KM</code>, <code>GRACE_KM</code>, <code>VALID_DAYS</code>,
 <code>GRACE_DAYS</code>), and names which branch applies: within eligibility, beyond eligibility,
 or a manual-versus-DMS conflict. <code>PopulateJobCardDetailsAngular</code> returns the job type
 but <b>not</b> model-level eligibility, which is why this wrapper exists.</p>
 <p class=d>Params: <code>DealerID</code> · <code>BranchID</code> · <code>UserId</code> ·
 <code>FrameNo</code> · <code>RequestedJobTypeId</code> (31 Paid Service, 12 Running Repair;
 omit to list what is enabled). Also returns <code>OpenJobCardExists</code>, which excludes
 <code>STATUS</code> 3 and 6 exactly as the source does.</p></div>

<div class=ep><span class="m get">GET</span><code>/OnlineSalesAPI/AuditTrail</code>
 <p class=d>Every call, including unauthorised attempts and replays.</p>
 <a class=try href="/OnlineSalesAPI/AuditTrail">see what has called this API &rarr;</a></div>

<h2>Browse the data</h2>
<div class=ep><span class="m get">GET</span><code>/db</code>
 <p class=d>All fifteen tables, row counts, and a read-only SQL box. The SOP walkthrough rows are highlighted.</p>
 <a class=try href="/db">open the database &rarr;</a></div>

<h2>Walk the SOP yourself</h2>
<table>
<tr><th>Value</th><th>What it is</th></tr>
<tr><td><code>13111</code> / <code>1</code></td><td>dealer and branch from the SOP</td></tr>
<tr><td><code>1152344</code> &middot; <code>2026-07-09</code></td><td>the SOP's invoice</td></tr>
<tr><td><code>26307</code></td><td>its booking</td></tr>
<tr><td><code>KE190260DB</code> &rarr; <code>000030000300000029</code></td><td>part &rarr; model (TVS iQube S)</td></tr>
<tr><td><code>KAR</code> &rarr; EMPS <code>5000</code></td><td>dealer state &rarr; subsidy</td></tr>
<tr><td><code>EMR150200007988RF4761</code></td><td>an EMR code deliberately used on TWO invoices</td></tr>
</table>

<h2>Walk the AMC scenarios</h2>
<table>
<tr><th>Value</th><th>What it is</th></tr>
<tr><td><code>AMC700001</code> &middot; <code>MD61311110T1H11100</code></td><td>STATUS 0 &mdash; Open. The only state the validity-date write accepts.</td></tr>
<tr><td><code>AMC700005</code> &middot; <code>MD61311114T1H11104</code></td><td>STATUS 1 &mdash; Closed. Reopen is refused; there is no reopen path in the source.</td></tr>
<tr><td><code>AMC700006</code> &middot; <code>MD61311115T1H11105</code></td><td>STATUS 2 &mdash; Cancelled. A date change against it is refused on status, not on permission.</td></tr>
<tr><td><code>AMC700004</code> &middot; <code>MD61311113T1H11103</code></td><td>held by <code>DS90001</code>, a dealership with <code>ACTIVE = 0</code> &mdash; the inactive-dealership branch.</td></tr>
<tr><td><code>AMC700001</code> + <code>WRONGFRAME999</code></td><td>frame that does not match the AMC &mdash; the guard that catches a mistyped number.</td></tr>
</table>

<h2>Walk the Job Type scenarios</h2>
<table>
<tr><th>Value</th><th>What it is</th></tr>
<tr><td><code>MD61311110T1H11100</code></td><td>6,000 km against a 20,000 km limit &mdash; within eligibility, so a job type that will not list is an L1 ticket.</td></tr>
<tr><td><code>MD61311115T1H11105</code></td><td>51,000 km &mdash; beyond eligibility, which is the answer rather than a ticket.</td></tr>
<tr><td><code>31</code> &rarr; <code>12</code></td><td>Paid Service to Running Repair &mdash; the one permitted change, and it needs ASM approval.</td></tr>
<tr><td><code>9</code> on model <code>000030000300000029</code></td><td>Insurance Claim with <code>ACTIVE = 0</code> &mdash; enabled in the manual, off in the DMS.</td></tr>
<tr><td>STATUS <code>3</code> and <code>6</code></td><td>excluded from &ldquo;an open job card exists&rdquo;, exactly as the source excludes them.</td></tr>
</table>
</div>"""


if __name__ == "__main__":
    print(f"Mock OnlineDMS on http://127.0.0.1:{PORT}{BASE}/")
    print(f"  db: {os.path.abspath(DB)}\n")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
