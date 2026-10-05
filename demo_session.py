#!/usr/bin/env python3
"""Demo session: the real Mail Triage app on a fake mailbox, for screenshots.

Boots the app against an isolated data dir (default ./demo-data) and a fake IMAP
server that speaks the real protocol. The mailbox is pre-populated with a
realistic inbox; the worker scans it, rules sort it, and the configured **real**
LLM classifies/frames the rest (the assistant is fully live - it reads the demo
mail through the fake IMAP). Nothing here touches the live mailbox or data/.

Usage:
    .venv/bin/python demo_session.py                     # boot (seeds on first run)
    .venv/bin/python demo_session.py --reset             # wipe demo data and reseed
    .venv/bin/python demo_session.py --skip-llm-seed     # don't pre-classify
    .venv/bin/python demo_session.py --host 100.93.139.49 --port 8098   # tailnet
    .venv/bin/python demo_session.py --port 8098 --imap-port 41993

The LLM endpoint is read from the main checkout's .env (LLM_BASE_URL, LLM_API_KEY,
LLM_MODEL, ...) unless those variables are already in the environment. Everything
else (DATA_DIR, IMAP_*) is forced to the demo instance.
"""
import argparse
import base64
import configparser
import email.utils
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "demo-session.json"
DEMO_EMAIL = "alex.morgan@example.com"
DEMO_NAME = "Alex Morgan"
DEMO_PASSWORD = "ep-demo-local-pw"

