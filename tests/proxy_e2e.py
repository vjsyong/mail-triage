#!/usr/bin/env python3
"""End-to-end test for the embedded email-oauth2-proxy integration.

Runs the REAL emailproxy package as a child process (exactly like app.py does) against a
mock OAuth provider and a mock upstream IMAP server (self-signed TLS), and drives the
whole flow through proxy.py the way the Accounts page does:

  1. account row -> generated emailproxy.config -> manager starts the proxy
  2. start_auth triggers a login and captures the provider URL from the proxy log
  3. paste-back completion replays the callback to the proxy's one-shot listener
  4. token exchange -> tokens cached (encrypted) -> XOAUTH2 reaches the upstream server
  5. second login reuses the cached token (no new authorisation)
  6. forced expiry + manager restart -> refresh_token grant
  7. wrong local password -> clean failure, tokens preserved
  8. reset_tokens clears the cached tokens
  9. paste-back rejects a URL without a code parameter

Fully offline. Usage:  .venv/bin/python tests/proxy_e2e.py
"""
import base64
import configparser
import datetime
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

EMAIL = "test.user@example.com"
PASSWORD = "test-password-123"
CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
AUTH_CODE = "MOCK-CODE-1"

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  \u2713 %s" % name)
    else:
        failed += 1
        print("  \u2717 FAIL: %s %s" % (name, ("-- %s" % detail) if detail else ""))


def pick_free_port(lo=42100, hi=43000):
    """The proxy's callback receiver binds without SO_REUSEADDR, so a recent run's
    TIME_WAIT entry would block a fixed port; use a fresh random free port per run."""
    import random
    for _ in range(300):
        port = random.randint(lo, hi)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            try:
                s.close()
            except OSError:
                pass
    raise RuntimeError("no free port found")


# ----------------------------------------------------------------- mock OAuth provider

class Provider(BaseHTTPRequestHandler):
    server_version = "mock-oauth/1.0"
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Provider.requests.append(("GET", self.path, {}))
        body = b"<html><body>mock authorize endpoint</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        params = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        Provider.requests.append(("POST", self.path, params))
        if self.path == "/token":
            if params.get("grant_type") == "authorization_code":
                payload = {"access_token": "ACCESS-TOKEN-1", "expires_in": 3600,
                           "refresh_token": "REFRESH-TOKEN-1", "token_type": "Bearer"}
            elif params.get("grant_type") == "refresh_token":
                payload = {"access_token": "ACCESS-TOKEN-2", "expires_in": 3600,
                           "token_type": "Bearer"}
            else:
                payload = {"error": "unsupported_grant_type"}
        else:
            payload = {"error": "not_found"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ----------------------------------------------------------------- mock upstream IMAP (implicit TLS)

class MockIMAP(threading.Thread):
    def __init__(self, ctx, port):
        super().__init__(daemon=True)
        self.ctx = ctx
        self.auths = []
        self.closed = False
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(8)

    def run(self):
        while not self.closed:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        s = None
        try:
            s = self.ctx.wrap_socket(conn, server_side=True)
            s.sendall(b"* OK [CAPABILITY IMAP4rev1 AUTH=XOAUTH2] mock ready\r\n")
            f = s.makefile("rb")
            pending_tag = None
            while True:
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                if pending_tag:
                    try:
                        decoded = base64.b64decode(line + b"=" * (-len(line) % 4)).decode(
                            "utf-8", "replace")
                    except Exception as exc:  # noqa: BLE001
                        decoded = "decode-error: %r" % exc
                    self.auths.append((pending_tag, decoded))
                    s.sendall(("%s OK [CAPABILITY IMAP4rev1] logged in\r\n"
                               % pending_tag).encode())
                    pending_tag = None
                    continue
                m = re.match(rb"(\S+) AUTHENTICATE XOAUTH2\s+(\S+)\s*$", line)
                if m:
                    tag = m.group(1).decode()
                    try:
                        decoded = base64.b64decode(
                            m.group(2) + b"=" * (-len(m.group(2)) % 4)).decode("utf-8", "replace")
                    except Exception as exc:  # noqa: BLE001
                        decoded = "decode-error: %r" % exc
                    self.auths.append((tag, decoded))
                    s.sendall(("%s OK [CAPABILITY IMAP4rev1] logged in\r\n" % tag).encode())
                    continue
                m = re.match(rb"(\S+) AUTHENTICATE XOAUTH2\s*$", line)
                if m:
                    pending_tag = m.group(1).decode()
                    continue
                m = re.match(rb"(\S+) LOGOUT", line)
                if m:
                    s.sendall(b"* BYE\r\n" + m.group(1) + b" OK done\r\n")
                    break
        except (ssl.SSLError, OSError):
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            try:
                if s is not None:
                    s.close()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self):
        self.closed = True
        try:
            self.srv.close()
        except OSError:
            pass


