"""Refuse requests that a web page in the user's browser could send to the local servers.

Why (security review 2026-11-08): the router (:4000), the NorthCUA tool server (:8933) and LM Studio accepted requests
sent by any website ("Origin: https://evil.example" got a 200), and the router accepted any Host name (DNS rebinding:
a malicious site can make the browser treat it as 127.0.0.1 and then also read the answers). A page could have started
browser tasks, spent web-search credits or kept the GPU busy.

Rule: the Host must be this machine (127.0.0.1 / localhost, plus names a server explicitly allows, such as the tailnet
name behind `tailscale serve`); a request that carries an Origin header (i.e. sent by a web page) must come from Open
WebUI's own addresses. Server-side clients (Open WebUI's backend, Open Code, Claude Code, the NorthCUA harness, the
router's own tools) send no Origin header and keep working.
"""

LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8282",               # Open WebUI on the PC
    "http://localhost:8282",
    "https://<pc-name>.<tailnet>.ts.net",   # Open WebUI through Tailscale (phone)
}


def _hostname(host):
    host = (host or "").strip().lower()
    if host.startswith("["):                  # [::1]:4000
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def refuse_reason(headers, extra_hostnames=()):
    """None when the request may proceed, else a short reason. `headers` is an http.server message object."""
    host = headers.get("Host")
    if _hostname(host) not in LOCAL_HOSTNAMES and _hostname(host) not in {h.lower() for h in extra_hostnames}:
        return f"host {host!r} is not this machine"
    origin = headers.get("Origin")
    if origin is not None and origin.strip().lower().rstrip("/") not in ALLOWED_ORIGINS:
        return f"requests from web pages ({origin!r}) are not accepted"
    return None