# The demo mailbox. Ages are hours before "now"; order here is UID order.
# kind=html renders text/html so the viewer's HTML path gets exercised.
DEMO_MAIL = [
    dict(age=2, frm="Sarah Chen <sarah.chen@northwind.io>",
         subj="Q4 budget review — need your numbers by Thursday",
         body="""Hi Alex,

I pulled together the first pass of the Q4 budget deck. Could you add your
headcount and infra numbers before Thursday's review? I left the rows blank in
the shared sheet.

No need for a full write-up — the spreadsheet plus two or three bullets on the
biggest deltas is enough.

Thanks,
Sarah"""),
    dict(age=3, frm="GitHub <notifications@github.com>",
         subj="[northwind/api] PR #482: add rate-limit middleware",
         body="""sarah-chen opened pull request #482: add rate-limit middleware.

  +412 −37  ·  6 files changed
  Review requested from alex-morgan

Body: "Adds a token-bucket limiter in front of the public API. Defaults to 120
req/min per key; configurable via RATE_LIMIT_PER_MIN. Needs a second pair of
eyes on the Redis failure path."

View it on GitHub: https://github.com/northwind/api/pull/482""",
         flags=["\\Seen"]),
    dict(age=4, frm="Stripe <receipts@stripe.com>",
         subj="Your invoice INV-2026-1043 is available",
         body="""Your invoice INV-2026-1043 for September is ready.

Amount due:   $482.00
Due date:     Oct 18, 2026
Billing to:   Northwind Labs, alex.morgan@example.com

The PDF is attached to this receipt in the billing portal. Payments are
collected automatically from the card ending in 4242.""",
         flags=["\\Seen"]),
    dict(age=5, frm="Maya Patel <maya.patel@brightloop.dev>",
         subj="Dinner Saturday?",
         body="""Hey! A few of us are doing dinner Saturday around 7 — that new
ramen place on 5th. Are you in? I'll book if you can make it.

Maya"""),
    dict(age=7, frm="Lakeside Dental <frontdesk@lakesidedental.example>",
         subj="Appointment reminder — Tue Oct 6, 14:30",
         body="""This is a reminder of your upcoming appointment:

  Tuesday, October 6 at 14:30
  Lakeside Dental, 221 Harbour Street
  Provider: Dr. Alvarez

Reply to reschedule. Please arrive ten minutes early.""",
         flags=["\\Seen"]),
    dict(age=9, frm="CloudScale Billing <billing@cloudscale.io>",
         subj="Invoice #CS-88410 — payment received",
         body="""Thanks for your payment. Invoice #CS-88410 was paid in full.

Service:    Compute — 4x dedicated vCPU
Period:     Sep 1 – Sep 30, 2026
Total:      $1,240.00

You can download past invoices from the billing console.""",
         flags=["\\Seen"]),
    dict(age=11, frm="Northwind Calendar <calendar@northwind.io>",
         subj="Invitation: Sprint planning — Thursday 10:00",
         body="""You have been invited to:

  Sprint planning
  Thursday, October 8 · 10:00–11:30
  Meeting room: Fjord (3rd floor)

Agenda: carry-over review, capacity, and scope for the rate-limit work.
Accept or decline from the calendar invite.""",
         flags=["\\Seen"]),
    dict(age=13, frm="Medium Daily Digest <noreply@medium.com>",
         subj="The quiet rise of local-first software",
         body="""Today's highlights for Alex:

· The quiet rise of local-first software — why sync engines are back
· A field guide to SQLite in production
· What we learned rewriting our editor in Rust (again)

Read time: ~18 min. You are receiving this because you follow Software Craft.""",
         html=True,
         body_html="""<div style="font-family:Georgia,serif;max-width:560px">
<h2>The quiet rise of local-first software</h2>
<p>Why sync engines are back, and what changed since last time.</p>
<ul><li><a href="https://example.com/local-first">The quiet rise of local-first software</a></li>
<li><a href="https://example.com/sqlite-prod">A field guide to SQLite in production</a></li></ul>
<p style="color:#777;font-size:12px">Medium Daily Digest · <a href="https://example.com/unsub">Unsubscribe</a></p>
</div>"""),
    dict(age=26, frm="Priya Raghavan <priya@northwind.io>",
         subj="Re: API latency budget — notes from today",
         body="""Sharing the notes before I forget:

- p50 is fine (41ms), but p99 keeps drifting up under load.
- The limiter middleware should help; let's measure after it lands.
- I still owe you the trace from Tuesday's spike.

Can you sanity-check the budget table before the review? I'd rather we argue
about the numbers once, together.""",
         flags=["\\Seen"]),
    dict(age=28, frm="npm <support@npmjs.com>",
         subj="Your package @northwind/logger was published",
         body="""Hi alex-morgan,

Version 2.3.1 of @northwind/logger was published successfully.

  tarball: 18.4 kB
  integrity: sha512-9f2c…
  dist-tag: latest

This is an automated notification; replies are not monitored.""",
         flags=["\\Seen"]),
    dict(age=30, frm="Dad <dad@family-mail.example>",
         subj="Photos from the lake trip",
         body="""Finally sorted through the photos from the lake. Uploaded the good
ones to the shared album — the sunrise ones came out surprisingly well.

Call when you have a minute. Mom says hi.

— Dad"""),
    dict(age=33, frm="Acme Tools Support <support@acme-tools.io>",
         subj="Ticket #5512 has been updated",
         body="""Your support ticket has a new reply:

  Status:   in progress
  Subject:  Export fails for mailboxes over 2 GB

"Thanks for the sample export. We can reproduce the failure — the writer runs
out of memory while building the index. A fix is scheduled for the next patch
release. We'll follow up here when it ships."

View the full thread in the support portal.""",
         flags=["\\Seen"]),
    dict(age=36, frm="Frontend Focus <digest@frontendfoc.us>",
         subj="Issue #712: container queries in practice",
         body="""This week: container queries in practice, the state of CSS 2026, and
a deep dive on view transitions.

· Container queries in practice — patterns that survived contact with prod
· The state of CSS 2026 — survey results
· View transitions beyond the demo

Read online · Manage preferences · Unsubscribe""",
         html=True,
         body_html="""<div style="font-family:system-ui;max-width:600px">
<h2>Issue #712</h2>
<p><b>Container queries in practice</b> — patterns that survived contact with production.</p>
<p><b>The state of CSS 2026</b> — survey results are in.</p>
<p><b>View transitions beyond the demo</b> — what actually ships.</p>
<p style="color:#888;font-size:12px"><a href="https://example.com/unsub">Unsubscribe</a> · Frontend Focus</p>
</div>"""),
    dict(age=40, frm="Meridian Bank <alerts@meridianbank.example>",
         subj="Your eStatement for September is ready",
         body="""Your September eStatement is now available in online banking.

  Account:  ···· 8841
  Closing balance:  $8,214.52
  Statement period: Sep 1 – Sep 30

Sign in to view or download the PDF. To stop paper statements, update your
preferences under Documents.

For your security we never include account numbers in full by email.""",
         flags=["\\Seen"]),
    dict(age=44, frm="Spotify <no-reply@spotify.com>",
         subj="Your Premium receipt — October",
         body="""Thanks for being Premium.

  Plan:       Individual
  Amount:     $11.99
  Billing date: Oct 1, 2026
  Payment:    Visa ···· 4242

Your receipt is attached as a PDF in the account page. Manage your plan at any
time under Account.""",
         flags=["\\Seen"]),
    dict(age=48, frm="LinkedIn <notifications@linkedin.com>",
         subj="You appeared in 9 searches this week",
         body="""You were found by more people this week.

  Search appearances: 9
  Profile views:      14
  Recruiter views:    1

See who's looking at your profile (and turn this off) in your notifications
settings.""",
         flags=["\\Seen"]),
    dict(age=52, frm="Nadia Rahman <nadia.rahman@university.example>",
         subj="Guest lecture request — March cohort",
         body="""Dear Alex,

Would you be open to giving a guest lecture to our distributed-systems cohort
in March? Last year's session on pragmatic consistency was the students'
favourite, so I'm hoping you can do a follow-up.

One hour, remote is fine, and I can send the topic outline in advance.

Best regards,
Nadia Rahman"""),
    dict(age=56, frm="Airbnb <automated@airbnb.com>",
         subj="Your Kyoto trip is confirmed",
         body="""Your reservation is confirmed.

  Check-in:   Thu, Nov 12 · after 15:00
  Check-out:  Mon, Nov 16 · before 11:00
  Listing:    Machiya near Gion (2 guests)
  Total:      ¥86,400

The host's check-in instructions will appear 48 hours before arrival.""",
         flags=["\\Seen"]),
    dict(age=60, frm="Harbourview Properties <office@harbourview.properties.example>",
         subj="Lease renewal — please confirm by Friday",
         body="""Hi Alex,

Your lease is up for renewal. The proposed terms for the next 12 months:

  Rent:     $2,340/month (up 3.1%)
  Term:     Dec 1, 2026 – Nov 30, 2027

If these work, reply with a yes and we'll send the renewal for signature.
If you'd like to discuss, call the office before Friday.

— Harbourview"""),
    dict(age=64, frm="Rust Weekly <newsletter@rustweekly.example>",
         subj="This week in Rust #562",
         body="""Hello from Rust Weekly!

· Crate of the week: loom gets a major release
· Inside the new borrow-checker diagnostics
· An evening of practical async: three patterns we reach for

Read the full issue online. You can unsubscribe at the bottom of this mail.""",
         html=True,
         body_html="""<div style="font-family:Menlo,monospace;max-width:600px">
<h2>This week in Rust #562</h2>
<ul>
<li>Crate of the week: <b>loom</b> gets a major release</li>
<li>Inside the new borrow-checker diagnostics</li>
<li>Practical async: three patterns we reach for</li>
</ul>
<p style="font-size:12px"><a href="https://example.com/unsub">unsubscribe</a></p>
</div>"""),
    dict(age=68, frm="USPS Informed Delivery <tracking@usps.com>",
         subj="Your package is out for delivery",
         body="""Your package is out for delivery today.

  Tracking: 9400 1112 0000 0000 0000
  Expected: today by 20:00

You can follow the carrier on the tracking page. No action needed.""",
         flags=["\\Seen"]),
    dict(age=72, frm="Quantile Search <talent@quantile-search.example>",
         subj="Senior backend role — remote, €120–150k",
         body="""Hi Alex,

I came across your work on local-first tooling and thought of a role we're
hiring for: backend lead at a data-infrastructure startup, fully remote (EU
timezones), €120–150k plus equity.

Not a fit? Just say so and I won't follow up again.

Best,
Quantile Search"""),
    dict(age=76, frm="Patreon <no-reply@patreon.com>",
         subj="Your monthly membership receipt",
         body="""Thanks! Your membership was renewed.

  Creator:  A Small Studio
  Tier:     Supporter
  Amount:   $5.00 / month
  Receipt:  #PT-99213

You can manage or pause your membership anytime.""",
         flags=["\\Seen"]),
    dict(age=80, frm="Spotify Deals <promo@spotify.example>",
         subj="3 months of Premium for ¥0",
         body="""Come back this month and your first three months are free.

  · No ads, offline listening, lossless audio
  · Cancel anytime
  · Offer ends Oct 15

Terms apply. You received this marketing email because you opted in.""",
         flags=["\\Seen"]),
    dict(age=84, frm="Northwind People Ops <hr@northwind.io>",
         subj="Open enrolment closes Friday — action required",
         body="""Reminder: open enrolment for next year's health and dental plans
closes this Friday.

  → Confirm or change your plan in the benefits portal
  → Review your dependants and beneficiaries
  → FSA elections do not roll over — re-elect if you want them

If you do nothing, your current coverage auto-renews. Questions? Reply here.""",
         flags=["\\Seen"]),
    dict(age=88, frm="Google Calendar <calendar-notification@google.com>",
         subj="Reminder: Dentist appointment tomorrow at 9:00",
         body="""Reminder: Dentist — tomorrow at 9:00.

  Location: Lakeside Dental, 221 Harbour Street
  Calendar: Personal

View the event in Google Calendar. This notification cannot be replied to.""",
         flags=["\\Seen"]),
    dict(age=92, frm="Amazon.com <auto-confirm@amazon.com>",
         subj="Order confirmation for #112-4471023-5588",
         body="""Thanks for your order.

  USB-C dock, 11-in-1  ·  $89.99
  Estimated delivery: Oct 5–6

Track your package from Your Orders. This address cannot receive replies.""",
         flags=["\\Seen"]),
    dict(age=96, frm="The Marginalian <newsletter@themarginalian.example>",
         subj="How to live with uncertainty",
         body="""This week: how to live with uncertainty, the psychology of the
infinite scroll, and a poem for October.

Read the full letter online. Unsubscribe from the footer if this no longer
finds you well.""",
         flags=["\\Seen"]),
    dict(age=100, frm="Vercel <notifications@vercel.com>",
         subj="Deployment ready: northwind-api-8f2c",
         body="""Your deployment is ready.

  Project:  northwind-api
  Branch:   main
  Status:   ● Ready
  Build:    1m 42s

View logs and promote to production in the dashboard.""",
         flags=["\\Seen"]),
    dict(age=104, frm="Mom <mom@family-mail.example>",
         subj="Call me when you're free?",
         body="""Nothing urgent — I just want to hear how the week went. Sunday
afternoon? Dad will be out in the garden.

Love,
Mom"""),
    dict(age=108, frm="Figma <updates@figma.com>",
         subj="New comment on 'Dashboard v3'",
         body="""Maya Patel commented on Dashboard v3:

"@alex Can we try the denser table variant for the mail list? The current
spacing looks great in the mock but feels airy with real data."

Open the file to reply.""",
         flags=["\\Seen"]),
    dict(age=110, frm="Stratechery <newsletter@stratechery.example>",
         subj="The Amazon-OpenAI deal and the commoditization of models",
         body="""This week: why the Amazon-OpenAI arrangement is less about
distribution than about deflating the value of frontier models, and what that
means for the next wave of applications.

Read online. Unsubscribe from the footer.""",
         flags=["\\Seen"]),
    dict(age=111, frm="Lenny's Newsletter <newsletter@lennys.example>",
         subj="The five questions that changed how I run reviews",
         body="""A short issue this week on running reviews that surface real
disagreement instead of status updates, plus a template you can copy.

You can subscribe or leave anytime from your settings.""",
         flags=["\\Seen"]),
    dict(age=112, frm="DevConf Tickets <tickets@devconf.example>",
         subj="Your DevConf ticket + receipt",
         body="""You're going to DevConf!

  Order:    DC-2026-8817
  Ticket:   Full conference pass
  Total:    €420.00
  Dates:    Nov 3–5, Lisbon

Your badge QR code will be available in the app a week before the event.""",
         flags=["\\Seen"]),
]

