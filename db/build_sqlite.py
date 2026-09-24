#!/usr/bin/env python3
"""
Builds the local SQLite copy of the OnlineDMS Vehicle Invoice slice.

Why SQLite: it runs with no install so the mock is one command to start.
The authoritative DDL is db/01_schema.sql (T-SQL, SQL Server) — that is what
TVS should read. This script mirrors the same tables and columns.

The data is SYNTHETIC. It is not a TVS dump. It is anchored on the exact
sample values in the Vehicle Invoice SOP so a walkthrough matches the document,
and then padded with ~3 months of generated invoices so queries behave like
they would against a real table rather than a 6-row toy.

Deterministic: same seed, same database, every time.
"""
import os, random, sqlite3, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "onlinedms.db")
random.seed(20260911)

DDL = """
CREATE TABLE MDMS_DEALER (
  DEALER_ID INTEGER NOT NULL, BRANCH_ID INTEGER NOT NULL, STATE_ID TEXT NOT NULL,
  DEALER_NAME TEXT, ACTIVE INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (DEALER_ID, BRANCH_ID));
CREATE TABLE MDMS_CUSTOMER (
  CUSTOMER_ID INTEGER PRIMARY KEY, CUSTOMER_NAME TEXT, CUSTOMER_TYPE TEXT NOT NULL,
  MOBILE_NO TEXT, DEALER_ID INTEGER NOT NULL, BRANCH_ID INTEGER NOT NULL);
CREATE TABLE MDMS_MODEL_PART (
  PART_ID TEXT PRIMARY KEY, MODEL_ID TEXT NOT NULL, MODEL_DESC TEXT);
CREATE TABLE MDMS_BOOKING_PART (
  BOOKING_ID INTEGER NOT NULL, DEALER_ID INTEGER NOT NULL, BRANCH_ID INTEGER NOT NULL,
  PART_ID TEXT NOT NULL, PRIMARY KEY (BOOKING_ID, DEALER_ID, BRANCH_ID));
CREATE TABLE MDMS_MODEL_SUBSIDY (
  MODEL_ID TEXT NOT NULL, STATE_ID TEXT NOT NULL, EMPS REAL NOT NULL,
  ACTIVE INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (MODEL_ID, STATE_ID));
CREATE TABLE MDMS_RTO (
  RTO_ID INTEGER PRIMARY KEY, RTO_CODE TEXT NOT NULL, RTO_NAME TEXT,
  STATE_ID TEXT NOT NULL, ACTIVE INTEGER NOT NULL DEFAULT 1);
CREATE TABLE MDMS_VEHICLE_PRICE_MASTER (
  MODEL_ID TEXT NOT NULL, STATE_ID TEXT NOT NULL, CUSTOMER_TYPE TEXT NOT NULL,
  PRICE REAL NOT NULL, ACTIVE INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (MODEL_ID, STATE_ID, CUSTOMER_TYPE));
CREATE TABLE MDMS_VEHICLE_INVOICE (
  VEH_INVOICE_ID INTEGER PRIMARY KEY, DEALER_ID INTEGER NOT NULL, BRANCH_ID INTEGER NOT NULL,
  INVOICE_NO TEXT NOT NULL, INVOICE_DATE TEXT NOT NULL, BOOKING_ID INTEGER,
  CUSTOMER_ID INTEGER, DISC_VALUE REAL, CRM_REF_CUST_CODE TEXT, RTO_ID INTEGER,
  GST_TYPE TEXT, INVOICE_STATUS INTEGER, ACTIVE INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IX_VEH_INVOICE_LOOKUP ON MDMS_VEHICLE_INVOICE (DEALER_ID, INVOICE_NO, INVOICE_DATE);
CREATE INDEX IX_VEH_INVOICE_EMR ON MDMS_VEHICLE_INVOICE (CRM_REF_CUST_CODE);
CREATE TABLE MDMS_API_AUDIT (
  AUDIT_ID INTEGER PRIMARY KEY AUTOINCREMENT, CALLED_AT TEXT NOT NULL,
  CLIENT_ID TEXT, DEALER_ID INTEGER, BRANCH_ID INTEGER, USER_ID INTEGER,
  ENDPOINT TEXT NOT NULL, IDEMPOTENCY_KEY TEXT, OUTCOME TEXT NOT NULL, DETAIL TEXT);
CREATE TABLE MDMS_API_IDEMPOTENCY (
  IDEMPOTENCY_KEY TEXT PRIMARY KEY, ENDPOINT TEXT NOT NULL, DEALER_ID INTEGER NOT NULL,
  REQUEST_HASH TEXT NOT NULL, RESPONSE_JSON TEXT NOT NULL, CREATED_AT TEXT NOT NULL);

CREATE TABLE MDMS_DEALERSHIP (
  DEALERSHIP_CODE TEXT PRIMARY KEY, DEALER_CODE TEXT, DEALERSHIP_NAME TEXT, ACTIVE INTEGER);
CREATE TABLE MDMS_AMC (
  AMC_ID INTEGER PRIMARY KEY, AMC_NO TEXT, FRAME_NO TEXT, DEALER_ID INTEGER,
  BRANCH_ID INTEGER, DEALERSHIP_CODE TEXT, CUSTOMER_ID INTEGER, STATUS INTEGER,
  VALID_FROM TEXT, VALID_TILL TEXT, ACTIVE INTEGER);
CREATE TABLE MDMS_JOB_TYPE (
  JOB_TYPE_ID INTEGER PRIMARY KEY, JOB_TYPE_DESC TEXT);
CREATE TABLE MDMS_MODEL_JOB_TYPE (
  MODEL_ID TEXT, JOB_TYPE_ID INTEGER, VALID_KM INTEGER, GRACE_KM INTEGER,
  VALID_DAYS INTEGER, GRACE_DAYS INTEGER, ACTIVE INTEGER);
CREATE TABLE MDMS_JOB_CARD (
  JC_ID INTEGER PRIMARY KEY, JC_NO TEXT, FRAME_NO TEXT, DEALER_ID INTEGER,
  BRANCH_ID INTEGER, MODEL_ID TEXT, JOB_TYPE_ID INTEGER, CURRENT_KM INTEGER,
  SALE_DATE TEXT, STATUS INTEGER, ACTIVE INTEGER);
"""

