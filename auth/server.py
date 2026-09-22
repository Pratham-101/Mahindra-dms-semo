#!/usr/bin/env python3
"""
Mock TVS MicroDMS — the smallest thing that proves the login handshake.

It does exactly what the real DMS must do, and nothing else:
  1. a dealer "logs in"
  2. the SERVER (never the browser) calls DevRev auth-tokens.create with rev_info
  3. the returned session token is handed to the PluG widget on the page

Run:  python3 server.py          then open http://localhost:8899
The AAT is read from ../../../<scratchpad>/.devrev_aat or the DEVREV_AAT env var.
Nothing is written to disk. The AAT never reaches the browser.
"""
import json, os, sqlite3, urllib.request, urllib.error, html
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

PORT   = int(os.environ.get("AUTH_PORT", "8899"))
APP_ID = os.environ.get("PLUG_APP_ID", "")
AAT    = os.environ.get("DEVREV_AAT", "")

if not AAT:
    for p in [os.path.expanduser("~/.devrev_aat"), ".devrev_aat", "../.devrev_aat"]:
        if os.path.exists(p):
            AAT = open(p).read().strip(); break


def dealer_name(dealer_id, branch_id):
    """The dealership name straight from the DMS, so the header cannot drift from
    the database the bot reads. Falls back to the ID rather than inventing a name."""
    db = os.environ.get("DMS_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "db", "onlinedms.db")
    try:
        con = sqlite3.connect(db)
        row = con.execute("SELECT DEALER_NAME FROM MDMS_DEALER WHERE DEALER_ID=? AND BRANCH_ID=?",
                          (dealer_id, branch_id)).fetchone()
        con.close()
        return row[0] if row else f"Dealer {dealer_id}"
    except Exception:
        return f"Dealer {dealer_id}"


def mint_session_token(dealer_id, branch_id, dms_user_id, display_name, role):
    """THE ONE CALL THAT MATTERS. In production this is your .NET endpoint."""
    body = {
        "rev_info": {
            "user_ref": dms_user_id,          # stable key — same dealer, same record, every session
            "account_ref": dealer_id,         # the dealership, so tickets group by dealer
            "user_traits": {
                "display_name": display_name,
                "custom_fields": {
                    "tnt__dealer_id":   dealer_id,
                    "tnt__branch_id":   branch_id,
                    "tnt__dms_user_id": dms_user_id,
                    "tnt__dealer_role": role,
                },
            },
            "account_traits": {"display_name": f"TVS Dealer {dealer_id}"},
        }
    }
    req = urllib.request.Request(
        "https://api.devrev.ai/auth-tokens.create",
        data=json.dumps(body).encode(),
        headers={"Authorization": AAT, "Content-Type": "application/json"},
    )
    try:
        return json.load(urllib.request.urlopen(req))["access_token"], None
    except urllib.error.HTTPError as e:
        return None, f"{e.code} {e.read().decode()[:400]}"


LOGIN = """<!doctype html><meta charset=utf-8><title>MicroDMS — sign in</title>
<style>
 body{font:15px -apple-system,Helvetica,Arial;background:#eef1f5;margin:0;display:grid;place-items:center;height:100vh}
 form{background:#fff;padding:34px 38px;border-radius:12px;box-shadow:0 10px 40px -20px #0006;width:380px}
 h1{font-size:21px;margin:0 0 4px}p.s{color:#6b7684;margin:0 0 22px;font-size:13.5px}
 label{display:block;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b7684;margin:14px 0 5px;font-weight:600}
 input{width:100%;padding:10px 12px;border:1px solid #cfd6de;border-radius:7px;font-size:15px;box-sizing:border-box}
 button{margin-top:22px;width:100%;padding:12px;border:0;border-radius:8px;background:#0b5fc7;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
 .err{background:#fdeaea;color:#9c2a2a;padding:10px 12px;border-radius:7px;font-size:13px;margin-top:16px;white-space:pre-wrap}
</style>
<form method=post action=/login>
 <h1>MicroDMS</h1><p class=s>Mock sign-in — stands in for the real dealer portal</p>
 <label>Dealer ID</label><input name=dealer_id value="13111">
 <label>Branch ID</label><input name=branch_id value="1">
 <label>DMS user ID</label><input name=dms_user_id value="DLR13111.BILL01">
 <label>Display name</label><input name=display_name value="Billing — Dealer 13111">
 <label>Password</label><input type=password value="anything" >
 <button>Sign in</button>
 __ERR__
 <div style="margin-top:20px;border-top:1px solid #e6eaef;padding-top:16px">
  <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:#6b7684;font-weight:600;margin-bottom:9px">Seeded dealers — click to fill</div>
  <div id="picks"></div>
 </div>
 <p style="font-size:12.5px;color:#6b7684;margin-top:16px;line-height:1.5">
  The <b>DMS user ID</b> is the key DevRev stores the dealer under. Change the Dealer ID and it
  follows automatically — keeping an old user ID against a new dealer would overwrite that
  dealer's record, which is exactly the mistake a real integration must not make.</p>
</form>
<script>
 var DEALERS=[
  {d:"13111",b:"1",u:"DLR13111.BILL01",n:"John Carter",s:"KAR",name:"Lakeview Motors, Bangalore",
   inv:"1152344 / 2026-07-09",note:"the SOP walkthrough \u00b7 CSD \u00b7 no price defined"},
  {d:"13111",b:"2",u:"DLR13111.BILL02",n:"Grace Mercer",s:"KAR",name:"Lakeview Motors \u2014 Peenya",
   inv:"1105225 / 2026-07-13",note:"same dealer, different branch \u00b7 CSD"},
  {d:"22222",b:"3",u:"DLR22222.BILL09",n:"Oliver Shaw",s:"TN",name:"Kingsway Motors, Chennai",
   inv:"1108543 / 2026-06-20",note:"Tamil Nadu \u00b7 EMPS 5000"},
  {d:"10587",b:"1",u:"DLR10587.BILL01",n:"Daniel Brooks",s:"MH",name:"Riverside Auto, Pune",
   inv:"1100577 / 2026-07-21",note:"Maharashtra \u00b7 EMPS 4500"},
  {d:"14071",b:"1",u:"DLR14071.BILL01",n:"Alice Hale",s:"KL",name:"Harbour Motors, Cochin",
   inv:"1100516 / 2026-07-26",note:"Kerala \u00b7 EMPS 5500 \u2014 the highest"},
  {d:"17250",b:"1",u:"DLR17250.BILL01",n:"Thomas Norton",s:"MH",name:"Northgate Wheels, Mumbai",
   inv:"1101451 / 2026-07-16",note:"Mumbai \u00b7 CSD \u00b7 EMPS 4500"}
 ];
 var wrap=document.getElementById('picks');
 DEALERS.forEach(function(x){
   var b=document.createElement('button');
   b.type='button';
   b.style.cssText='display:block;width:100%;text-align:left;margin-bottom:7px;padding:9px 12px;'+
     'border:1px solid #cfd6de;border-radius:7px;background:#f7f9fb;cursor:pointer;font:inherit';
   b.innerHTML='<b style="font-size:13.5px">'+x.d+' / branch '+x.b+'</b>'+
     '<span style="color:#6b7684;font-size:12px"> &nbsp;'+x.s+' &middot; '+x.name+'</span>'+
     '<div style="color:#6b7684;font-size:11.5px;margin-top:2px">invoice '+x.inv+' &middot; '+x.note+'</div>';
   b.onclick=function(){
     document.querySelector('[name=dealer_id]').value=x.d;
     document.querySelector('[name=branch_id]').value=x.b;
     document.querySelector('[name=dms_user_id]').value=x.u;
     document.querySelector('[name=display_name]').value=x.n;
   };
   wrap.appendChild(b);
 });
 var d=document.querySelector('[name=dealer_id]'),
     u=document.querySelector('[name=dms_user_id]'),
     n=document.querySelector('[name=display_name]');
 d.addEventListener('input',function(){
   u.value='DLR'+d.value+'.BILL01';
   n.value='Billing \u2014 Dealer '+d.value;
 });
</script>"""

PAGE = """<!doctype html><meta charset=utf-8><title>MicroDMS — Vehicle Invoice</title>
<style>
 body{font:15px -apple-system,Helvetica,Arial;margin:0;background:#f4f6f9;color:#12171e}
 header{background:#12171e;color:#fff;padding:14px 26px;display:flex;justify-content:space-between;align-items:center}
 header b{font-size:17px}
 .who{font-size:13px;color:#9fb0c4}
 .wrap{padding:30px 26px;max-width:1000px}
 .card{background:#fff;border:1px solid #dde3ea;border-radius:10px;padding:22px 26px;margin-bottom:18px}
 h2{font-size:17px;margin:0 0 14px}
 table{border-collapse:collapse;width:100%;font-size:14px}
 td{padding:7px 0;border-bottom:1px solid #eef1f5}
 td:first-child{color:#6b7684;width:210px}
 .tip{background:#e6effa;border-left:4px solid #0b5fc7;padding:14px 18px;border-radius:0 8px 8px 0;font-size:14px;line-height:1.6}
 code{background:#eef1f5;padding:2px 6px;border-radius:4px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
</style>
<header><b>TVS MicroDMS</b><span class=who>Signed in — __NAME__ · dealer __DEALER__ / branch __BRANCH__</span></header>
<div class=wrap>
 <div class=card><h2>Vehicle Invoice</h2>
  <table>
   <tr><td>Dealer</td><td>__DEALER__</td></tr>
   <tr><td>Branch</td><td>__BRANCH__</td></tr>
   <tr><td>Logged in as</td><td>__USER__</td></tr>
   <tr><td>Invoice no.</td><td>1152344</td></tr>
   <tr><td>Booking no.</td><td>26307</td></tr>
  </table>
 </div>
 <div class=card><h2>What to test</h2>
  <div class=tip>
   Open the chat at the bottom right and ask:<br>
   <b>“what is my dealer id?”</b> &nbsp;or&nbsp; <b>“i am getting price not defined”</b><br><br>
   The bot should already know you are dealer <code>__DEALER__</code>, branch <code>__BRANCH__</code>,
   user <code>__USER__</code> — and should <b>not</b> ask you for them.<br><br>
   Sign in again as a different dealer to confirm it picks up the change.
  </div>
 </div>
</div>
<script>
(function(){var s=document.createElement('script');
 s.src='https://plug-platform.devrev.ai/static/plug.js';s.async=true;
 s.onload=function(){window.plugSDK.init({app_id:'__APPID__', session_token:'__TOKEN__'});};
 document.head.appendChild(s);})();
</script>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(LOGIN.replace("__ERR__", ""))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        f = parse_qs(self.rfile.read(n).decode())
        g = lambda k, d="": html.escape(f.get(k, [d])[0])
        dealer, branch = g("dealer_id", "13111"), g("branch_id", "1")
        user, name = g("dms_user_id"), g("display_name")

        if not AAT:
            return self._send(LOGIN.replace("__ERR__",
                "<div class=err>No AAT found. Set DEVREV_AAT or drop it in .devrev_aat</div>"))
        if not APP_ID:
            return self._send(LOGIN.replace("__ERR__",
                "<div class=err>No PLUG_APP_ID set.</div>"))

        # GUARD — the rule the real .NET endpoint gets for free, because there both values
        # come from one authenticated session and neither is settable from the request.
        # Here the form can disagree, and a mismatch silently repoints one dealer's DevRev
        # record at another dealer. Refuse it loudly instead.
        if dealer not in user:
            return self._send(LOGIN.replace("__ERR__",
                "<div class=err>Refused: DMS user ID <b>" + user + "</b> does not belong to dealer <b>"
                + dealer + "</b>.\n\nDevRev stores the dealer against the user ID, so signing in with "
                "another dealer's user ID would overwrite THAT dealer's record. In the real DMS both "
                "values come from the same login session and cannot disagree.</div>"))

        token, err = mint_session_token(dealer, branch, user, name, "BILLING")
        if err:
            return self._send(LOGIN.replace("__ERR__", f"<div class=err>DevRev rejected it:\n{html.escape(err)}</div>"))

        print(f"  [login] {user} (dealer {dealer}/{branch}) -> session token {len(token)} chars")

        # Prefer the full MicroDMS replica when it is present — same authentication,
        # far more convincing as a demo because it looks like the real screen.
        rich = os.environ.get("PORTAL_HTML") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "portal.html")
        if os.path.exists(rich) and f.get("plain", [""])[0] != "1":
            html_doc = open(rich, encoding="utf-8").read()
            # The identity actually gets substituted now. It used to be behind an
            # `if False`, so the page always rendered dealer 13111 while the PluG
            # session token carried whoever had really signed in — the header and
            # the bot then disagreed about who the dealer was.
            for k, v in (("__SESSION_TOKEN__", token),
                         ("__PLUG_APP_ID__",   APP_ID),
                         ("__DEALER_ID__",     dealer),
                         ("__BRANCH_ID__",     branch),
                         ("__DMS_USER_ID__",   user),
                         ("__DEALER_NAME__",   dealer_name(dealer, branch)),
                         ("__DEALER_ROLE__",   "Billing Executive"),
                         ("__DMS_USER_EMAIL__", user.lower().replace(".", "_") + "@dealer.example.com")):
                html_doc = html_doc.replace(k, v)
            return self._send(html_doc)

        page = (PAGE.replace("__TOKEN__", token).replace("__APPID__", APP_ID)
                    .replace("__DEALER__", dealer).replace("__BRANCH__", branch)
                    .replace("__USER__", user).replace("__NAME__", name))
        self._send(page)

    def log_message(self, *a):  # quieter
        pass


if __name__ == "__main__":
    print(f"AAT   : {'loaded (' + str(len(AAT)) + ' chars)' if AAT else 'MISSING'}")
    print(f"app_id: {APP_ID[:28] + '...' if APP_ID else 'MISSING — set PLUG_APP_ID'}")
    print(f"\n  http://localhost:{PORT}\n")
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