# Rules are run for real against the demo mailbox on first scan.
DEMO_RULES = [
    dict(name="Receipts & invoices", mode="any", conditions=[
        {"field": "from", "op": "contains", "value": "billing@"},
        {"field": "from", "op": "contains", "value": "receipts@"},
        {"field": "from", "op": "contains", "value": "auto-confirm@"},
        {"field": "subject", "op": "regex", "value": "(?i)invoice|receipt|order confirmation"},
    ], actions={"move_to": "Receipts"}),
    dict(name="Newsletters & digests", mode="any", conditions=[
        {"field": "from", "op": "contains", "value": "newsletter@"},
        {"field": "from", "op": "contains", "value": "digest@"},
        {"field": "from", "op": "contains", "value": "noreply@medium.com"},
    ], actions={"move_to": "Newsletters"}),
    dict(name="Keep mail from Sarah", mode="all", conditions=[
        {"field": "from", "op": "contains", "value": "sarah.chen@"},
    ], actions={}),
]

# Flows: one deterministic (scan-time), two that act on the LLM verdict.
# All steps are move-only: a read step after the move would leave the shared
# connection selected on the destination folder mid-scan (upstream quirk), so
# the demo avoids it and keeps the pipeline 1:1.
DEMO_FLOWS = [
    dict(name="Meeting invitations → Meetings", mode="all", conditions=[
        {"field": "subject", "op": "contains", "value": "Invitation"},
    ], steps=[{"type": "move", "folder": "Meetings"}]),
    dict(name="Promotions → Promotions", mode="all", conditions=[
        {"kind": "category", "value": "Promo", "min_confidence": 0.6},
    ], steps=[{"type": "move", "folder": "Promotions"}]),
    dict(name="Receipts → Receipts", mode="all", conditions=[
        {"kind": "category", "value": "Receipt", "min_confidence": 0.6},
    ], steps=[{"type": "move", "folder": "Receipts"}]),
]