# ── anchors taken verbatim from the SOP ──────────────────────────────────────
SOP_DEALER, SOP_BRANCH = 13111, 1
SOP_STATE   = "KAR"
SOP_INVOICE = "1152344"
SOP_DATE    = "2026-07-09"          # SOP wrote 09/07/2026 (dd/mm/yyyy)
SOP_BOOKING = 26307
SOP_PART    = "KE190260DB"
SOP_MODEL   = "000030000300000029"
SOP_EMR     = "EMR150200007988RF4761"

STATES  = ["KAR","TN","MH","KL","AP","TS","GJ"]
# Neutral, non-regional trading names on purpose — the demo should not look like it is
# pointing at any real dealership. Cities and states are kept, because the state is what
# drives the subsidy amount and that difference is the point of the demo.
DEALERS = [(13111,1,"KAR","Lakeview Motors, Bangalore"),
           (13111,2,"KAR","Lakeview Motors, Bangalore - Peenya"),
           (22222,3,"TN","Kingsway Motors, Chennai"),
           (10587,1,"MH","Riverside Auto, Pune"),
           (14071,1,"KL","Harbour Motors, Cochin"),
           (17250,1,"MH","Northgate Wheels, Mumbai")]
MODELS  = [(SOP_MODEL,"KE190260DB","TVS iQube S"), ("000030000300000031","KE242080","TVS iQube ST"),
           ("000030000300000045","KE300120","TVS X"),  ("000020000200000011","KE165400","TVS Ntorq 125"),
           ("000020000200000012","KE320120","TVS Apache RTR 160"), ("000020000200000013","KE190455","TVS Jupiter 110")]
EV_MODELS = {m[0] for m in MODELS[:3]}     # only the EVs carry an EMPS row

FIRST = ["John","Daniel","Emma","Oliver","Grace","Henry","Alice","Thomas","Clara","Edward",
         "Sophie","George","Ruth","Martin","Helen","Peter","Anna","Charles","Laura","Simon"]
LAST  = ["Carter","Brooks","Shaw","Mercer","Hale","Bennett","Whitfield","Doyle","Ashcroft",
         "Radcliffe","Norton","Prescott","Langley","Harrow","Vaughan","Sinclair"]

