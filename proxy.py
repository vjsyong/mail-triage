"""Embedded email-oauth2-proxy: account store, config generation, process supervision,
authorisation flow and token status.

This folds the standalone emailproxy deployment (systemd services + companion UI) into
Mail Triage: the proxy runs as a child process of this app, its emailproxy.config is
generated from the `proxy_accounts` table, and the OAuth flows (authorise / paste-back /
reset tokens) are driven from the Accounts page.

Layout under DATA_DIR/emailproxy/:
  emailproxy.config     generated from the account table (never edit by hand)
  credentials.cache     OAuth tokens, written by the proxy itself
  emailproxy.log        proxy log (rotates at 32 MB)
  emailproxy-stdout.log child process stdout/stderr (startup problems land here)

The proxy binds its listeners to 127.0.0.1 INSIDE this app's container; nothing is
published except the OAuth callback ports (compose maps the redirect pool on host
loopback for tailnet-mode redirects; loopback-mode flows complete via the paste-back
box). The pool defaults to 41810-41819; a second instance on the same host shifts it
via PROXY_REDIRECT_POOL_START so its redirect URIs land on its own published ports.
"""
import configparser
import importlib.util
import json
import os
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import config
import store

# ---------------------------------------------------------------- paths

def proxy_dir():
    d = os.path.join(config.DATA_DIR, "emailproxy")
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    return os.path.join(proxy_dir(), "emailproxy.config")


def cache_path():
    return os.path.join(proxy_dir(), "credentials.cache")


def log_path():
    return os.path.join(proxy_dir(), "emailproxy.log")


def stdout_path():
    return os.path.join(proxy_dir(), "emailproxy-stdout.log")


# ---------------------------------------------------------------- provider presets

PRESETS = {
    "gmail": {
        "label": "Gmail / Google Workspace",
        "auth_url": "https://accounts.google.com/o/oauth2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "https://mail.google.com/",
        "imap": {"host": "imap.gmail.com", "port": 993, "local_port": 2993},
        "smtp": {"host": "smtp.gmail.com", "port": 465, "local_port": 2465, "starttls": False},
        "register_notes": (
            "Google Cloud Console -> APIs & Services -> Credentials -> Create credentials -> "
            "OAuth client ID. Use application type \"Web application\" and add the redirect URI "
            "shown on the account card. Configure the OAuth consent screen with the scope "
            "https://mail.google.com/ and set publishing status to \"In production\" so refresh "
            "tokens do not expire after 7 days."
        ),
    },
    "outlook": {
        "label": "Outlook / Microsoft 365 / Hotmail",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ("https://outlook.office.com/IMAP.AccessAsUser.All "
                   "https://outlook.office.com/SMTP.Send offline_access"),
        "imap": {"host": "outlook.office365.com", "port": 993, "local_port": 1993},
        "smtp": {"host": "smtp-mail.outlook.com", "port": 587, "local_port": 1587, "starttls": True},
        "loopback_scheme": "https",
        "reuse_client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        "register_notes": (
            "Have your own Entra app? Use it: Entra admin center -> App registrations -> New "
            "registration, add the redirect URI shown on the account card under platform \"Web\", "
            "grant delegated permissions IMAP.AccessAsUser.All + SMTP.Send + offline_access, then "
            "use tailnet mode. Cannot register one (personal accounts; most university tenants)? "
            "Tick \"Use Thunderbird's public client ID\" on the add form: loopback mode is forced "
            "and the login finishes by pasting the final browser URL into the Authorise panel."
        ),
    },
    "fastmail": {
        "label": "Fastmail",
        "auth_url": "https://api.fastmail.com/oauth/authorize",
        "token_url": "https://api.fastmail.com/oauth/refresh",
        "scopes": ("https://www.fastmail.com/dev/protocol-imap "
                   "https://www.fastmail.com/dev/protocol-smtp"),
        "use_pkce": True,
        "imap": {"host": "imap.fastmail.com", "port": 993, "local_port": 3993},
        "smtp": {"host": "smtp.fastmail.com", "port": 465, "local_port": 3465, "starttls": False},
        "register_notes": (
            "Fastmail -> Settings -> Privacy & Security -> Manage API tokens -> New application; "
            "register the redirect URI shown on the account card. Fastmail uses PKCE (no client "
            "secret)."
        ),
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "auth_url": "https://api.login.yahoo.com/oauth2/request_auth",
        "token_url": "https://api.login.yahoo.com/oauth2/get_token",
        "scopes": "mail-w",
        "imap": {"host": "imap.mail.yahoo.com", "port": 993, "local_port": 4993},
        "smtp": {"host": "smtp.mail.yahoo.com", "port": 465, "local_port": 4465, "starttls": False},
        "register_notes": (
            "Yahoo does not accept new client registrations with the mail scope: reuse an existing "
            "client ID (for example Thunderbird's) and loopback mode."
        ),
    },
    "custom": {
        "label": "Custom / other provider",
        "register_notes": (
            "Enter the OAuth 2.0 endpoints and IMAP/SMTP server details as documented by your "
            "provider. Make sure the OAuth scope allows IMAP/SMTP access and offline refresh."
        ),
    },
}

