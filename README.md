# Mahindra DMS — Dealer Support Bot demo

A working replica of a dealer management system with an AI support agent attached, used to
demonstrate ticket deflection for the Vehicle Invoice use case.

A dealer signs in, hits an error mid-task, asks the bot in the corner of the screen, and the bot
reads the DMS and answers — or raises a ticket with the identifiers already filled in. What
normally takes an L1 engineer running SQL by hand takes the dealer a minute.

## What is in here

| | |
|---|---|
| `app.py` | Single public port. Proxies `/OnlineSalesAPI/*` to the DMS API, everything else to the portal. |
| `auth/server.py` | Sign-in page. Mints the DevRev PluG session token **server-side** and serves the portal. |
| `api/server.py` | Mock OnlineDMS: dealer-scoped JWTs, the invoice diagnostics chain, a write with replay protection, and an audit trail. |
| `portal.html` | The MicroDMS screen replica, with the PluG widget embedded. |
| `db/onlinedms.db` | SQLite: 403 invoices, 6 dealers, 83 price rows, 19 subsidy rows. |

No third-party dependencies — Python standard library only.

## Deploying on Railway

Railway detects Python and runs `python3 app.py` (see `railway.json`).

**Set these environment variables** — the deploy will not work without the first two:

| Variable | What it is |
|---|---|
| `DEVREV_AAT` | DevRev application access token. Authorises `auth-tokens.create`. **Never commit this.** |
| `PLUG_APP_ID` | The PluG app id for the widget. |
| `PORT` | Set by Railway automatically. |

`MOCK_DMS_PORT` and `AUTH_PORT` default to 8900/8899 on loopback and do not need setting.

Once deployed you get one URL that serves everything:

```
https://<app>.up.railway.app/                  sign-in, then the MicroDMS portal + bot
https://<app>.up.railway.app/OnlineSalesAPI/AuditTrail   every API call, most recent first
```

### After deploying

The DevRev workflows that call this DMS embed the hostname literally, so both have to be
repointed once at the Railway URL — `workflow-30` (the `LookUpTheDMS` agent skill) and
`workflow-21` (ticket intake). After that the hostname is stable and never needs touching again,
which is the whole reason for moving off the Cloudflare quick tunnel.

## Running it locally

```bash
export DEVREV_AAT="..."      # or drop the token in auth/.devrev_aat
export PLUG_APP_ID="..."
python3 app.py               # http://127.0.0.1:8080
```

## Signing in

Any of the six seeded dealers; the sign-in page lists them as click-to-fill buttons.

| Dealer | Branch | User | State |
|---|---|---|---|
| 13111 | 1 | DLR13111.BILL01 | KAR |
| 13111 | 2 | DLR13111.BILL02 | KAR |
| 22222 | 3 | DLR22222.BILL09 | TN |
| 10587 | 1 | DLR10587.BILL01 | MH |
| 14071 | 1 | DLR14071.BILL01 | KL |
| 17250 | 1 | DLR17250.BILL01 | MH |

The DMS user ID must belong to the dealer ID. The sign-in page refuses a mismatch on purpose:
DevRev stores the dealer against the user ID, so signing in with another dealer's user ID would
overwrite that dealer's record. In a real DMS both values come from one authenticated session and
cannot disagree.

## Demo invoices — dealer 13111 / branch 1

**Subsidy applied at the wrong amount.** The bot names both figures and routes to L1. A data error
a human reviewer would miss, and the strongest thing to show:

| Booking | Invoice | Date | Applied | Entitled |
|---|---|---|---|---|
| 30010 | 1134215 | 2026-07-25 | 4500 | 5000 |
| 30022 | 1101007 | 2026-08-28 | 4000 | 5000 |
| 30031 | 1132859 | 2026-09-11 | 4000 | 5000 |

**Eligible, not yet applied** — the core deflection case: booking `26307` / invoice `1152344` /
`2026-07-09`. This one is also a CSD customer with no price defined, so it demonstrates the SOP
approval path.

**Correctly applied**, so the bot declines to re-apply: booking `26308` / invoice `1152201` /
`2026-06-28`.

**No subsidy** (petrol model), so the bot must invent no figure: invoice `1121142`.

Karnataka carries ₹5,000, Maharashtra ₹4,500, Kerala ₹5,500.

## Notes

The dealer never types a dealer ID or branch ID. Both come from the PluG session token minted at
sign-in, and every DMS call is scoped to them — a 15-minute JWT bound to dealer, branch and user.
Ask for another dealer's invoice and the DMS returns "not found", which is the behaviour you want.

Raising a ticket ends the chat: the conversation becomes the ticket, and support replies inside
it. The bot cannot post to that thread afterwards.