DEMO_TEMPLATES = [
    dict(name="Quick acknowledgement", subject="Re: {subject}",
         body="Hi,\n\nThanks for the note — I'll take a look today and get back "
              "to you.\n\nBest,\n{my_name}"),
    dict(name="Meeting follow-up", subject="Re: {subject}",
         body="Thanks for the time today. Quick recap of what I took away:\n\n"
              "· \n· \n\nI'll follow up with the next step by Friday.\n\n{my_name}"),
]


# ---------------------------------------------------------------- helpers

def log(msg):
    print("[demo] %s" % msg, flush=True)


def fail(msg):
    print("[demo] error: %s" % msg, file=sys.stderr, flush=True)
    sys.exit(1)


def main_checkout():
    """The main checkout (worktrees share the same .git dir)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, cwd=HERE)
        if out.returncode == 0 and out.stdout.strip():
            return os.path.dirname(out.stdout.strip())
    except OSError:
        pass
    return HERE


def read_env_file(path):
    env = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def port_free(port, host="127.0.0.1"):
    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def prepare_data_dir(path, reset):
    path = os.path.abspath(path)
    live = os.path.abspath(os.path.join(HERE, "data"))
    if path == live:
        fail("refusing to use the repo's live data directory (%s)" % live)
    marker = os.path.join(path, MARKER)
    if reset and os.path.isdir(path):
        if not os.path.exists(marker):
            fail("--reset: %s is not a demo data dir (no %s marker)" % (path, MARKER))
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    if os.path.exists(os.path.join(path, "triage.db")) and not os.path.exists(marker):
        fail("%s already contains a triage.db but no demo marker — refusing to seed over it" % path)
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"note": "demo session data - safe to delete",
                       "created": int(time.time())}, fh)
    return path


def build_imap_server(MockState, IMAPHandler, port, state):
    class DemoIMAPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = DemoIMAPServer(("127.0.0.1", port), IMAPHandler)
    server.state = state
    server.no_copyuid = False
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="demo-imap").start()
    return server


def build_raw(frm, to, subject, body, ts, msgid, html=False):
    date = email.utils.formatdate(ts, localtime=False)
    ctype = "text/html; charset=utf-8" if html else "text/plain; charset=utf-8"
    headers = [
        "From: %s" % frm,
        "To: %s" % to,
        "Subject: %s" % subject,
        "Date: %s" % date,
        "Message-ID: <%s>" % msgid,
        "MIME-Version: 1.0",
        "Content-Type: %s" % ctype,
        "Content-Transfer-Encoding: 8bit",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


def seed_mailbox(MockState, email):
    state = MockState()
    for folder, flags in (("INBOX", "\\HasNoChildren"),
                          ("Drafts", "\\HasNoChildren \\Drafts"),
                          ("Sent", "\\HasNoChildren \\Sent"),
                          ("Archive", "\\HasNoChildren")):
        state.ensure(folder, flags)
    now = time.time()
    for i, m in enumerate(DEMO_MAIL, 1):
        body = m.get("body_html") if m.get("html") else m["body"]
        raw = build_raw(m["frm"], email, m["subj"], body,
                        now - m["age"] * 3600, "demo-%03d@example.com" % i,
                        html=bool(m.get("html")))
        state.add("INBOX", raw, flags=list(m.get("flags") or []))
    return state


def state_to_json(state):
    out = {"next_uid": state._next_uid, "folders": {}}
    for name, f in state.folders.items():
        out["folders"][name] = {
            "flags": f["flags"],
            "uidvalidity": f["uidvalidity"],
            "msgs": {str(u): {"raw": base64.b64encode(m["raw"]).decode(),
                              "flags": sorted(m["flags"])}
                     for u, m in f["msgs"].items()},
        }
    return out


def load_state(MockState, path):
    data = json.load(open(path, "r", encoding="utf-8"))
    state = MockState()
    state._next_uid = data["next_uid"]
    for name, f in data["folders"].items():
        msgs = {int(u): {"raw": base64.b64decode(m["raw"]),
                         "flags": set(m["flags"])}
                for u, m in f["msgs"].items()}
        state.folders[name] = {
            "flags": f["flags"],
            "uidvalidity": f["uidvalidity"],
            "uids": sorted(msgs),
            "msgs": msgs,
        }
    return state


def save_state(state, path):
    with state.lock:
        data = state_to_json(state)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class StateSaver(threading.Thread):
    """Periodically persist the fake IMAP state so restarts keep it."""

    def __init__(self, state, path, interval=20):
        super().__init__(daemon=True, name="demo-state-saver")
        self.state, self.path, self.interval = state, path, interval
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.wait(self.interval):
            try:
                save_state(self.state, self.path)
            except Exception as exc:
                log("state save failed: %r" % exc)


# ---------------------------------------------------------------- proxy shim

def patch_proxy(proxy):
    """Make the app believe its embedded OAuth proxy is running.

    The Accounts page and dashboard probe Manager.state; the demo has no OAuth
    provider, so the fake IMAP server is the listener and `_spawn` just records
    a fake process. Everything else behaves exactly like the live app.
    """
    class _DemoProc:
        def __init__(self):
            self.pid = os.getpid()
            self.returncode = None

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode or 0

    def _spawn(self):
        self.proc = _DemoProc()

    proxy.installed = lambda: True
    proxy.Manager._spawn = _spawn


# ---------------------------------------------------------------- seeding

def seed_database(store, proxy, email, name, imap_port):
    store.init_db()
    if not proxy.get_account(email):
        rec, err = proxy.account_from_form({
            "provider": "outlook",
            "email": email,
            "password": DEMO_PASSWORD,
            "client_id": proxy.PRESETS["outlook"].get("reuse_client_id") or "demo-client-id",
            "redirect_mode": "loopback",
        })
        if err:
            fail("could not build the demo account: %s" % err)
        rec["imap_local_port"] = imap_port
        rec["smtp_local_port"] = 0
        proxy.upsert_account(rec)
        log("account created: %s" % email)

    # token cache so the account shows as authorised on the Accounts page
    cache = configparser.ConfigParser(interpolation=None)
    cache.read(proxy.cache_path())
    if not cache.has_section(email):
        now = int(time.time())
        cache[email] = {"access_token": "demo-access-token",
                        "refresh_token": "demo-refresh-token",
                        "access_token_expiry": str(now + 3600),
                        "last_activity": str(now)}
        with open(proxy.cache_path(), "w", encoding="utf-8") as fh:
            cache.write(fh)
        os.chmod(proxy.cache_path(), 0o600)

    store.set_setting("proxy_mode", "embedded")
    store.set_setting("imap_user", email)
    store.set_setting("my_name", name)
    store.set_setting("welcome_done", 1)
    store.set_setting("lookback_hours", 720)
    store.set_setting("poll_interval", 90)
    store.set_setting("max_llm_per_hour", 200)
    store.set_setting("llm_batch_per_cycle", 5)
    store.set_setting("classify_concurrency", 6)
    # lite RAG backend on CPU (matches a default install: FastEmbed/ONNX)
    store.set_setting("embed_protocol", "local")
    store.set_setting("rerank_protocol", "local")

    if not store.list_rules():
        for r in DEMO_RULES:
            store.add_rule(r["name"], r["mode"], r["conditions"], r["actions"])
        log("seeded %d rules" % len(DEMO_RULES))
    if not store.list_flows():
        for f in DEMO_FLOWS:
            store.add_flow(f["name"], f["mode"], f["conditions"], f["steps"])
        log("seeded %d flows" % len(DEMO_FLOWS))
    if not store.list_templates():
        for t in DEMO_TEMPLATES:
            store.add_template(t["name"], t["subject"], t["body"])
        log("seeded %d templates" % len(DEMO_TEMPLATES))


def run_scan(engine, store):
    log("first scan against the fake mailbox ...")
    summary = engine.process_mailbox()
    log("scan: %s" % (summary or "nothing new"))
    log("%d message(s) stored, %d awaiting classification"
        % (store.count_messages("all"), store.unclassified_count()))


def run_classify(app_mod, store, timeout=1800):
    pending = store.unclassified_count()
    if not pending:
        return
    log("classifying %d message(s) with the real LLM ..." % pending)
    app_mod.classifier.start()
    deadline = time.time() + timeout
    while store.unclassified_count() and time.time() < deadline:
        app_mod.classifier.trigger(None)
        started = time.time()
        while not app_mod.classifier.state.get("running") and time.time() - started < 10:
            time.sleep(0.3)
        last = -1
        while app_mod.classifier.state.get("running") and time.time() < deadline:
            done = app_mod.classifier.state.get("done") or 0
            if done != last:
                print("    classified %d/%d" % (done, app_mod.classifier.state.get("total") or pending),
                      flush=True)
                last = done
            time.sleep(0.5)
    left = store.unclassified_count()
    if left:
        log("warning: %d message(s) still unclassified (LLM errors?)" % left)
    else:
        log("classification complete")


def run_index(rag, timeout=1200):
    log("building the search index (CPU embeddings) ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = rag.index_pass_active(limit=40)
        print("    index: %s" % res.get("summary"), flush=True)
        if not res.get("remaining"):
            return True
        if not res.get("processed"):
            break
    log("warning: index did not finish")
    return False


def seed_tags_and_learning(store, heuristics, engine):
    """Tag a few demo messages, train two classifiers, propose rules."""
    rows = store.messages(limit=500)
    tag_map = {
        "Newsletter": lambda m: ("newsletter@" in (m["from_addr"] or "")
                                 or "digest@" in (m["from_addr"] or "")
                                 or "noreply@medium.com" in (m["from_addr"] or "")),
        "Receipt": lambda m: (m["folder"] == "Receipts"
                              or "billing@" in (m["from_addr"] or "")
                              or "receipts@" in (m["from_addr"] or "")),
        "Notification": lambda m: ("github.com" in (m["from_addr"] or "")
                                   or "notifications@" in (m["from_addr"] or "")
                                   or "no-reply@vercel.com" in (m["from_addr"] or "")),
    }
    tagged = 0
    for cat, pred in tag_map.items():
        ids = [m["id"] for m in rows if pred(m)]
        if len(ids) >= 5:
            store.tag_messages([str(i) for i in ids], cat, by="user")
            tagged += len(ids)
    log("tagged %d message(s) for the learning loop" % tagged)

    for cat, kind in (("Newsletter", "naive_bayes"), ("Receipt", "decision_list")):
        try:
            model, stats = heuristics.train_heuristic(
                kind, cat, source="tags", min_confidence=0.7, created_by="demo")
            store.add_heuristic(name="%s (from tags)" % cat, kind=kind, category=cat,
                               model=json.dumps(model), stats=json.dumps(stats),
                               min_confidence=0.7, created_by="demo")
            log("trained classifier: %s / %s" % (cat, kind))
        except Exception as exc:
            log("classifier %s skipped: %s" % (cat, exc))

    try:
        reply, rules = engine.propose_rules_from_tags()
        for r in rules:
            store.add_rule_proposal("tags", r, reply)
        log("rule proposals from tags: %d" % len(rules))
    except Exception as exc:
        log("rule proposals skipped: %s" % exc)


# ---------------------------------------------------------------- boot

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(HERE, "demo-data"))
    ap.add_argument("--port", type=int, default=8098, help="UI port (default 8098)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--imap-port", type=int, default=41993)
    ap.add_argument("--email", default=DEMO_EMAIL)
    ap.add_argument("--name", default=DEMO_NAME)
    ap.add_argument("--env-file", default=os.path.join(main_checkout(), ".env"),
                    help="real LLM settings are read from here")
    ap.add_argument("--reset", action="store_true", help="wipe the demo data dir and reseed")
    ap.add_argument("--skip-llm-seed", action="store_true",
                    help="don't pre-classify at boot (the UI's Classify all still works)")
    ap.add_argument("--skip-index", action="store_true")
    args = ap.parse_args()

    data_dir = prepare_data_dir(args.data_dir, args.reset)
    if not port_free(args.port, args.host):
        fail("UI port %d is already in use on %s" % (args.port, args.host))
    if not port_free(args.imap_port):
        fail("fake IMAP port %d is already in use" % args.imap_port)

    env = read_env_file(args.env_file)
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT",
              "LLM_FALLBACK_BASE_URL", "LLM_FALLBACK_API_KEY", "LLM_FALLBACK_MODEL",
              "EMBED_BASE_URL", "EMBED_MODEL", "RERANK_BASE_URL", "RERANK_MODEL"):
        if not os.environ.get(k) and env.get(k):
            os.environ[k] = env[k]
    if not os.environ.get("LLM_BASE_URL"):
        fail("no LLM endpoint found (looked in %s); the demo needs a real one "
             "for classification and the assistant" % args.env_file)
    os.environ["DATA_DIR"] = data_dir
    os.environ["IMAP_HOST"] = "127.0.0.1"
    os.environ["IMAP_PORT"] = str(args.imap_port)
    os.environ["IMAP_USER"] = args.email
    os.environ["IMAP_PASSWORD"] = DEMO_PASSWORD
    os.environ["IMAP_TLS"] = "0"
    os.environ["UI_HOST"] = args.host
    os.environ["UI_PORT"] = str(args.port)
    os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(main_checkout(), "ragmodels"))

    # app imports must happen after DATA_DIR etc. are set
    sys.path.insert(0, HERE)
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import mock_e2e  # the protocol-exact fake IMAP server used by the test suite
    import config
    import store
    import engine
    import rag
    import heuristics
    import proxy
    import plugins
    import plugin_rt
    import app as app_mod

    store.init_db()
    log("demo data: %s" % config.DATA_DIR)
    log("UI: http://%s:%d   fake IMAP: 127.0.0.1:%d" % (args.host, args.port, args.imap_port))
    log("LLM: %s (%s)" % (engine.llm_config().get("base") or "?",
                          engine.llm_config().get("model") or "?"))

    state_path = os.path.join(data_dir, "imap-state.json")
    if os.path.exists(state_path) and not args.reset:
        state = load_state(mock_e2e.MockState, state_path)
        log("restored fake mailbox state (%d folder(s))" % len(state.folders))
    else:
        state = seed_mailbox(mock_e2e.MockState, args.email)
        save_state(state, state_path)
        log("seeded fake mailbox (%d message(s))" % len(state.get("INBOX")["uids"]))
    build_imap_server(mock_e2e.MockState, mock_e2e.IMAPHandler, args.imap_port, state)
    saver = StateSaver(state, state_path)
    saver.start()

    patch_proxy(proxy)
    plugins.scan()
    store.init_db()
    seed_database(store, proxy, args.email, args.name, args.imap_port)

    first_run = store.count_messages("all") == 0
    if first_run:
        store.set_setting("llm_batch_per_cycle", 0)  # classify via the batch job below
        run_scan(engine, store)
    if store.unclassified_count() and (not args.skip_llm_seed or not first_run):
        run_classify(app_mod, store)
    if not args.skip_index and not rag.index_stats().get("messages"):
        run_index(rag)
    if not store.list_heuristics() and not store.tagged_examples(1):
        seed_tags_and_learning(store, heuristics, engine)
    if first_run:
        with open(os.path.join(data_dir, MARKER), "w", encoding="utf-8") as fh:
            json.dump({"email": args.email, "created": int(time.time()),
                       "ui_port": args.port, "imap_port": args.imap_port,
                       "note": "demo session data - safe to delete"}, fh)
    store.set_setting("llm_batch_per_cycle", 5)  # normal live batching

    # normal app boot (same threads as `python app.py`)
    app_mod.worker.start()
    app_mod.indexer.start()
    if not app_mod.classifier.is_alive():
        app_mod.classifier.start()
    proxy.supervisor.start()
    plugin_rt.scheduler.start()

    log("ready — open http://%s:%d/ (Ctrl+C to stop)" % (args.host, args.port))
    try:
        app_mod.app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        saver.stop_flag.set()
        save_state(state, state_path)
        log("stopped; fake mailbox state saved")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