def main():
    if os.path.exists(DB): os.remove(DB)
    cx = sqlite3.connect(DB); cu = cx.cursor()
    cu.executescript(DDL)

    cu.executemany("INSERT INTO MDMS_DEALER VALUES (?,?,?,?,1)", DEALERS)
    cu.executemany("INSERT INTO MDMS_MODEL_PART VALUES (?,?,?)",
                   [(p, m, d) for m, p, d in MODELS])

    # RTO master — one live row per state, plus a deliberately INACTIVE one that
    # reproduces Scenario 7 (the fk_rto_book conflict).
    rtos = [(100+i, f"{st}-01", f"{st} Central RTO", st, 1) for i, st in enumerate(STATES)]
    rtos.append((999, "KAR-99", "KAR Decommissioned RTO", "KAR", 0))
    cu.executemany("INSERT INTO MDMS_RTO VALUES (?,?,?,?,?)", rtos)

    # EMPS subsidy — per model AND state, never a flat figure.
    subs = []
    for mid in EV_MODELS:
        for st in STATES:
            if st == "GJ":            # deliberately missing -> "contact your EV Team"
                continue
            amt = {"KAR": 5000, "TN": 5000, "MH": 4500, "KL": 5500, "AP": 4000, "TS": 4000}[st]
            subs.append((mid, st, float(amt), 1))
    subs.append((MODELS[3][0], "KAR", 0.0, 0))     # petrol model, row exists but INACTIVE
    cu.executemany("INSERT INTO MDMS_MODEL_SUBSIDY VALUES (?,?,?,?)", subs)

    # Price master — CSD price deliberately ABSENT for the SOP model in KAR,
    # which is exactly what makes Scenario 6 fire.
    prices = []
    for mid, _, _ in MODELS:
        for st in STATES:
            prices.append((mid, st, "INDIVIDUAL", round(random.uniform(85000, 195000), 2), 1))
            if not (mid == SOP_MODEL and st == "KAR"):
                prices.append((mid, st, "CSD", round(random.uniform(80000, 185000), 2), 1))
    cu.executemany("INSERT INTO MDMS_VEHICLE_PRICE_MASTER VALUES (?,?,?,?,?)", prices)

    # ── AMC Issues + Job Type Issues ────────────────────────────────────────
    # Seeded so every documented branch has a row that reaches it. The branches
    # are the point: an AMC that is not Open cannot have its dates changed, and
    # Scenario 4 turns entirely on whether the holding dealership is active.
    dships = []
    for i, (d, b, st, nm) in enumerate(DEALERS):
        dships.append((f"DS{d}{b}", f"DC{d}", nm, 1))
    dships.append(("DS90001", "DC90001", "Eastway Motors, Nagpur (closed down)", 0))
    cu.executemany("INSERT INTO MDMS_DEALERSHIP VALUES (?,?,?,?)", dships)

    JOB_TYPES = [(31, "Paid Service"), (12, "Running Repair"), (1, "Free Service"),
                 (7, "PDI"), (9, "Insurance Claim"), (14, "Accidental Repair")]
    cu.executemany("INSERT INTO MDMS_JOB_TYPE VALUES (?,?)", JOB_TYPES)

    # Model-level eligibility. PDI and insurance are deliberately NOT enabled on
    # every model, which is what makes "not listing" a real answer rather than a bug.
    mjt = []
    for mid, _, _ in MODELS:
        for jt, vkm, gkm, vd, gd in ((31, 20000, 2000, 365, 30),
                                     (12, 100000, 5000, 1095, 60),
                                     (1, 5000, 500, 180, 15),
                                     (14, 100000, 5000, 1095, 60)):
            mjt.append((mid, jt, vkm, gkm, vd, gd, 1))
        # one model carries an INACTIVE row: enabled in the manual, off in the DMS
        if mid == SOP_MODEL:
            mjt.append((mid, 9, 100000, 5000, 1095, 60, 0))
    cu.executemany("INSERT INTO MDMS_MODEL_JOB_TYPE VALUES (?,?,?,?,?,?,?)", mjt)

    amcs, jcs = [], []
    aid, jid = 700001, 800001
    for n, (d, b, st, nm) in enumerate(DEALERS):
        for k in range(6):
            frame = f"MD6{d}{b}{k}T1H{11100+k}"
            mid = MODELS[k % len(MODELS)][0]
            # statuses: mostly Open, some Closed and Cancelled so the guard fires
            status = 0 if k < 4 else (1 if k == 4 else 2)
            # one AMC per dealer sits under the closed-down dealership
            dscode = "DS90001" if k == 3 else f"DS{d}{b}"
            vf = dt.date(2026, 1, 10) + dt.timedelta(days=k * 11)
            amcs.append((aid, f"AMC{aid}", frame, d, b, dscode, 500001 + n * 10 + k,
                         status, str(vf), str(vf + dt.timedelta(days=365)), 1))
            # job card: statuses 3 and 6 are the ones the source excludes
            jstatus = (3 if k == 0 else 6 if k == 1 else 1)
            km = 6000 + k * 9000          # k>=2 crosses the Paid Service 20000+2000 limit
            jcs.append((jid, f"JC{jid}", frame, d, b, mid, 31 if k % 2 == 0 else 12,
                        km, str(dt.date(2026, 3, 1) + dt.timedelta(days=k * 40)), jstatus, 1))
            aid += 1; jid += 1
    cu.executemany("INSERT INTO MDMS_AMC VALUES (?,?,?,?,?,?,?,?,?,?,?)", amcs)
    cu.executemany("INSERT INTO MDMS_JOB_CARD VALUES (?,?,?,?,?,?,?,?,?,?,?)", jcs)


    # ── customers, bookings, invoices: ~3 months ────────────────────────────
    custs, bookings, invs = [], [], []
    cid, bid, vid = 500001, 30001, 900001
    start = dt.date(2026, 6, 15)

    def add_invoice(dealer, branch, state, inv_no, date, booking, customer,
                    disc, emr, rto, gst, status=1):
        nonlocal vid
        invs.append((vid, dealer, branch, inv_no, f"{date} 00:00:00", booking,
                     customer, disc, emr, rto, gst, status, 1)); vid += 1

    for d, b, st, _name in DEALERS:
        for _ in range(random.randint(55, 80)):
            mid, part, _ = random.choice(MODELS)
            ctype = random.choices(["INDIVIDUAL","CSD","CORPORATE"], [0.78,0.14,0.08])[0]
            custs.append((cid, f"{random.choice(FIRST)} {random.choice(LAST)}", ctype,
                      f"9{random.randint(100000000,999999999)}", d, b))
            bookings.append((bid, d, b, part)); 
            day = start + dt.timedelta(days=random.randint(0, 88))
            emr = f"EMR{random.randint(10**17,10**18-1)}RF{random.randint(1000,9999)}" if random.random() < .35 else None
            disc = float(random.choice([0, 0, 0, 4000, 4500, 5000])) if mid in EV_MODELS else 0.0
            add_invoice(d, b, st, str(random.randint(1100000, 1199999)), day.isoformat(),
                        bid, cid, disc, emr, 100+STATES.index(st), random.choice(["CGST_SGST","CGST_SGST","IGST"]))
            cid += 1; bid += 1

    # ── the SOP's own walkthrough row, exact values ────────────────────────
    custs.append((SOP_DEALER*10, "John Carter", "CSD", "9880012345", SOP_DEALER, SOP_BRANCH))
    bookings.append((SOP_BOOKING, SOP_DEALER, SOP_BRANCH, SOP_PART))
    add_invoice(SOP_DEALER, SOP_BRANCH, SOP_STATE, SOP_INVOICE, SOP_DATE, SOP_BOOKING,
                SOP_DEALER*10, None, SOP_EMR, 100, "CGST_SGST")          # DISC_VALUE NULL -> PM E-Drive missing

    # a second invoice already carrying the SAME EMR code -> "code used elsewhere"
    custs.append((SOP_DEALER*10+1, "Emma Shaw", "INDIVIDUAL", "9880067890", SOP_DEALER, SOP_BRANCH))
    bookings.append((SOP_BOOKING+1, SOP_DEALER, SOP_BRANCH, SOP_PART))
    add_invoice(SOP_DEALER, SOP_BRANCH, SOP_STATE, "1152201", "2026-06-28", SOP_BOOKING+1,
                SOP_DEALER*10+1, 5000.0, SOP_EMR, 100, "CGST_SGST")

    # an invoice pointing at the DECOMMISSIONED RTO -> reproduces fk_rto_book
    custs.append((SOP_DEALER*10+2, "Henry Bennett", "INDIVIDUAL", "9880099999", SOP_DEALER, SOP_BRANCH))
    bookings.append((SOP_BOOKING+2, SOP_DEALER, SOP_BRANCH, SOP_PART))
    add_invoice(SOP_DEALER, SOP_BRANCH, SOP_STATE, "1152350", "2026-09-02", SOP_BOOKING+2,
                SOP_DEALER*10+2, 0.0, None, 999, "IGST")

    cu.executemany("INSERT INTO MDMS_CUSTOMER VALUES (?,?,?,?,?,?)", custs)
    cu.executemany("INSERT INTO MDMS_BOOKING_PART VALUES (?,?,?,?)", bookings)
    cu.executemany("INSERT INTO MDMS_VEHICLE_INVOICE VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", invs)
    cx.commit()

    print(f"built {DB}")
    for t in ("MDMS_DEALER","MDMS_CUSTOMER","MDMS_MODEL_PART","MDMS_BOOKING_PART",
              "MDMS_MODEL_SUBSIDY","MDMS_RTO","MDMS_VEHICLE_PRICE_MASTER","MDMS_VEHICLE_INVOICE",
              "MDMS_API_AUDIT","MDMS_API_IDEMPOTENCY"):
        print(f"  {t:28s} {cu.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>5} rows")
    cx.close()

if __name__ == "__main__":
    main()
