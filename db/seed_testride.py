#!/usr/bin/env python3
"""Seed the test-ride tables for the voice lead-conversion demo.

Additive only. It creates three new tables and touches nothing the Dealer BOT
already relies on, so the existing audits stay green. Run it as many times as you
like - it drops and rebuilds only its own tables.

    python3 db/seed_testride.py

Three tables:
  MDMS_TEST_RIDE_MODEL  the bikes a customer can ask for, with segment and price
  MDMS_TEST_RIDE_SLOT   per dealer, per day, per model: capacity and what is taken
  MDMS_TEST_RIDE        the bookings themselves
"""
import datetime as dt
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onlinedms.db")

# The line-up a premium lead would actually ask about. The first six already exist
# in MDMS_MODEL_PART and keep their MODEL_IDs so the two sides line up; the premium
# three are new, because the lead-conversion story is about the premium segment and
# the DMS seed had only the RTR 160 from it.
MODELS = [
    # model_id,              description,            segment,     from price, body
    ("000020000200000021", "TVS Apache RTR 310",   "Premium",   242000, "Naked street"),
    ("000020000200000022", "TVS Apache RR 310",    "Premium",   272000, "Full faired"),
    ("000020000200000023", "TVS Ronin",            "Premium",   149000, "Modern retro"),
    ("000020000200000012", "TVS Apache RTR 160",   "Sports",    121000, "Naked street"),
    ("000030000300000031", "TVS iQube ST",         "Premium EV", 158000, "Electric scooter"),
    ("000030000300000045", "TVS X",                "Premium EV", 199000, "Electric crossover"),
    ("000030000300000029", "TVS iQube S",          "EV",        139000, "Electric scooter"),
    ("000020000200000011", "TVS Ntorq 125",        "Sports",     95000, "Scooter"),
    ("000020000200000013", "TVS Jupiter 110",      "Commuter",   78000, "Scooter"),
]

# Showrooms that run test rides, keyed to the dealers already in MDMS_DEALER.
SHOWROOMS = [
    (13111, 1, "Bangalore", "Lakeview Motors, Bangalore",        "100 Feet Road, Indiranagar"),
    (13111, 2, "Bangalore", "Lakeview Motors, Bangalore - Peenya", "Peenya Industrial Area, Phase 2"),
    (22222, 3, "Chennai",   "Kingsway Motors, Chennai",          "Anna Salai, Teynampet"),
    (10587, 1, "Pune",      "Riverside Auto, Pune",              "Baner Road, Baner"),
    (14071, 1, "Cochin",    "Harbour Motors, Cochin",            "MG Road, Ravipuram"),
    (17250, 1, "Mumbai",    "Northgate Wheels, Mumbai",          "Linking Road, Bandra West"),
]

SLOT_TIMES = ["10:00", "11:30", "14:00", "15:30", "17:00"]

# A few slots are deliberately full. A demo where everything is available never shows
# the agent handling "that one is taken, the next is at 11:30" - which is the moment
# that proves the slot is real and not narrated by the model.
PREBOOKED = {
    ("Bangalore", 1, "10:00"), ("Bangalore", 1, "11:30"),
    ("Bangalore", 2, "10:00"),
    ("Mumbai", 1, "10:00"), ("Mumbai", 1, "14:00"),
    ("Chennai", 2, "15:30"),
}