def make_cert(path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mock-imap")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path = os.path.join(path, "mock-imap-cert.pem")
    key_path = os.path.join(path, "mock-imap-key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return cert_path, key_path


def login(email, password, port, wait_ok=60.0, greet_timeout=20.0):
    """Send an IMAP login the way a client (or the auth trigger) would: connect, wait for
    the greeting relayed by the proxy, then send the login command."""
    connect_deadline = time.time() + 15
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=15)
            break
        except OSError:
            if time.time() > connect_deadline:
                raise
            time.sleep(0.4)
    try:
        s.settimeout(1.0)
        buf = b""
        greet_deadline = time.time() + greet_timeout
        while time.time() < greet_deadline:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            if b"* OK" in buf:
                break
        if b"* OK" not in buf:
            return "NO GREETING: %r" % buf, buf
        s.sendall(("a1 login %s %s\r\n" % (email, password)).encode())
        deadline = time.time() + wait_ok
        while time.time() < deadline:
            s.settimeout(max(0.2, min(2.0, deadline - time.time())))
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                break
            buf += d
            m = re.search(rb"(?m)^a1 (OK|NO|BAD)[^\r\n]*", buf)
            if m:
                return m.group(0).decode("utf-8", "replace"), buf
        return None, buf
    finally:
        s.close()


def main():
    tmp = tempfile.mkdtemp(prefix="mail-triage-proxy-")
    os.environ["DATA_DIR"] = tmp
    sys.path.insert(0, PROJECT)
    import store
    import proxy

    store.init_db()
    store.set_setting("proxy_mode", "embedded")

    provider_port = pick_free_port()
    upstream_port = pick_free_port()
    local_port = pick_free_port()
    redirect_port = pick_free_port()
    print("workdir: %s | provider:%d upstream:%d local:%d redirect:%d"
          % (tmp, provider_port, upstream_port, local_port, redirect_port))

    cert_path, key_path = make_cert(tmp)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    imap = MockIMAP(ctx, upstream_port)
    imap.start()
    provider = ThreadingHTTPServer(("127.0.0.1", provider_port), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()

    # the proxy child process must trust the mock server's self-signed cert
    os.environ["SSL_CERT_FILE"] = cert_path

    rec, err = proxy.account_from_form({
        "provider": "custom", "email": EMAIL, "password": PASSWORD,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_mode": "loopback",
        "permission_url": "http://127.0.0.1:%d/authorize" % provider_port,
        "token_url": "http://127.0.0.1:%d/token" % provider_port,
        "scope": "mock.mail.scope",
        "imap_host": "127.0.0.1", "imap_port": str(upstream_port),
        "smtp_host": "", "smtp_port": "",
    }, existing=None)
    if rec:
        rec["redirect_port"] = redirect_port
        rec["imap_local_port"] = local_port
        proxy.upsert_account(rec)
    check("custom account record built from the form", rec is not None and not err, str(err))

    cfg = proxy.config_text()
    check("generated config carries the account + listener",
          ("[%s]" % EMAIL) in cfg and ("[IMAP-%d]" % local_port) in cfg
          and ("redirect_uri = http://localhost:%d" % redirect_port) in cfg)
    check("generated config points upstream at the mock server",
          ("server_address = 127.0.0.1" in cfg) and ("server_port = %d" % upstream_port) in cfg)

    try:
        ok, merr = proxy.manager.start()
        check("manager starts the embedded proxy", ok and not merr, str(merr))
        check("manager reports running with a live listener",
              proxy.manager.running()
              and proxy.manager.status()["ports"].get(local_port) is True)

        # --- 1. start_auth triggers a login and captures the provider URL ---------
        ok, err = proxy.start_auth(EMAIL)
        check("start_auth accepted", ok and not err, str(err))
        url = None
        deadline = time.time() + 60
        while time.time() < deadline:
            st = proxy.auth_state(EMAIL)
            if st.get("url"):
                url = st["url"]
                break
            if st.get("status") in ("failed", "timeout"):
                break
            time.sleep(0.4)
        check("authorisation URL captured from the proxy log", bool(url),
              str(proxy.auth_state(EMAIL).get("message")))
        params = {}
        if url:
            parsed = urllib.parse.urlparse(url)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            check("URL points at the mock provider",
                  parsed.netloc == "127.0.0.1:%d" % provider_port, url)
            check("URL carries client_id / scope / redirect_uri",
                  params.get("client_id") == CLIENT_ID
                  and params.get("scope") == "mock.mail.scope"
                  and params.get("redirect_uri") == "http://localhost:%d" % redirect_port, url)

        # --- 2. paste-back completion (what the Authorise panel does) -------------
        pasted = "https://localhost:%d/?code=%s&state=%s" % (
            redirect_port, AUTH_CODE, params.get("state", "s"))
        ok, msg = proxy.complete_auth(EMAIL, pasted)
        check("paste-back completion accepted the pasted URL", ok, msg)
        st = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            st = proxy.auth_state(EMAIL)
            if st.get("status") in ("success", "failed", "timeout"):
                break
            time.sleep(0.4)
        check("authorisation reported success", st.get("status") == "success",
              str(st.get("message")))

        # --- 3. token exchange + XOAUTH2 ------------------------------------------
        token_posts = [r for r in Provider.requests if r[0] == "POST" and r[1] == "/token"]
        code_post = token_posts[0][2] if token_posts else {}
        check("token endpoint called with authorization_code grant",
              code_post.get("grant_type") == "authorization_code"
              and code_post.get("code") == AUTH_CODE, str(code_post))
        check("token request carries client credentials and redirect_uri",
              code_post.get("client_id") == CLIENT_ID
              and code_post.get("client_secret") == CLIENT_SECRET
              and code_post.get("redirect_uri") == "http://localhost:%d" % redirect_port,
              str(code_post))
        check("upstream server received XOAUTH2 with the access token",
              any("auth=Bearer ACCESS-TOKEN-1" in a[1] for a in list(imap.auths)),
              str(imap.auths))
        cache = configparser.ConfigParser(interpolation=None)
        cache.read(proxy.cache_path())
        check("tokens cached (encrypted, not plaintext)",
              cache.has_section(EMAIL)
              and cache.get(EMAIL, "access_token", fallback="") not in ("", "ACCESS-TOKEN-1")
              and bool(cache.get(EMAIL, "refresh_token", fallback="")),
              "sections: %s" % cache.sections())
        tok = proxy.token_status(EMAIL)
        check("token_status reports authorised", tok["authorized"] and tok["refresh_token"],
              str(tok))

        # --- 4. token reuse --------------------------------------------------------
        line, _ = login(EMAIL, PASSWORD, local_port, wait_ok=30)
        check("second login succeeds using the cached token",
              bool(line) and line.startswith("a1 OK"), str(line))
        logtext = open(proxy.log_path(), encoding="utf-8", errors="replace").read()
        check("no second authorisation request was raised",
              logtext.count("Please visit the following URL") == 1,
              "visits=%d" % logtext.count("Please visit the following URL"))

        # --- 5. forced expiry + manager restart -> refresh grant -------------------
        def force_expiry():
            c = configparser.ConfigParser(interpolation=None)
            c.read(proxy.cache_path())
            c.set(EMAIL, "access_token_expiry", "1")
            with open(proxy.cache_path(), "w", encoding="utf-8") as fh:
                c.write(fh)

        ok, err = proxy.manager.apply(edit_fn=force_expiry)
        check("manager restarts cleanly for a stopped-edit", ok, str(err))
        line, _ = login(EMAIL, PASSWORD, local_port, wait_ok=30)
        check("login after forced expiry succeeds", bool(line) and line.startswith("a1 OK"),
              str(line))
        refresh_posts = [r for r in Provider.requests
                         if r[0] == "POST" and r[2].get("grant_type") == "refresh_token"]
        check("refresh_token grant used after expiry", len(refresh_posts) >= 1,
              str(refresh_posts))
        check("new access token sent upstream after refresh",
              any("auth=Bearer ACCESS-TOKEN-2" in a[1] for a in list(imap.auths)),
              str(list(imap.auths)[-2:]))

        # --- 6. wrong local password ----------------------------------------------
        line, _ = login(EMAIL, "WRONG-PASSWORD", local_port, wait_ok=20)
        check("wrong password is rejected cleanly",
              bool(line) and line.startswith("a1 NO") and "incorrect" in line.lower(), str(line))
        cache = configparser.ConfigParser(interpolation=None)
        cache.read(proxy.cache_path())
        check("tokens preserved after wrong-password attempt",
              bool(cache.get(EMAIL, "refresh_token", fallback="")))

        # --- 7. paste-back validation ----------------------------------------------
        ok, msg = proxy.complete_auth(EMAIL, "https://localhost:%d/no-code-here" % redirect_port)
        check("paste-back rejects a URL without a code parameter",
              (not ok) and "code" in (msg or "").lower(), msg)

        # --- 8. reset tokens ---------------------------------------------------------
        ok, err = proxy.reset_tokens(EMAIL)
        check("reset_tokens clears the cached tokens",
              ok and not proxy.token_status(EMAIL)["authorized"], str(err))
    finally:
        proxy.manager.stop()
        imap.shutdown()
        provider.shutdown()

    print("\n%s\n%d passed, %d failed (workspace: %s)\n"
          % ("ALL PASS" if failed == 0 else "FAILURES PRESENT", passed, failed, tmp))
    if failed == 0:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