REDIRECT_POOL_START = config.PROXY_REDIRECT_POOL_START
REDIRECT_POOL_SIZE = config.PROXY_REDIRECT_POOL_SIZE
IMAP_LOCAL_BASE = 15400   # custom-provider IMAP local ports
SMTP_LOCAL_BASE = 16400   # custom-provider SMTP local ports
AUTH_DEADLINE = 660       # seconds the proxy keeps an authorisation window open
BIND_ADDRESS = "127.0.0.1"

AUTH_URL_RE = "Please visit the following URL to authenticate account %s: (\\S+)"


# ---------------------------------------------------------------- account store

def list_accounts():
    with store.db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM proxy_accounts ORDER BY email")]


def get_account(email=None):
    """Exact account (case-insensitive) or, without an email, the first configured account."""
    with store.db() as conn:
        if email:
            row = conn.execute("SELECT * FROM proxy_accounts WHERE lower(email)=lower(?)",
                               (email,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM proxy_accounts ORDER BY email LIMIT 1").fetchone()
    return dict(row) if row else None


def upsert_account(rec):
    now = int(time.time())
    fields = {k: rec.get(k, "") for k in (
        "email", "provider", "password", "client_id", "client_secret", "scopes", "auth_url",
        "token_url", "redirect_mode", "imap_host", "smtp_host")}
    nums = {k: int(rec.get(k) or 0) for k in (
        "use_pkce", "redirect_port", "imap_port", "imap_local_port", "smtp_port", "smtp_local_port",
        "smtp_starttls")}
    with store.db() as conn:
        conn.execute(
            "INSERT INTO proxy_accounts (email, provider, password, client_id, client_secret, "
            "scopes, auth_url, token_url, use_pkce, redirect_port, redirect_mode, imap_host, "
            "imap_port, imap_local_port, smtp_host, smtp_port, smtp_local_port, smtp_starttls, "
            "created, updated) VALUES (:email, :provider, :password, :client_id, :client_secret, "
            ":scopes, :auth_url, :token_url, :use_pkce, :redirect_port, :redirect_mode, :imap_host, "
            ":imap_port, :imap_local_port, :smtp_host, :smtp_port, :smtp_local_port, :smtp_starttls, "
            ":created, :updated) "
            "ON CONFLICT(email) DO UPDATE SET provider=:provider, password=:password, "
            "client_id=:client_id, client_secret=:client_secret, scopes=:scopes, "
            "auth_url=:auth_url, token_url=:token_url, use_pkce=:use_pkce, "
            "redirect_port=:redirect_port, redirect_mode=:redirect_mode, imap_host=:imap_host, "
            "imap_port=:imap_port, imap_local_port=:imap_local_port, smtp_host=:smtp_host, "
            "smtp_port=:smtp_port, smtp_local_port=:smtp_local_port, "
            "smtp_starttls=:smtp_starttls, updated=:updated",
            {**fields, **nums, "created": now, "updated": now})
    return rec.get("email")


def delete_account(email):
    with store.db() as conn:
        conn.execute("DELETE FROM proxy_accounts WHERE lower(email)=lower(?)", (email,))


def used_redirect_ports():
    return {a["redirect_port"] for a in list_accounts() if a["redirect_port"]}


def alloc_redirect_port():
    taken = used_redirect_ports()
    for port in range(REDIRECT_POOL_START, REDIRECT_POOL_START + REDIRECT_POOL_SIZE):
        if port not in taken:
            return port
    return 0


def used_local_ports():
    out = set()
    for a in list_accounts():
        for key in ("imap_local_port", "smtp_local_port"):
            if a.get(key):
                out.add(int(a[key]))
    return out


def alloc_local_port(base):
    taken = used_local_ports()
    port = base
    while port in taken:
        port += 1
    return port


def default_password():
    return "ep-" + secrets.token_urlsafe(9)


def account_from_form(form, existing=None):
    """Build an account record from the add/edit form. Returns (record, error)."""
    provider = (form.get("provider") or "custom").strip()
    if provider not in PRESETS:
        return None, "Unknown provider."
    preset = PRESETS[provider]
    email = (form.get("email") or (existing or {}).get("email") or "").strip()
    password = (form.get("password") or "").strip()
    client_id = (form.get("client_id") or "").strip()
    client_secret = (form.get("client_secret") or "").strip()
    redirect_mode = (form.get("redirect_mode") or "tailnet").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None, "That does not look like a valid email address."
    if not password or any(ch.isspace() for ch in password):
        return None, "The local password must be non-empty and contain no whitespace."
    if not client_id:
        return None, "A client ID is required (provider app credentials, or a reused public ID)."
    if provider == "outlook" and client_id == preset.get("reuse_client_id") \
            and redirect_mode != "loopback":
        redirect_mode = "loopback"
    rec = {
        "email": email,
        "provider": provider,
        "password": password,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_mode": "loopback" if redirect_mode == "loopback" else "tailnet",
    }
    if existing:
        rec["redirect_port"] = existing.get("redirect_port") or alloc_redirect_port()
        rec["imap_local_port"] = existing.get("imap_local_port")
        rec["smtp_local_port"] = existing.get("smtp_local_port")
    else:
        rec["redirect_port"] = alloc_redirect_port()
        rec["imap_local_port"] = None
        rec["smtp_local_port"] = None
    if provider == "custom":
        for k, cast in (("imap_host", str), ("imap_port", int), ("smtp_host", str),
                        ("smtp_port", int)):
            val = (form.get(k) or "").strip()
            rec[k] = cast(val or 0) if cast is int else val
        rec["scopes"] = (form.get("scopes") or form.get("scope") or "").strip()
        rec["auth_url"] = (form.get("auth_url") or form.get("permission_url") or "").strip()
        rec["token_url"] = (form.get("token_url") or "").strip()
        rec["use_pkce"] = 1 if form.get("use_pkce") else 0
        if not (rec["imap_host"] and rec["auth_url"] and rec["token_url"] and rec["scopes"]):
            return None, ("For a custom provider, fill in the OAuth URLs, scope and IMAP "
                          "server fields.")
        if not rec["imap_port"]:
            rec["imap_port"] = 993
        if not rec["smtp_port"]:
            rec["smtp_port"] = 465
        if not rec["imap_local_port"]:
            rec["imap_local_port"] = alloc_local_port(IMAP_LOCAL_BASE)
        if rec["smtp_host"] and not rec["smtp_local_port"]:
            rec["smtp_local_port"] = alloc_local_port(SMTP_LOCAL_BASE)
    else:
        rec.update({
            "auth_url": preset["auth_url"], "token_url": preset["token_url"],
            "scopes": preset["scopes"], "use_pkce": 1 if preset.get("use_pkce") else 0,
            "imap_host": preset["imap"]["host"], "imap_port": preset["imap"]["port"],
            "imap_local_port": existing.get("imap_local_port") if existing
                               else preset["imap"]["local_port"],
            "smtp_host": preset["smtp"]["host"], "smtp_port": preset["smtp"]["port"],
            "smtp_local_port": existing.get("smtp_local_port") if existing
                               else preset["smtp"]["local_port"],
            "smtp_starttls": 1 if preset["smtp"].get("starttls") else 0,
        })
    return rec, None


# ---------------------------------------------------------------- config generation

def redirect_uri(account):
    if account.get("redirect_mode") == "loopback":
        scheme = PRESETS.get(account.get("provider"), {}).get("loopback_scheme", "http")
        return "%s://localhost:%d" % (scheme, int(account.get("redirect_port") or 0))
    host = (store.get_setting("proxy_tailnet_host") or "").strip() or "localhost"
    return "https://%s:%d" % (host, int(account.get("redirect_port") or 0))


def _server_rows(accounts):
    """Unique listener rows [(kind, section, host, port, local_port, starttls)]."""
    seen, rows = set(), []
    for a in accounts:
        for kind, client_key, local_key in (("IMAP", "imap_host", "imap_local_port"),
                                            ("SMTP", "smtp_host", "smtp_local_port")):
            host = a.get(client_key) or ""
            local = int(a.get(local_key) or 0)
            if not host or not local:
                continue
            remote = int(a.get(client_key.replace("_host", "_port")) or 0)
            key = (kind, local)
            if key in seen:
                continue
            seen.add(key)
            rows.append((kind, "%s-%d" % (kind, local), host, remote, local,
                         bool(a.get("smtp_starttls"))))
    rows.sort(key=lambda r: (r[0], r[4]))
    return rows


def config_text(accounts=None):
    accounts = list_accounts() if accounts is None else accounts
    out = [
        "# Email OAuth 2.0 Proxy configuration - generated by Mail Triage.",
        "# Manage accounts on the app's Accounts page; this file is rewritten on every change.",
        "# OAuth tokens are cached separately in credentials.cache (--cache-store).",
        "",
        "[emailproxy]",
        "delete_account_token_on_password_error = False",
        "encrypt_client_secret_on_first_use = False",
        "use_login_password_as_client_credentials_secret = False",
        "allow_catch_all_accounts = False",
        "",
    ]
    for kind, section, host, remote_port, local_port, starttls in _server_rows(accounts):
        out.append("[%s]" % section)
        out.append("server_address = %s" % host)
        out.append("server_port = %d" % remote_port)
        if starttls:
            out.append("server_starttls = True")
        out.append("local_address = %s" % BIND_ADDRESS)
        out.append("")
    for a in accounts:
        out.append("[%s]" % a["email"])
        out.append("permission_url = %s" % a.get("auth_url", ""))
        out.append("token_url = %s" % a.get("token_url", ""))
        out.append("oauth2_scope = %s" % a.get("scopes", ""))
        out.append("redirect_uri = %s" % redirect_uri(a))
        out.append("redirect_listen_address = http://127.0.0.1:%d" % int(a.get("redirect_port") or 0))
        out.append("client_id = %s" % a.get("client_id", ""))
        if a.get("client_secret"):
            out.append("client_secret = %s" % a["client_secret"])
        if a.get("use_pkce"):
            out.append("use_pkce = True")
        out.append("")
    return "\n".join(out)


def write_config():
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(config_text())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


# ---------------------------------------------------------------- process manager

def installed():
    try:
        return importlib.util.find_spec("emailproxy") is not None
    except (ImportError, ValueError):
        return False


class Manager:
    """Supervises the embedded emailproxy child process."""

    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.last_error = None
        self.started_at = 0
        self.restarts = 0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def ports(self):
        return [int(a["imap_local_port"]) for a in list_accounts() if a.get("imap_local_port")]

    def status(self):
        ports = self.ports()
        listening = {}
        for p in ports:
            try:
                s = socket.create_connection(("127.0.0.1", p), timeout=0.5)
                s.close()
                listening[p] = True
            except OSError:
                listening[p] = False
        return {
            "mode": store.get_setting("proxy_mode") or "embedded",
            "installed": installed(),
            "running": self.running(),
            "pid": self.proc.pid if self.running() else None,
            "started_at": self.started_at or None,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "ports": listening,
            "config_file": config_path(),
            "cache_file": cache_path(),
            "log_file": log_path(),
        }

    def _spawn(self):
        handle = open(stdout_path(), "ab")
        cmd = [sys.executable, "-m", "emailproxy", "--no-gui", "--local-server-auth",
               "--config-file", config_path(), "--cache-store", cache_path(),
               "--log-file", log_path()]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(cmd, cwd=proxy_dir(), stdout=handle, stderr=handle,
                                     env=env, start_new_session=True)

    def _wait_ready(self, timeout=30.0):
        ports = self.ports()
        if not ports:
            time.sleep(1.0)
            return self.running()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running():
                return False
            for p in ports:
                try:
                    s = socket.create_connection(("127.0.0.1", p), timeout=0.5)
                    s.close()
                    return True
                except OSError:
                    continue
            time.sleep(0.4)
        return False

    def start(self):
        with self.lock:
            if self.running():
                return True, None
            if not installed():
                self.last_error = ("the emailproxy package is not installed in this image "
                                   "(pip install emailproxy)")
                return False, self.last_error
            if not list_accounts():
                self.last_error = "no accounts configured yet"
                return False, self.last_error
            write_config()
            try:
                self._spawn()
            except Exception as exc:  # pragma: no cover
                self.last_error = "could not start emailproxy: %r" % exc
                return False, self.last_error
            if self._wait_ready():
                self.last_error = None
                self.started_at = int(time.time())
                store.log_event("info", "emailproxy started (pid %s)" % self.proc.pid)
                return True, None
            err = self.last_error = "emailproxy did not start listening (see the proxy log)"
            self.stop()
            return False, err

    def stop(self, timeout=15.0):
        with self.lock:
            proc = self.proc
            if proc is None:
                return True, None
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass
                deadline = time.time() + timeout
                while proc.poll() is None and time.time() < deadline:
                    time.sleep(0.2)
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:  # pragma: no cover
                        pass
            rc = proc.returncode
            self.proc = None
            if rc not in (0, None, -signal.SIGTERM):
                self.last_error = "emailproxy exited with code %s" % rc
            return True, None

    def restart(self):
        self.stop()
        return self.start()

    def apply(self, edit_fn=None):
        """Stop the proxy, apply an optional cache edit, rewrite the config, start again.

        Cache edits must happen while the daemon is stopped: it rewrites
        credentials.cache from memory when it exits, so edits made while it runs are
        silently overwritten. Config changes need a full restart too (SIGHUP-style
        reload leaves listeners in a rebind race and the daemon exits; verified
        against the asyncore loop in this kernel)."""
        with self.lock:
            self.stop()
            if edit_fn is not None:
                try:
                    edit_fn()
                except Exception as exc:
                    self.last_error = "edit failed: %r" % exc
                    return False, self.last_error
            return self.start()


manager = Manager()


def wait_ready(timeout=20.0):
    """True once any configured listener accepts connections. Used at boot so the
    worker's first cycle does not race the proxy coming up."""
    ports = manager.ports()
    if not ports:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in ports:
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                s.close()
                return True
            except OSError:
                continue
        time.sleep(0.3)
    return False


def ensure_running():
    """Called by the supervisor loop: keep the proxy up when it should be up."""
    if (store.get_setting("proxy_mode") or "embedded").lower() != "embedded":
        return
    if not list_accounts() or manager.running():
        return
    manager.start()


class Supervisor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="emailproxy-supervisor")
        self.stop_flag = threading.Event()

    def run(self):
        self.stop_flag.wait(2)  # let the UI come up
        while not self.stop_flag.is_set():
            try:
                ensure_running()
            except Exception as exc:  # pragma: no cover
                store.log_event("error", "emailproxy supervisor: %r" % exc)
            self.stop_flag.wait(15)


supervisor = Supervisor()


# ---------------------------------------------------------------- token cache helpers

def _load_ini(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    if os.path.exists(path):
        try:
            parser.read(path)
        except (OSError, configparser.Error):
            pass
    return parser


def token_status(email):
    """Authorisation status from the token cache (falls back to the config file)."""
    cache = _load_ini(cache_path())
    conf = _load_ini(config_path())
    src = cache if cache.has_section(email) else conf
    result = {"authorized": False, "access_token": False, "refresh_token": False,
              "expires_at": None, "last_activity": None}
    if not src.has_section(email):
        return result
    result["refresh_token"] = bool(src.get(email, "refresh_token", fallback=None))
    result["access_token"] = bool(src.get(email, "access_token", fallback=None))
    result["authorized"] = result["refresh_token"] or result["access_token"]
    for key, name in (("access_token_expiry", "expires_at"), ("last_activity", "last_activity")):
        try:
            result[name] = int(src.get(email, key, fallback="") or "")
        except (TypeError, ValueError):
            result[name] = None
    return result


def _remove_cache_section(email):
    path = cache_path()
    cache = _load_ini(path)
    if cache.has_section(email):
        cache.remove_section(email)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            cache.write(fh)
        os.replace(tmp, path)
        os.chmod(path, 0o600)


def reset_tokens(email):
    """Forget the cached OAuth tokens for an account (proxy must be stopped while editing)."""
    ok, err = manager.apply(edit_fn=lambda: _remove_cache_section(email))
    if ok:
        store.log_event("info", "emailproxy: tokens reset for %s" % email)
    return ok, err


def remove_account(email):
    """Remove an account: tokens, config entry and DB row (proxy stopped during the edit)."""
    def _edit():
        _remove_cache_section(email)
        delete_account(email)

    ok, err = manager.apply(edit_fn=_edit)
    if not ok and err == "no accounts configured yet":
        ok, err = True, None  # nothing left to serve: a clean outcome
    if ok:
        store.log_event("info", "emailproxy: account %s removed" % email)
    return ok, err


# ---------------------------------------------------------------- authorisation flow

AUTH_STATE = {}
AUTH_LOCK = threading.Lock()


def auth_state(email):
    return dict(AUTH_STATE.get(email) or {"status": "idle",
                                          "message": "No authorisation in progress."})


def _log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_new_log(path, pos):
    try:
        size = os.path.getsize(path)
        if size < pos:  # rotation
            pos = 0
        if size == pos:
            return "", pos
        with open(path, "rb") as fh:
            fh.seek(pos)
            chunk = fh.read(min(size - pos, 512 * 1024))
            return chunk.decode("utf-8", "replace"), pos + len(chunk)
    except OSError:
        return "", pos


def start_auth(email):
    if not AUTH_LOCK.acquire(blocking=False):
        return False, "Another authorisation is already in progress; wait for it to finish."
    entry = AUTH_STATE.setdefault(email, {})
    entry.clear()
    entry.update({"status": "starting", "message": "Starting…", "url": None,
                  "started_at": time.time(), "updated_at": time.time()})
    try:
        manager.start()  # make sure the proxy is up before triggering a login
    except Exception as exc:  # pragma: no cover
        AUTH_LOCK.release()
        return False, "could not start the proxy: %r" % exc
    threading.Thread(target=_auth_worker, args=(email,), daemon=True).start()
    return True, None


def _auth_worker(email):
    entry = AUTH_STATE[email]

    def upd(**kw):
        entry.update(kw)
        entry["updated_at"] = time.time()

    try:
        acct = get_account(email)
        if not acct:
            upd(status="failed", message="Unknown account.")
            return
        port = acct.get("imap_local_port")
        password = acct.get("password")
        if not port:
            upd(status="failed", message="No IMAP listener recorded for this account; re-add it.")
            return
        if not password:
            upd(status="failed", message="No local password stored for this account.")
            return

        path = log_path()
        deadline = time.time() + AUTH_DEADLINE
        pos = _log_size(path)
        url_re = re.compile(AUTH_URL_RE % re.escape(email))
        outcome = None

        for attempt in range(1, 5):
            if time.time() >= deadline:
                outcome = outcome or ("timeout", "Timed out waiting for authorisation.")
                break
            upd(status="triggering",
                message=("Connecting to the proxy at 127.0.0.1:%s…" % port) if attempt == 1
                else "Retrying authorisation (attempt %d)…" % attempt)
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", int(port)), timeout=15)
            except OSError as exc:
                upd(status="failed", message=("Could not connect to the embedded proxy on port "
                                              "%s — is it running? (%s)" % (port, exc)))
                return
            try:
                sock.setblocking(False)
                # Wait for the greeting (relayed by the proxy) before sending LOGIN: a login
                # racing the proxy's upstream TLS setup fails right after authorisation.
                greet_deadline = time.time() + 15
                buf = b""
                greeted = False
                while time.time() < greet_deadline and not greeted and b"* BYE" not in buf:
                    try:
                        readable, _, _ = select.select([sock], [], [], 0.5)
                    except (OSError, ValueError):
                        break
                    if readable:
                        try:
                            data = sock.recv(8192)
                        except BlockingIOError:
                            continue
                        except OSError:
                            break
                        if not data:
                            break
                        buf = (buf + data)[-131072:]
                        if b"* OK" in buf or b"* PREAUTH" in buf:
                            greeted = True
                if b"* BYE" in buf:
                    upd(status="failed", message="The proxy refused the connection: %s"
                        % buf.decode("utf-8", "replace").strip()[:200])
                    return
                sock.sendall(("a1 login %s %s\r\n" % (email, password)).encode())
                upd(status="triggered",
                    message="Login attempt sent. Waiting for the proxy to raise an "
                            "authorisation request…")
                attempt_outcome = None
                while time.time() < deadline:
                    text, pos = _read_new_log(path, pos)
                    if text:
                        m = url_re.search(text)
                        if m and entry.get("status") not in ("url_ready", "success"):
                            upd(status="url_ready", url=m.group(1),
                                message="Authorisation URL ready. Open it in a browser and "
                                        "complete the login.")
                    try:
                        readable, _, _ = select.select([sock], [], [], 0.5)
                    except (OSError, ValueError):
                        readable = []
                    if readable:
                        try:
                            data = sock.recv(8192)
                        except BlockingIOError:
                            continue
                        except OSError as exc:
                            attempt_outcome = ("failed", "Connection error while waiting: %s" % exc)
                            break
                        if not data:
                            attempt_outcome = ("failed", "The proxy closed the connection unexpectedly.")
                            break
                        buf = (buf + data)[-131072:]
                        m = re.search(rb"(?m)^a1 (OK|NO|BAD)[^\r\n]*", buf)
                        if m:
                            verdict = m.group(1).decode()
                            line = m.group(0).decode("utf-8", "replace").strip()[:280]
                            if verdict == "OK":
                                attempt_outcome = ("success", "Authenticated. " + line)
                            else:
                                attempt_outcome = ("failed", "Login failed: " + line)
                            break
                if attempt_outcome is None:
                    attempt_outcome = ("timeout",
                                       "Timed out after %d seconds. The proxy's authorisation "
                                       "window is %d seconds; try again."
                                       % (AUTH_DEADLINE, AUTH_DEADLINE))
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

            # A quick retry can fail while the previous callback connection is still closing:
            # the callback receiver binds without SO_REUSEADDR, so a recent TIME_WAIT entry
            # blocks it for ~60 s. Detect and wait it out.
            if attempt_outcome[0] == "failed" and entry.get("url") is None and attempt < 4:
                text, pos = _read_new_log(path, pos)
                if "unable to start local server" in text:
                    upd(status="triggering",
                        message="The proxy's callback port is still in use from a recent "
                                "attempt; retrying in 20 seconds…")
                    time.sleep(20)
                    continue
            outcome = attempt_outcome
            break

        if outcome is None:
            outcome = ("timeout", "Timed out waiting for authorisation.")
        upd(status=outcome[0], message=outcome[1])
        if outcome[0] == "success":
            store.log_event("info", "emailproxy: %s authorised" % email)
        else:
            store.log_event("info", "emailproxy: authorisation for %s: %s" % (email, outcome[0]))
    except Exception as exc:  # pragma: no cover
        upd(status="failed", message="Unexpected error: %r" % exc)
    finally:
        AUTH_LOCK.release()


def complete_auth(email, pasted):
    """Complete a login by replaying the final redirect URL the user pasted.

    Some flows (e.g. Microsoft with a reused client) redirect to https://localhost, which
    can never load in a browser; the standard way to finish is to copy the failed URL and
    paste it back. Replay its query to the proxy's one-shot callback listener (inside this
    container). Returns (ok, message)."""
    acct = get_account(email)
    if not acct:
        return False, "Unknown account."
    pasted = (pasted or "").strip()
    if not pasted:
        return False, "Paste the URL from the browser's address bar first."
    parsed = urllib.parse.urlparse(pasted)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, ("That does not look like a full URL (it should start with http:// or "
                       "https://).")
    query = parsed.query
    if not query or ("code=" not in query and "error=" not in query):
        return False, ("No code parameter found in the URL — copy the complete address from the "
                       "browser's address bar.")
    port = int(acct.get("redirect_port") or 0)
    if not port:
        return False, "No redirect port recorded for this account."
    replay_url = "http://127.0.0.1:%d/?%s" % (port, query)
    last_err = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(replay_url, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            if "successfully authenticated" not in body:
                return False, ("The proxy's callback page did not look right — is this the URL "
                               "from the expected login?")
            params = {str(k): v for k, v in urllib.parse.parse_qsl(query)}
            if "error" in params:
                detail = params.get("error_description") or ""
                return False, "The provider returned an error: %s %s" % (params.get("error"), detail)
            return True, "Callback submitted — waiting for the proxy to finish the token exchange…"
        except OSError as exc:
            last_err = exc
            time.sleep(1.2)
    return False, ("Could not reach the proxy's callback listener on port %s — press Authorise "
                   "first (and open the login link), then submit again. (%s)" % (port, last_err))


def tail_log(lines=300):
    path = log_path()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return "(no proxy log yet)"
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-max(1, int(lines)):])


# ---------------------------------------------------------------- legacy import

def import_legacy_state(state_path):
    """Import accounts from a standalone deployment's ui_state.json (plus its config file).

    Used once during migration from the separate emailproxy install; existing token caches
    are picked up as-is when credentials.cache is copied into the proxy dir."""
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"imported": 0, "error": "could not read %s: %r" % (state_path, exc)}
    legacy_dir = os.path.dirname(os.path.abspath(state_path))
    candidates = [os.path.join(legacy_dir, "legacy_emailproxy.config")]
    recorded = (st.get("settings") or {}).get("config_file")
    if recorded:
        # the recorded path points at the ORIGINAL deployment, so it is only a fallback
        candidates.append(recorded)
    conf = None
    for cand in candidates:
        if cand and os.path.exists(cand):
            conf = _load_ini(cand)
            break
    if conf is None:
        conf = configparser.ConfigParser(interpolation=None, strict=False)
    imported = 0
    warnings = []
    for email, a in (st.get("accounts") or {}).items():
        if get_account(email):
            continue
        provider = a.get("provider", "custom")
        preset = PRESETS.get(provider, PRESETS["custom"])
        sec_ok = conf.has_section(email)
        rec = {
            "email": email,
            "provider": provider,
            "password": a.get("password", ""),
            "client_id": conf.get(email, "client_id", fallback="") if sec_ok else "",
            "client_secret": conf.get(email, "client_secret", fallback="") if sec_ok else "",
            "scopes": (conf.get(email, "oauth2_scope", fallback="") if sec_ok else "")
                      or preset.get("scopes", ""),
            "auth_url": (conf.get(email, "permission_url", fallback="") if sec_ok else "")
                        or preset.get("auth_url", ""),
            "token_url": (conf.get(email, "token_url", fallback="") if sec_ok else "")
                         or preset.get("token_url", ""),
            "use_pkce": 1 if (sec_ok and conf.getboolean(email, "use_pkce", fallback=False)) else 0,
            "redirect_mode": a.get("redirect_mode", "loopback"),
            "redirect_port": a.get("redirect_port") or alloc_redirect_port(),
            "imap_local_port": a.get("imap_local_port") or preset.get("imap", {}).get("local_port") or 0,
            "smtp_local_port": a.get("smtp_local_port") or preset.get("smtp", {}).get("local_port") or 0,
        }
        if not rec.get("client_id"):
            warnings.append("%s: no client_id found (add it on the Accounts page, or copy the "
                            "legacy emailproxy.config next to the state file as "
                            "legacy_emailproxy.config and re-import)" % email)
        if not rec.get("scopes"):
            warnings.append("%s: no oauth2_scope found" % email)
        if provider == "custom":
            for section in conf.sections():
                if section.startswith("IMAP-") and int(section.rsplit("-", 1)[1]) == rec["imap_local_port"]:
                    rec["imap_host"] = conf.get(section, "server_address", fallback="")
                    rec["imap_port"] = conf.getint(section, "server_port", fallback=993)
                if section.startswith("SMTP-") and int(section.rsplit("-", 1)[1]) == rec["smtp_local_port"]:
                    rec["smtp_host"] = conf.get(section, "server_address", fallback="")
                    rec["smtp_port"] = conf.getint(section, "server_port", fallback=465)
                    rec["smtp_starttls"] = 1 if conf.getboolean(section, "server_starttls",
                                                                fallback=False) else 0
        else:
            rec["imap_host"] = preset["imap"]["host"]
            rec["imap_port"] = preset["imap"]["port"]
            rec["smtp_host"] = preset["smtp"]["host"]
            rec["smtp_port"] = preset["smtp"]["port"]
            rec["smtp_starttls"] = 1 if preset["smtp"].get("starttls") else 0
        upsert_account(rec)
        imported += 1
    return {"imported": imported, "accounts": [a["email"] for a in list_accounts()],
            "warnings": warnings}


def client_settings(account):
    """Display strings for the account card."""
    imap = "127.0.0.1:%s" % account.get("imap_local_port")
    smtp = "127.0.0.1:%s" % account.get("smtp_local_port") if account.get("smtp_local_port") else ""
    mode = account.get("redirect_mode")
    if mode == "tailnet":
        note = ("Tailnet URL — complete the login from any browser on the tailnet; the proxy "
                "receives the callback via Tailscale Serve.")
    else:
        note = ("Loopback — finish the login by copy+pasting the final browser URL back into "
                "the Authorise panel.")
    return {"imap": imap, "smtp": smtp, "redirect_uri": redirect_uri(account),
            "mode": mode, "mode_note": note, "provider_label":
            PRESETS.get(account.get("provider"), PRESETS["custom"])["label"]}