def main():
    c = sqlite3.connect(DB)
    c.executescript("""
        DROP TABLE IF EXISTS MDMS_TEST_RIDE;
        DROP TABLE IF EXISTS MDMS_TEST_RIDE_SLOT;
        DROP TABLE IF EXISTS MDMS_TEST_RIDE_MODEL;

        CREATE TABLE MDMS_TEST_RIDE_MODEL (
            MODEL_ID    TEXT PRIMARY KEY,
            MODEL_DESC  TEXT NOT NULL,
            SEGMENT     TEXT NOT NULL,
            PRICE_FROM  INTEGER NOT NULL,
            BODY_TYPE   TEXT,
            ACTIVE      INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE MDMS_TEST_RIDE_SLOT (
            SLOT_ID    INTEGER PRIMARY KEY AUTOINCREMENT,
            DEALER_ID  INTEGER NOT NULL,
            BRANCH_ID  INTEGER NOT NULL,
            CITY       TEXT NOT NULL,
            SHOWROOM   TEXT NOT NULL,
            ADDRESS    TEXT,
            MODEL_ID   TEXT NOT NULL,
            SLOT_DATE  TEXT NOT NULL,
            SLOT_TIME  TEXT NOT NULL,
            CAPACITY   INTEGER NOT NULL DEFAULT 1,
            BOOKED     INTEGER NOT NULL DEFAULT 0,
            ACTIVE     INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE MDMS_TEST_RIDE (
            TR_ID         INTEGER PRIMARY KEY AUTOINCREMENT,
            TR_NO         TEXT UNIQUE NOT NULL,
            CUSTOMER_NAME TEXT NOT NULL,
            MOBILE_NO     TEXT NOT NULL,
            EMAIL         TEXT,
            MODEL_ID      TEXT NOT NULL,
            MODEL_DESC    TEXT NOT NULL,
            DEALER_ID     INTEGER NOT NULL,
            BRANCH_ID     INTEGER NOT NULL,
            CITY          TEXT NOT NULL,
            SHOWROOM      TEXT NOT NULL,
            SLOT_DATE     TEXT NOT NULL,
            SLOT_TIME     TEXT NOT NULL,
            STATUS        TEXT NOT NULL DEFAULT 'Confirmed',
            SOURCE        TEXT,
            LEAD_REF      TEXT,
            CREATED_AT    TEXT NOT NULL
        );
    """)

    c.executemany("INSERT INTO MDMS_TEST_RIDE_MODEL "
                  "(MODEL_ID, MODEL_DESC, SEGMENT, PRICE_FROM, BODY_TYPE) VALUES (?,?,?,?,?)",
                  MODELS)

    # Fourteen days of slots from today, premium models only - a test ride is offered
    # on the bikes the campaign is pushing, not on the whole catalogue.
    offered = [m for m in MODELS if m[2] in ("Premium", "Premium EV", "Sports")]
    today = dt.date.today()
    rows = []
    for day in range(0, 14):
        d = today + dt.timedelta(days=day)
        if d.weekday() == 1:              # showrooms closed Tuesdays
            continue
        for dealer, branch, city, showroom, addr in SHOWROOMS:
            for model_id, desc, seg, price, body in offered:
                for t in SLOT_TIMES:
                    booked = 1 if (day <= 2 and (city, day, t) in PREBOOKED) else 0
                    rows.append((dealer, branch, city, showroom, addr, model_id,
                                 d.isoformat(), t, 1, booked, 1))
    c.executemany("""INSERT INTO MDMS_TEST_RIDE_SLOT
                     (DEALER_ID, BRANCH_ID, CITY, SHOWROOM, ADDRESS, MODEL_ID,
                      SLOT_DATE, SLOT_TIME, CAPACITY, BOOKED, ACTIVE)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
    c.commit()

    n_model = c.execute("SELECT COUNT(*) FROM MDMS_TEST_RIDE_MODEL").fetchone()[0]
    n_slot = c.execute("SELECT COUNT(*) FROM MDMS_TEST_RIDE_SLOT").fetchone()[0]
    n_free = c.execute("SELECT COUNT(*) FROM MDMS_TEST_RIDE_SLOT WHERE BOOKED < CAPACITY").fetchone()[0]
    print(f"models {n_model}   slots {n_slot}   free {n_free}   taken {n_slot - n_free}")
    print(f"window {today} to {today + dt.timedelta(days=13)}  (Tuesdays closed)")
    print("\npremium line-up offered for test ride:")
    for m in offered:
        print(f"   {m[1]:22} {m[2]:11} from Rs {m[3]:,}")
    c.close()


if __name__ == "__main__":
    main()
