"""Built-in web UI, JSON API and webhooks (stdlib http.server)."""
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, clients, config, store

_NONCES = {}  # one-time approval form nonce -> approval id

E = html.escape

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#2f6fde;
--warn:#b7791f;--bad:#c53030;--good:#2f855a;--dry:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--muted:#9c9b95;
--line:#33322f;--accent:#6c9cf0;--warn:#e0a84a;--bad:#f07171;--good:#6cc08f;--dry:#3a3218}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:18px;margin-right:12px}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--ink);font-weight:600}
main{max-width:1100px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.stat b{display:block;font-size:26px}.stat span{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:13px}.muted{color:var(--muted)}
.banner{background:var(--dry);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:16px}
.tag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line)}
.t-error,.t-delete{color:var(--bad)}.t-warning{color:var(--warn)}.t-action{color:var(--good)}.t-plan{color:var(--accent)}
input[type=text],input[type=number],input[type=password]{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
label{display:block;margin:10px 0 4px;font-weight:500}small{color:var(--muted)}
button,.btn{display:inline-block;padding:8px 16px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer;text-decoration:none}
button.sec{background:transparent;color:var(--accent)}button.bad{background:var(--bad);border-color:var(--bad)}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 14px;padding:4px 14px 14px}legend{padding:0 6px;color:var(--muted)}
code{font-size:13px;background:var(--bg);padding:1px 5px;border-radius:4px;word-break:break-all}
"""


def _ago(ts):
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 120:
        return f"{int(d)} s ago"
    if d < 7200:
        return f"{int(d // 60)} min ago"
    if d < 172800:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def _check_pw(pw, stored):
    if not stored or "$" not in stored:
        return False
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(_hash_pw(pw, salt), stored)


def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        server_version = f"Nextarr/{__version__}"

        def log_message(self, *a):
            pass

        # ----- helpers -----
        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str), "application/json")

        def _redirect(self, to):
            self._send(303, "", extra={"Location": to})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(n, 1_000_000)) if n else b""

        def _form(self):
            return {k: v[-1] for k, v in urllib.parse.parse_qs(self._body().decode()).items()}

        def _authed(self, cfg):
            a = cfg["auth"]
            if not a.get("enabled") or not a.get("password_hash"):
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                    return hmac.compare_digest(u, a.get("username", "")) and _check_pw(p, a["password_hash"])
                except Exception:
                    return False
            return False

        def _page(self, title, active, body):
            s = engine.summary or {}
            cfg = config.load()
            nav = "".join(f'<a href="{h}" class="{"on" if active == h else ""}">{n}</a>'
                          for h, n in [("/", "Overview"), ("/series", "Series"), ("/cleanup", "Cleanup"),
                                       ("/log", "Log"), ("/settings", "Settings")])
            banner = ""
            if not engine.configured(cfg):
                banner += ('<div class="banner"><b>Welcome to Nextarr.</b> Connect Sonarr and Tautulli under '
                           '<a href="/settings">Settings</a> (Plex and Overseerr are optional), then press '
                           '<i>Test connections</i>.</div>')
            if cfg["dry_run"]:
                banner += ('<div class="banner"><b>Dry run.</b> Nextarr only logs what it would do. '
                           'Nothing is monitored, searched or deleted. Turn it off under Settings.</div>')
            if config.paused():
                banner += '<div class="banner"><b>Paused.</b> A PAUSE file exists in the config folder.</div>'
            if engine.last_error:
                banner += f'<div class="banner"><b>Last run failed:</b> {E(engine.last_error)}</div>'
            if s.get("pending_approvals"):
                banner += (f'<div class="banner"><b>{s["pending_approvals"]} cleanup(s) wait for approval.</b> '
                           f'<a href="/cleanup">Review</a></div>')
            return self._send(200, f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nextarr · {E(title)}</title>
<style>{CSS}</style></head><body><header><b>Nextarr</b>{nav}</header><main>{banner}{body}</main></body></html>""")

        # ----- routing -----
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            u = urllib.parse.urlparse(self.path)
            path, q = u.path.rstrip("/") or "/", {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            cfg = config.load()
            try:
                if path == "/health":
                    hb = store.kv_get("heartbeat", 0)
                    ok = time.time() - hb < 3 * max(60, cfg["sync_interval_minutes"] * 60) + 600
                    return self._json({"ok": ok, "heartbeat": hb}, 200 if ok else 503)
                if path.startswith("/webhook/"):
                    return self._webhook(method, path, q, cfg)
                if path == "/api/status":
                    return self._json(self._status())
                if path.startswith("/approvals/") and q.get("token"):
                    return self._approval_link(path, q)
                if not self._authed(cfg):
                    return self._send(401, "Login required", extra={"WWW-Authenticate": 'Basic realm="Nextarr"'})
                if path == "/":
                    return self._overview()
                if path == "/series":
                    return self._series()
                if path.startswith("/series/"):
                    return self._series_detail(int(path.split("/")[2]))
                if path == "/cleanup":
                    return self._cleanup_page()
                if path == "/log":
                    return self._log(q.get("level"))
                if path == "/settings":
                    return self._settings(method, cfg)
                if path == "/api/plan":
                    return self._json(engine.plans)
                if path == "/done":  # result page after a POST, safe to refresh
                    return self._page("Done", "/settings", f'<div class="card"><b>{E(q.get("msg", ""))}</b>'
                                      f'<p><a href="/settings">Back to settings</a></p></div>')
                if path == "/actions" and method == "POST":
                    return self._actions(self._form())
                if path.startswith("/approvals/"):
                    return self._approval_page(int(path.split("/")[2]), method)
                return self._send(404, "Not found")
            except Exception as e:
                store.log(f"Web error on {path}: {e}", "error")
                return self._send(500, f"Error: {E(str(e))}")

        # ----- webhooks -----
        def _webhook(self, method, path, q, cfg):
            if method != "POST":
                return self._json({"error": "POST only"}, 405)
            token = q.get("token") or self.headers.get("X-Nextarr-Token", "")
            if not hmac.compare_digest(token, cfg["webhook_token"]):
                return self._json({"error": "bad token"}, 403)
            try:
                payload = json.loads(self._body() or b"{}")
            except ValueError:
                payload = {}
            src = path.split("/")[2]
            if src == "sonarr":
                ev = payload.get("eventType")
                if ev == "Test":
                    store.log("Sonarr test webhook received", "info")
                    return self._json({"ok": True})
                if ev == "SeriesAdd":
                    s = payload.get("series") or {}
                    store.log(f"Sonarr: series added: {s.get('title')}", "info")
                    threading.Thread(target=self._series_added, args=(s, cfg), daemon=True).start()
            elif src == "tautulli":
                store.log(f"Tautulli: watched {payload.get('title') or payload}", "info")
            engine.trigger()
            return self._json({"ok": True})

        def _series_added(self, s, cfg):
            # A series with the exclude tag is not managed: give it the full search Overseerr no longer starts.
            try:
                sonarr = clients.Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
                tags = sonarr.tags()
                full = sonarr.one_series(s["id"])
                labels = {tags.get(t, "").lower() for t in full.get("tags", [])}
                if cfg["exclude_tag"].lower() in labels:
                    if cfg["dry_run"]:
                        store.log(f"[dry run] {full['title']}: would start a full series search (excluded)", "plan")
                    else:
                        sonarr.search_series(full["id"])
                        store.log(f"{full['title']}: full series search (excluded from Nextarr)", "action")
            except Exception as e:
                store.log(f"Series-add handling failed: {e}", "error")

        # ----- pages -----
        def _status(self):
            s = engine.summary or {}
            day = time.time() - 86400
            return {"version": __version__, "dry_run": s.get("dry_run"), "last_sync": s.get("last_sync"),
                    "series": s.get("series", 0), "managed": s.get("managed", 0),
                    "episodes_kept": s.get("keep", 0), "delete_candidates": s.get("delete_candidates", 0),
                    "pending_approvals": len(store.approvals("pending")),
                    "deleted_24h": store.count_log_since("delete", day),
                    "actions_24h": store.count_log_since("action", day),
                    "hard_to_find": s.get("hard_to_find", 0), "excluded": s.get("excluded", 0)}

        def _overview(self):
            st = self._status()
            s = engine.summary or {}
            tiles = "".join(f'<div class="stat"><b>{v}</b><span>{n}</span></div>' for n, v in [
                ("managed series", st["managed"]), ("episodes kept", st["episodes_kept"]),
                ("delete candidates", st["delete_candidates"]), ("waiting approval", st["pending_approvals"]),
                ("deleted 24 h", st["deleted_24h"]), ("hard to find", st["hard_to_find"]),
                ("excluded", st["excluded"])])
            um = s.get("unmatched") or []
            um_html = (f'<div class="card"><b>Watched in Plex but not found in Sonarr</b> '
                       f'<small>({len(um)})</small><p class="muted">{E(", ".join(um))}</p></div>') if um else ""
            logs = self._log_rows(store.recent_log(15))
            return self._page("Overview", "/", f"""
<div class="card"><div class="grid">{tiles}</div>
<p class="muted">Last sync {_ago(st['last_sync'])} · last cleanup {_ago(store.kv_get('last_cleanup', 0))}</p>
<form method="post" action="/actions" style="display:flex;gap:8px;flex-wrap:wrap">
<button name="do" value="sync">Sync now</button>
<button name="do" value="cleanup" class="sec">Run cleanup now</button></form></div>
{um_html}<div class="card"><b>Recent activity</b>{logs}</div>""")

        def _series(self):
            rows = []
            order = {"managed": 0, "hard to find": 1, "excluded": 2}
            for p in sorted(engine.plans.values(), key=lambda p: (order.get(p["status"], 3), p["title"].lower())):
                vw = ", ".join(f'{E(v["name"])} {v["pos"] if v["pos"] != "-" else "(requested)"}'
                               for v in p["viewers"]) or '<span class="muted">none</span>'
                rows.append(f'<tr><td><a href="/series/{p["id"]}">{E(p["title"])}</a></td>'
                            f'<td><span class="tag">{E(p["status"])}</span></td><td>{vw}</td>'
                            f'<td>{p["files"]}/{p["episodes"]}</td><td>{len(p["keep"])}</td>'
                            f'<td>{len(p["delete"])}</td></tr>')
            return self._page("Series", "/series", f"""<div class="card"><table>
<tr><th>Series</th><th>Status</th><th>Active viewers (last watched)</th><th>Files</th><th>Keep</th><th>Delete</th></tr>
{''.join(rows) or '<tr><td colspan=6 class=muted>No sync yet</td></tr>'}</table></div>""")

        def _series_detail(self, sid):
            p = engine.plans.get(sid)
            if not p:
                return self._send(404, "Unknown series")
            vrows = "".join(f'<tr><td>{E(v["name"])}</td><td>{v["pos"]}</td><td>{", ".join(v["next"])}</td>'
                            f'<td>{"yes" if v["requested"] else ""}</td><td>{_ago(v["last"])}</td></tr>'
                            for v in p["viewers"])
            drows = "".join(f'<tr><td>{d["label"]}</td><td>{E(d["reason"])}</td></tr>' for d in p["delete"])
            inactive = ", ".join(E(x) for x in p.get("inactive_viewers", [])) or "none"
            return self._page(p["title"], "/series", f"""<div class="card"><h2>{E(p['title'])}</h2>
<p><span class="tag">{E(p['status'])}</span> · {p['files']} of {p['episodes']} episodes have a file</p>
<p><b>Always kept:</b> {E(', '.join(p['keep'])) or '-'}</p></div>
<div class="card"><b>Active viewers</b><table><tr><th>User</th><th>Last watched</th><th>Kept ahead</th>
<th>Requested</th><th>Last activity</th></tr>{vrows or '<tr><td colspan=5 class=muted>none</td></tr>'}</table>
<p class="muted">Inactive viewers (ignored): {inactive}</p></div>
<div class="card"><b>Delete candidates</b><table><tr><th>Episode</th><th>Reason</th></tr>
{drows or '<tr><td colspan=2 class=muted>none</td></tr>'}</table></div>""")

        def _cleanup_page(self):
            cfg = config.load()
            appr = store.approvals()
            arows = "".join(f'<tr><td><a href="/approvals/{a["id"]}">#{a["id"]}</a></td><td>{_ago(a["created"])}</td>'
                            f'<td>{len(a["payload"])}</td><td>{E(a["status"])}</td></tr>' for a in appr)
            cands = [dict(d, title=p["title"], series_id=p["id"]) for p in engine.plans.values() for d in p["delete"]]
            by = {}
            for c in cands:
                by.setdefault(c["title"], []).append(c)
            def avail(v):
                seasons = sorted({(x["series_id"], x["season"]) for x in v})
                res = [store.availability(sid, se)[1] for sid, se in seasons]
                ok, bad = sum(r is True for r in res), sum(r is False for r in res)
                txt = f"{ok}/{len(res)} seasons OK" + (f", {bad} hard to find" if bad else "")
                return f'<span class="{"t-warning" if bad else ""}">{txt}</span>'
            crows = "".join(f'<tr><td>{E(t)}</td><td>{len(v)}</td><td>{avail(v)}</td><td class="muted">'
                            f'{E(", ".join(sorted(x["label"] for x in v))[:300])}</td>'
                            f'<td class="muted">{E(", ".join(sorted({x["reason"] for x in v})))}</td></tr>'
                            for t, v in sorted(by.items(), key=lambda kv: -len(kv[1])))
            return self._page("Cleanup", "/cleanup", f"""
<div class="card"><b>Delete candidates now: {len(cands)}</b>
<p class="muted">Deleted nightly at {cfg['cleanup_hour']:02d}:00 after an availability check.
More than {cfg['approval_threshold']} at once needs approval.</p>
<form method="post" action="/actions" style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
<input type="number" name="n" value="20" style="width:90px"><button name="do" value="check" class="sec">Check availability now</button>
<small>Asks your indexers about the largest unchecked seasons. Deletes nothing.</small></form>
<table><tr><th>Series</th><th>Files</th><th>Availability</th><th>Episodes</th><th>Reason</th></tr>
{crows or '<tr><td colspan=5 class=muted>none</td></tr>'}</table></div>
<div class="card"><b>Approvals</b><table><tr><th>#</th><th>Created</th><th>Files</th><th>Status</th></tr>
{arows or '<tr><td colspan=4 class=muted>none</td></tr>'}</table></div>""")

        def _approval_page(self, aid, method):
            a = store.get_approval(aid)
            if not a:
                return self._send(404, "Unknown approval")
            # Every rendered form gets a one-time nonce that is consumed on use, so a re-sent old
            # form (browser refresh, back button) can never act a second time.
            here = self.path.split("&msg=")[0].split("?msg=")[0]
            if method == "POST":
                f = self._form()
                msg = "That form was out of date. Nothing was done."
                if _NONCES.pop(f.get("nonce", ""), None) == aid and a["status"] == "pending":
                    if f.get("do") == "approve":
                        store.set_approval_status(aid, "running")
                        threading.Thread(target=engine.approve, args=(aid,), daemon=True).start()
                        msg = "Approved. Deleting in the background, see the Log page."
                    elif f.get("do") == "stop":
                        engine.stop(aid)
                        msg = "Stopped. Nothing was deleted."
                sep = "&" if "?" in here else "?"
                return self._redirect(here + sep + "msg=" + urllib.parse.quote(msg))
            msg = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("msg", [""])[0]
            nonce = secrets.token_urlsafe(18)
            _NONCES[nonce] = aid
            while len(_NONCES) > 200:
                _NONCES.pop(next(iter(_NONCES)))
            rows = "".join(f'<tr><td>{E(c["title"])}</td><td>{c["label"]}</td><td>{E(c["reason"])}</td></tr>'
                           for c in a["payload"])
            buttons = ("" if a["status"] != "pending" else
                       '<form method="post" style="display:flex;gap:8px"><input type="hidden" name="nonce" value="' + nonce + '"><button name="do" value="approve">'
                       'Approve and delete</button><button name="do" value="stop" class="bad">Stop</button></form>')
            return self._page(f"Approval #{aid}", "/cleanup", f"""<div class="card"><h2>Approval #{aid}</h2>
<p>Status: <span class="tag">{E(a['status'])}</span> · {len(a['payload'])} files · expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(a['expires']))}</p>
{f'<p><b>{E(msg)}</b></p>' if msg else ''}{buttons}</div>
<div class="card"><table><tr><th>Series</th><th>Episode</th><th>Reason</th></tr>{rows}</table></div>""")

        def _approval_link(self, path, q):
            """Links from Discord: the token proves the link came from Nextarr. Shows a confirm button."""
            aid = int(path.split("/")[2])
            a = store.get_approval(aid)
            if not a or not hmac.compare_digest(q.get("token", ""), a["token"]):
                return self._send(403, "Invalid link")
            if not self._authed(config.load()):
                return self._send(401, "Login required", extra={"WWW-Authenticate": 'Basic realm="Nextarr"'})
            return self._approval_page(aid, self.command)

        def _log(self, level):
            levels = ["", "action", "delete", "plan", "warning", "error", "info"]
            nav = " · ".join(f'<a href="/log?level={l}">{l or "all"}</a>' for l in levels)
            return self._page("Log", "/log", f'<div class="card">{nav}{self._log_rows(store.recent_log(400, level or None))}</div>')

        def _log_rows(self, rows):
            return "<table>" + "".join(
                f'<tr><td class="muted" style="white-space:nowrap">{time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))}</td>'
                f'<td class="t-{r["level"]}">{E(r["level"])}</td><td>{E(r["msg"])}</td></tr>' for r in rows) + "</table>"

        def _actions(self, f):
            if f.get("do") == "sync":
                threading.Thread(target=engine.sync, args=("manual",), daemon=True).start()
            elif f.get("do") == "cleanup":
                threading.Thread(target=engine.cleanup, args=("manual",), daemon=True).start()
            elif f.get("do") == "check":
                threading.Thread(target=engine.check_availability, args=(int(f.get("n") or 20),), daemon=True).start()
                return self._redirect("/cleanup")
            elif f.get("do") == "test":
                rows = "".join(f'<tr><td>{E(n)}</td><td class="{"t-action" if ok else "t-error" if ok is False else "muted"}">'
                               f'{"OK" if ok else "failed" if ok is False else "-"}</td><td>{E(d)}</td></tr>'
                               for n, ok, d in engine.test_connections())
                return self._page("Test connections", "/settings",
                                  f'<div class="card"><b>Connection test</b><table>{rows}</table>'
                                  f'<p><a href="/settings">Back to settings</a></p></div>')
            elif f.get("do") == "restore":
                msg = engine.restore_backup()
                return self._redirect("/done?msg=" + urllib.parse.quote(msg))
            return self._redirect("/")

        def _settings(self, method, cfg):
            saved = ""
            if method == "POST":
                f = self._form()
                for svc, fields in (("sonarr", ("url", "api_key")), ("tautulli", ("url", "api_key")),
                                    ("plex", ("url", "token")), ("overseerr", ("url", "api_key"))):
                    for fld in fields:
                        v = f.get(f"{svc}.{fld}", "").strip()
                        if fld == "url" or v:  # empty secret field = keep the stored one
                            cfg[svc][fld] = v
                for k in ("episodes_ahead", "active_days", "delete_grace_days", "sync_interval_minutes",
                          "cleanup_hour", "approval_threshold", "approval_expiry_days",
                          "availability_recheck_days", "max_availability_checks_per_run"):
                    if f.get(k, "").strip().isdigit():
                        cfg[k] = int(f[k])
                for k in ("exclude_tag", "public_url"):
                    cfg[k] = f.get(k, cfg[k]).strip()
                if f.get("discord_webhook", "").strip():
                    cfg["discord_webhook"] = f["discord_webhook"].strip()
                if f.get("discord_clear"):
                    cfg["discord_webhook"] = ""
                for k in ("dry_run", "include_specials", "availability_check"):
                    cfg[k] = k in f
                cfg["plex"]["verify_ssl"] = "plex.verify_ssl" in f
                cfg["auth"]["enabled"] = "auth.enabled" in f
                cfg["auth"]["username"] = f.get("auth.username", cfg["auth"]["username"]).strip() or "admin"
                if f.get("auth.password"):
                    cfg["auth"]["password_hash"] = _hash_pw(f["auth.password"])
                if cfg["auth"]["enabled"] and not cfg["auth"]["password_hash"]:
                    cfg["auth"]["enabled"] = False
                    saved = "Login was not enabled: set a password first. "
                config.save(cfg)
                store.log("Settings changed", "info")
                engine.trigger()
                saved += "Saved."
            c = cfg

            def txt(name, val, help_="", typ="text"):
                return (f'<label>{E(name.split(".")[-1].replace("_", " ").capitalize())}</label>'
                        f'<input type="{typ}" name="{name}" value="{E(str(val))}">'
                        + (f"<small>{E(help_)}</small>" if help_ else ""))

            def secret(name, has):
                return (f'<label>{E(name.split(".")[-1].replace("_", " ").capitalize())}</label>'
                        f'<input type="password" name="{name}" placeholder="{"stored, leave empty to keep" if has else "not set"}">')

            def box(name, on, text):
                return f'<label><input type="checkbox" name="{name}" {"checked" if on else ""}> {E(text)}</label>'

            svc = "".join(f'<fieldset><legend>{n.capitalize()}</legend>{txt(n + ".url", c[n]["url"])}'
                          f'{secret(n + "." + k, bool(c[n][k]))}</fieldset>'
                          for n, k in (("sonarr", "api_key"), ("tautulli", "api_key"), ("plex", "token"),
                                       ("overseerr", "api_key")))
            base = c["public_url"].rstrip("/") or "http://<nextarr-host>:8790"
            return self._page("Settings", "/settings", f"""<form method="post">
{f'<div class="banner">{E(saved)}</div>' if saved else ''}
<div class="card"><fieldset><legend>Mode</legend>{box('dry_run', c['dry_run'], 'Dry run: only log what would happen')}</fieldset>
<fieldset><legend>Rules</legend>
{txt('episodes_ahead', c['episodes_ahead'], 'Episodes kept ready after the last watched one, per viewer', 'number')}
{txt('active_days', c['active_days'], 'A viewer counts as active this many days after their last activity', 'number')}
{txt('delete_grace_days', c['delete_grace_days'], 'Wait this many days before deleting a watched episode', 'number')}
{txt('exclude_tag', c['exclude_tag'], 'Sonarr tag that excludes a series completely')}
{box('include_specials', c['include_specials'], 'Include specials (season 0)')}</fieldset>
<fieldset><legend>Schedule and safety</legend>
{txt('sync_interval_minutes', c['sync_interval_minutes'], '', 'number')}
{txt('cleanup_hour', c['cleanup_hour'], 'Hour of the nightly cleanup (0-23)', 'number')}
{txt('approval_threshold', c['approval_threshold'], 'More deletions than this at once needs approval', 'number')}
{txt('approval_expiry_days', c['approval_expiry_days'], '', 'number')}
{box('availability_check', c['availability_check'], 'Check that a release can be found before deleting')}
{txt('availability_recheck_days', c['availability_recheck_days'], '', 'number')}
{txt('max_availability_checks_per_run', c['max_availability_checks_per_run'], 'Limits load on your indexers', 'number')}
</fieldset></div>
<div class="card">{svc}{box('plex.verify_ssl', c['plex'].get('verify_ssl', True), 'Verify the Plex certificate (turn off for https://plex:32400 inside Docker)')}</div>
<div class="card"><fieldset><legend>Notifications</legend>
{secret('discord_webhook', bool(c['discord_webhook']))}
{box('discord_clear', False, 'Remove the stored Discord webhook')}
{txt('public_url', c['public_url'], 'Address used in Discord links, e.g. https://nextarr.example.com')}</fieldset>
<fieldset><legend>Login</legend>{box('auth.enabled', c['auth']['enabled'], 'Require login for the web UI')}
{txt('auth.username', c['auth']['username'])}
<label>Password</label><input type="password" name="auth.password" placeholder="{'stored, leave empty to keep' if c['auth']['password_hash'] else 'not set'}">
</fieldset></div>
<div class="card"><b>Webhooks</b><p class="muted">Tautulli (Webhook agent, trigger "Watched", JSON body):</p>
<code>{E(base)}/webhook/tautulli?token={E(c['webhook_token'])}</code>
<p class="muted">Sonarr (Connect → Webhook, "On Series Add"):</p>
<code>{E(base)}/webhook/sonarr?token={E(c['webhook_token'])}</code></div>
<div style="display:flex;gap:8px;flex-wrap:wrap"><button>Save</button>
<button formaction="/actions" name="do" value="test" class="sec">Test connections</button></div></form>
<div class="card" style="margin-top:16px"><b>Monitoring backup</b>
<p class="muted">Saved automatically before the first live run. Restoring puts every episode's monitored
flag back as it was and turns dry run on. Files already deleted are not restored.</p>
<p>{E(os.path.basename(store.kv_get('monitor_backup') or '')) or 'No backup yet (created on the first live run).'}</p>
{'<form method="post" action="/actions" onsubmit="return confirm(\'Restore monitoring from the backup?\')"><button name="do" value="restore" class="sec">Restore monitoring</button></form>' if store.kv_get('monitor_backup') else ''}</div>""")

    return H


def serve(engine, port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(engine))
    srv.daemon_threads = True
    srv.serve_forever()
