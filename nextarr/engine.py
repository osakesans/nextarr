"""The rules: who watches what, which episodes to keep, monitor, search or delete."""
import datetime as dt
import json
import os
import re
import secrets
import threading
import time

from . import clients, config, store

DAY = 86400
SEARCH_COOLDOWN = 12 * 3600

# Rejections that only exist because we still have the file (or it is unmonitored).
# A release rejected for these reasons alone could be downloaded again after deletion.
_IGNORABLE = re.compile(r"existing file|meets cutoff|not an upgrade|is not monitored|not monitored|"
                        r"upgrades? (are )?not allowed|already (been )?(imported|grabbed)|in queue", re.I)


def usable_release(r, min_score=None):
    """True when Sonarr would grab this release once our file is gone, and it is at least
    as good (custom format score, e.g. language) as the file we delete."""
    if min_score is not None and (r.get("customFormatScore") or 0) < min_score:
        return False
    return all(_IGNORABLE.search(x) for x in r.get("rejections") or [])


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _ts(iso):
    if not iso:
        return 0
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _key(e):
    return (e["seasonNumber"], e["episodeNumber"])


def _label(k):
    return f"S{k[0]:02d}E{k[1]:02d}" if k else "-"


def _set_start(v, season, ts):
    """Remember the newest requested start season for a viewer."""
    if ts >= v.get("start_ts", 0):
        v["start"], v["start_ts"] = season, ts


def _effective_pos(v, eps):
    """Where the viewer's window starts from.

    A start season counts only while it is newer than the viewer's last finished episode,
    so an old request never pulls someone back once they are watching.
    """
    start = v.get("start")
    if start is None or v.get("start_ts", 0) <= max(v["watched"].values(), default=0):
        return v["pos"]
    if not any(e["seasonNumber"] == start for e in eps):
        return v["pos"]
    if v["pos"] is None and start <= eps[0]["seasonNumber"]:
        return None  # a request from the first season is the normal S01E01 + n
    if v["pos"] is not None and start <= v["pos"][0]:
        return v["pos"]  # never pulled back to a season they have already reached
    return (start, 0)


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.plans = {}          # series id -> plan dict (for the web UI)
        self.summary = store.kv_get("summary", {})
        self.preview = store.kv_get("cleanup_preview", [])
        self.last_error = None
        self._wake = threading.Event()
        self._last_plan_msg = {}  # series id -> last dry-run message, to log changes only

    # ---------- clients ----------
    def _clients(self, cfg):
        s = clients.Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
        t = clients.Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["api_key"])
        p = clients.Plex(cfg["plex"]["url"], cfg["plex"]["token"], cfg["plex"].get("verify_ssl", True)) if cfg["plex"]["token"] else None
        o = (clients.Overseerr(cfg["overseerr"]["url"], cfg["overseerr"]["api_key"])
             if cfg["overseerr"]["url"] and cfg["overseerr"]["api_key"] else None)
        return s, t, p, o

    # ---------- viewers ----------
    def _viewers(self, cfg, series_list, tautulli, plex, overseerr, now):
        """tvdb id -> {user(lower): {name, pos, last, watched{key: ts}, requested}}"""
        by_title = {}
        for s in series_list:
            for t in [s["title"], f"{s['title']} ({s.get('year')})"] + \
                     [a.get("title") for a in s.get("alternateTitles", [])]:
                by_title.setdefault(_norm(t), s["tvdbId"])
        rk_map = plex.show_tvdb_map() if plex else {}
        after = dt.date.fromtimestamp(now - cfg["active_days"] * DAY).isoformat()
        rows = tautulli.episode_history(after)
        viewers, unmatched = {}, set()
        for r in rows:
            tvdb = rk_map.get(int(r.get("grandparent_rating_key") or 0)) or \
                by_title.get(_norm(r.get("grandparent_title")))
            if not tvdb:
                unmatched.add(r.get("grandparent_title"))
                continue
            name = r.get("user") or "?"
            v = viewers.setdefault(tvdb, {}).setdefault(
                name.lower(), {"name": name, "pos": None, "last": 0, "watched": {}, "requested": False})
            v["last"] = max(v["last"], r.get("date") or 0)
            if r.get("watched_status") == 1 and r.get("parent_media_index") is not None \
                    and r.get("media_index") is not None:
                k = (int(r["parent_media_index"]), int(r["media_index"]))
                if k[0] == 0 and not cfg["include_specials"]:
                    continue
                v["watched"][k] = max(v["watched"].get(k, 0), r.get("date") or 0)
                if v["pos"] is None or k > v["pos"]:
                    v["pos"] = k
        if overseerr:
            try:
                for q in overseerr.tv_requests():
                    created = _ts(q["created"])
                    if not q["tvdb_id"] or not q["user"] or created < now - cfg["active_days"] * DAY:
                        continue
                    v = viewers.setdefault(q["tvdb_id"], {}).setdefault(
                        q["user"].lower(), {"name": q["user"], "pos": None, "last": 0,
                                            "watched": {}, "requested": False})
                    v["requested"] = True
                    v["last"] = max(v["last"], created)
                    seasons = [s for s in q.get("seasons") or [] if s]
                    if seasons:
                        _set_start(v, min(seasons), created)
            except clients.ApiError as e:
                store.log(f"Overseerr unavailable, requests ignored this run: {e}", "warning")
        # A start set by hand in the web UI, for a season Overseerr refuses to request
        # (it does not allow seasons that are already partly available).
        for st in store.viewer_starts():
            if st["ts"] < now - cfg["active_days"] * DAY:
                continue
            v = viewers.setdefault(st["tvdb_id"], {}).setdefault(
                st["user"], {"name": st["name"], "pos": None, "last": 0, "watched": {}, "requested": False})
            v["last"] = max(v["last"], st["ts"])
            _set_start(v, st["season"], st["ts"])
        return viewers, unmatched

    # ---------- per-series plan ----------
    def plan_series(self, cfg, series, episodes, viewers, now, exclude_tag_id, unavailable):
        plan = {"id": series["id"], "title": series["title"], "tvdb": series["tvdbId"],
                "status": "managed", "viewers": [], "keep": [], "monitor_on": [], "monitor_off": [],
                "search": [], "delete": [], "files": 0, "episodes": 0, "series_fix": False}
        if exclude_tag_id is not None and exclude_tag_id in series.get("tags", []):
            plan["status"] = "excluded"
            return plan
        if not series.get("monitored"):
            plan["status"] = "series unmonitored"
            return plan
        eps = sorted((e for e in episodes if cfg["include_specials"] or e["seasonNumber"] > 0), key=_key)
        if not eps:
            plan["status"] = "no episodes"
            return plan
        n = int(cfg["episodes_ahead"])
        grace = cfg["delete_grace_days"] * DAY
        all_v = list(viewers.values())
        active = [v for v in all_v if v["last"] >= now - cfg["active_days"] * DAY]
        plan["episodes"] = len(eps)
        plan["files"] = sum(1 for e in eps if e.get("hasFile"))
        first = eps[0]
        keep = {first["id"]}
        ends, next_seasons, positions = [], set(), []
        for v in active:
            pos = _effective_pos(v, eps)
            positions.append(pos)
            if pos is None:
                window = eps[:1 + n]
            else:
                i = sum(1 for e in eps if _key(e) <= pos)
                window = eps[i:i + n]
            keep.update(e["id"] for e in window)
            if window:
                ends.append(_key(window[-1]))
                next_seasons.add(window[0]["seasonNumber"])
            else:
                ends.append(pos)
            plan["viewers"].append({"name": v["name"], "pos": _label(v["pos"]),
                                    "next": [_label(_key(e)) for e in window],
                                    "requested": v["requested"],
                                    "start": v.get("start") if pos != v["pos"] else None,
                                    "last": v["last"]})
        plan["inactive_viewers"] = [v["name"] for v in all_v if v not in active]
        min_pos = None
        if active and all(p is not None for p in positions):
            min_pos = min(positions)
        max_end = max(ends) if ends else None
        last_activity = max((v["last"] for v in all_v), default=0)

        for e in eps:
            k = _key(e)
            aired = _ts(e.get("airDateUtc")) and _ts(e.get("airDateUtc")) <= now
            if e["id"] in keep:
                plan["keep"].append(_label(k))
                if not e.get("monitored"):
                    plan["monitor_on"].append(e["id"])
                if not e.get("hasFile") and aired and now - store.last_search(e["id"]) > SEARCH_COOLDOWN:
                    plan["search"].append(e["id"])
                continue
            if e.get("monitored"):
                plan["monitor_off"].append(e["id"])
            if not e.get("hasFile") or e["id"] == first["id"]:
                continue
            added = _ts((e.get("episodeFile") or {}).get("dateAdded"))
            reason = None
            if not active:
                if now - last_activity >= grace and now - added >= grace:
                    reason = "no active viewers"
            elif min_pos is not None and k <= min_pos:
                # When did each viewer pass this episode? Their watch of it, or else their
                # first watch of anything later (they skipped ahead).
                passed = max(v["watched"].get(k) or min((t for wk, t in v["watched"].items() if wk > k),
                                                        default=v["last"]) for v in active)
                if now - passed >= grace:
                    reason = "watched by all active viewers"
            elif max_end is not None and k > max_end:
                if e["seasonNumber"] in next_seasons:
                    continue  # season pack in the season someone is watching: keep it
                if now - added >= grace:
                    reason = "ahead of every viewer"
            if reason:
                plan["delete"].append({"episode_id": e["id"], "file_id": e["episodeFileId"],
                                       "label": _label(k), "season": e["seasonNumber"], "reason": reason,
                                       "score": (e.get("episodeFile") or {}).get("customFormatScore")})
        # A multi-episode file is only deleted when every episode in it is a candidate.
        doomed = {d["episode_id"] for d in plan["delete"]}
        shared = {e["episodeFileId"] for e in episodes
                  if e.get("hasFile") and e["id"] not in doomed}
        plan["delete"] = [d for d in plan["delete"] if d["file_id"] not in shared]
        if series["id"] in unavailable and plan["delete"]:
            plan["status"] = "hard to find"
            plan["held"] = len(plan["delete"])
            plan["delete"] = []
        plan["series_fix"] = series.get("monitorNewItems", "all") != "none"
        return plan

    # ---------- sync ----------
    def sync(self, reason="schedule"):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._sync(reason)
            self.last_error = None
        except Exception as e:  # keep the loop alive, show the error in the UI
            self.last_error = str(e)
            store.log(f"Sync failed: {e}", "error")
        finally:
            self.lock.release()

    def configured(self, cfg=None):
        cfg = cfg or config.load()
        return bool(cfg["sonarr"]["url"] and cfg["sonarr"]["api_key"]
                    and cfg["tautulli"]["url"] and cfg["tautulli"]["api_key"])

    def test_connections(self):
        """Try every configured service once; returns [(name, ok, detail)]."""
        cfg = config.load()
        sonarr, tautulli, plex, overseerr = self._clients(cfg)
        out = []
        for name, fn in (("Sonarr", lambda: f"version {sonarr.status().get('version')}"),
                         ("Tautulli", lambda: f"{len(tautulli.call('get_users') or [])} users"),
                         ("Plex", (lambda: f"{len(plex.show_tvdb_map())} shows") if plex else None),
                         ("Overseerr", (lambda: f"{len(overseerr.tv_requests(take=20))} recent TV requests")
                          if overseerr else None)):
            if fn is None:
                out.append((name, None, "not configured (optional)"))
                continue
            try:
                out.append((name, True, fn()))
            except Exception as e:
                out.append((name, False, str(e)[:200]))
        return out

    def _sync(self, reason):
        cfg = config.load()
        if not self.configured(cfg):
            self.summary = {"last_sync": None, "setup_needed": True}
            store.kv_set("heartbeat", time.time())
            return
        if config.paused():
            store.log("PAUSE file present: sync skipped", "warning")
            return
        sonarr, tautulli, plex, overseerr = self._clients(cfg)
        now = time.time()
        series_list = sonarr.series()
        tags = sonarr.tags()
        tag_id = next((i for i, l in tags.items() if l.lower() == cfg["exclude_tag"].lower()), None)
        viewers, unmatched = self._viewers(cfg, series_list, tautulli, plex, overseerr, now)
        unavailable = store.unavailable_series()
        plans, dry = {}, cfg["dry_run"]
        episode_cache = {s["id"]: sonarr.episodes(s["id"]) for s in series_list}
        if not dry and not store.kv_get("monitor_backup"):
            # First live run: remember how Sonarr was set up before Nextarr changes anything.
            self._write_backup(series_list, episode_cache)
        totals = {"series": 0, "managed": 0, "keep": 0, "monitor_on": 0, "monitor_off": 0,
                  "search": 0, "delete_candidates": 0, "hard_to_find": 0, "excluded": 0}
        for s in series_list:
            eps = episode_cache[s["id"]]
            if any(e.get("hasFile") for e in eps):
                files = {f["id"]: f for f in sonarr.get("/episodefile", seriesId=s["id"])}
                for e in eps:
                    if e.get("hasFile"):
                        e["episodeFile"] = files.get(e.get("episodeFileId"), {})
            p = self.plan_series(cfg, s, eps, viewers.get(s["tvdbId"], {}), now, tag_id, unavailable)
            plans[s["id"]] = p
            totals["series"] += 1
            if p["status"] in ("managed", "hard to find"):
                totals["managed"] += 1
                totals["keep"] += len(p["keep"])
                for k in ("monitor_on", "monitor_off", "search"):
                    totals[k] += len(p[k])
                totals["delete_candidates"] += len(p["delete"])
            totals["hard_to_find"] += p["status"] == "hard to find"
            totals["excluded"] += p["status"] == "excluded"
            if p["status"] not in ("managed", "hard to find"):
                continue
            changes = []
            if p["series_fix"]:
                changes.append("stop auto-monitoring new episodes")
            if p["monitor_on"]:
                changes.append(f"monitor {len(p['monitor_on'])}")
            if p["monitor_off"]:
                changes.append(f"unmonitor {len(p['monitor_off'])}")
            if p["search"]:
                changes.append(f"search {len(p['search'])}")
            if not changes:
                continue
            if dry:
                msg = f"[dry run] {s['title']}: would {', '.join(changes)}"
                if self._last_plan_msg.get(s["id"]) != msg:
                    self._last_plan_msg[s["id"]] = msg
                    store.log(msg, "plan")
                continue
            if p["series_fix"]:
                full = sonarr.one_series(s["id"])
                full["monitorNewItems"] = "none"
                sonarr.update_series(full)
            sonarr.set_monitored(p["monitor_on"], True)
            sonarr.set_monitored(p["monitor_off"], False)
            sonarr.search_episodes(p["search"])
            store.mark_searched(p["search"])
            store.log(f"{s['title']}: {', '.join(changes)}", "action")
        self.plans = plans
        self.summary = {"last_sync": now, "reason": reason, "dry_run": dry, **totals,
                        "unmatched": sorted(x for x in unmatched if x)[:50],
                        "pending_approvals": len(store.approvals("pending"))}
        store.kv_set("summary", self.summary)
        store.kv_set("heartbeat", now)

    # ---------- availability check without deleting ----------
    def check_availability(self, limit):
        """Check up to `limit` unchecked seasons (largest first). Never deletes; works in dry run."""
        cfg = config.load()
        sonarr = self._clients(cfg)[0]
        groups = {}
        for p in self.plans.values():
            for d in p["delete"]:
                groups.setdefault((p["id"], d["season"]), []).append(dict(d, title=p["title"]))
        now, done = time.time(), []
        for (sid, season), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(done) >= limit:
                break
            ts, ok = store.availability(sid, season)
            if ok is not None and now - ts < cfg["availability_recheck_days"] * DAY:
                continue
            try:
                rel = sonarr.releases(items[0]["episode_id"])
            except clients.ApiError as e:
                store.log(f"Availability check failed for {items[0]['title']} S{season:02d}: {e}", "warning")
                continue
            scores = [c["score"] for c in items if c.get("score") is not None]
            ok = any(usable_release(r, max(scores) if scores else None) for r in rel)
            store.set_availability(sid, season, ok)
            done.append((items[0]["title"], season, len(items), ok, sum(usable_release(r) for r in rel)))
            store.log(f"Availability {items[0]['title']} S{season:02d} ({len(items)} files): "
                      f"{'OK' if ok else 'HARD TO FIND'}", "info" if ok else "warning")
        return done

    # ---------- monitoring backup ----------
    def _write_backup(self, series_list, episode_cache):
        data = {"created": time.time(), "series": {
            str(s["id"]): {"title": s["title"], "monitorNewItems": s.get("monitorNewItems", "all"),
                           "monitored": [e["id"] for e in episode_cache[s["id"]] if e.get("monitored")],
                           "unmonitored": [e["id"] for e in episode_cache[s["id"]] if not e.get("monitored")]}
            for s in series_list}}
        path = os.path.join(config.CONFIG_DIR, f"backup-monitoring-{time.strftime('%Y-%m-%d')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        store.kv_set("monitor_backup", path)
        store.log(f"Saved Sonarr monitoring backup before the first live run: {os.path.basename(path)}", "action")

    def restore_backup(self):
        """Put every episode's monitored flag and each series' new-episode setting back."""
        path = store.kv_get("monitor_backup")
        if not path or not os.path.exists(path):
            return "No backup found."
        cfg = config.load()
        sonarr = self._clients(cfg)[0]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        with self.lock:
            existing = {s["id"] for s in sonarr.series()}
            n = 0
            for sid, b in data["series"].items():
                if int(sid) not in existing:
                    continue
                sonarr.set_monitored(b["monitored"], True)
                sonarr.set_monitored(b["unmonitored"], False)
                full = sonarr.one_series(int(sid))
                if full.get("monitorNewItems") != b["monitorNewItems"]:
                    full["monitorNewItems"] = b["monitorNewItems"]
                    sonarr.update_series(full)
                n += 1
            cfg["dry_run"] = True
            config.save(cfg)
        store.log(f"Restored monitoring for {n} series from {os.path.basename(path)}; dry run turned on", "action")
        return f"Restored {n} series. Dry run is on again."

    # ---------- cleanup ----------
    def cleanup(self, reason="schedule"):
        if not self.lock.acquire(blocking=True, timeout=600):
            return
        try:
            self._cleanup(reason)
        except Exception as e:
            self.last_error = str(e)
            store.log(f"Cleanup failed: {e}", "error")
        finally:
            self.lock.release()

    def _cleanup(self, reason):
        cfg = config.load()
        if config.paused():
            store.log("PAUSE file present: cleanup skipped", "warning")
            return
        sonarr = self._clients(cfg)[0]
        now = time.time()
        store.kv_set("last_cleanup", now)
        cands = [dict(d, series_id=p["id"], title=p["title"])
                 for p in self.plans.values() for d in p["delete"]]
        ready, deferred, checks = [], 0, 0
        if cfg["availability_check"]:
            groups = {}
            for c in cands:
                groups.setdefault((c["series_id"], c["season"]), []).append(c)
            for (sid, season), items in groups.items():
                ts, ok = store.availability(sid, season)
                if ok is None or now - ts > cfg["availability_recheck_days"] * DAY:
                    if checks >= cfg["max_availability_checks_per_run"] or cfg["dry_run"]:
                        deferred += len(items)
                        continue
                    checks += 1
                    try:
                        rel = sonarr.releases(items[0]["episode_id"])
                    except clients.ApiError as e:
                        store.log(f"Availability check failed for {items[0]['title']}: {e}", "warning")
                        deferred += len(items)
                        continue
                    scores = [c["score"] for c in items if c.get("score") is not None]
                    ok = any(usable_release(r, max(scores) if scores else None) for r in rel)
                    store.set_availability(sid, season, ok)
                    if not ok:
                        msg = (f"Nextarr: **{items[0]['title']}** season {season} looks hard to find "
                               f"(no usable release right now). No episodes of this series will be deleted.")
                        store.log(msg, "warning")
                        self._notify(cfg, msg)
                if ok:
                    ready.extend(items)
        else:
            ready = cands
        if cfg["dry_run"]:
            self.preview = cands
            store.kv_set("cleanup_preview", cands)
            store.log(f"[dry run] cleanup: {len(cands)} files would be considered for deletion "
                      f"(availability not checked in dry run)", "plan")
            return
        if not ready:
            store.log(f"Cleanup: nothing to delete ({deferred} deferred to a later run)", "info")
            return
        if len(ready) > cfg["approval_threshold"]:
            pending = store.approvals("pending")
            ids = sorted(c["file_id"] for c in ready)
            if any(sorted(c["file_id"] for c in a["payload"]) == ids for a in pending):
                return
            token = secrets.token_urlsafe(16)
            aid = store.create_approval(token, ready, cfg["approval_expiry_days"])
            store.log(f"Cleanup of {len(ready)} files waits for approval #{aid}", "warning")
            base = cfg["public_url"].rstrip("/")
            series = sorted({c["title"] for c in ready})
            self._notify(cfg, (f"Nextarr wants to delete **{len(ready)} episodes** from {len(series)} series "
                               f"({', '.join(series[:8])}{' …' if len(series) > 8 else ''}).\n"
                               f"Review, then Approve or Stop: {base}/approvals/{aid}?token={token}\n"
                               f"Nothing happens without an answer. Expires in {cfg['approval_expiry_days']} days."))
            return
        self._delete(sonarr, ready)

    def _delete(self, sonarr, items):
        by_file = {}
        for c in items:  # one file can hold several episodes
            by_file.setdefault(c["file_id"], []).append(c)
        ids = list(by_file)
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            sonarr.send("DELETE", "/episodefile/bulk", {"episodeFileIds": chunk}, timeout=600)
            for fid in chunk:
                for c in by_file[fid]:
                    store.log(f"Deleted {c['title']} {c['label']} ({c['reason']})", "delete")

    def approve(self, aid):
        a = store.get_approval(aid)
        if not a or a["status"] not in ("pending", "running"):
            return "This approval is no longer pending."
        cfg = config.load()
        if cfg["dry_run"]:
            return "Nextarr is in dry run mode. Nothing was deleted."
        self.sync("approval")
        still = {(c["series_id"], c["file_id"]) for p in self.plans.values() for c in
                 [dict(d, series_id=p["id"]) for d in p["delete"]]}
        items = [c for c in a["payload"] if (c["series_id"], c["file_id"]) in still]
        try:
            with self.lock:
                self._delete(self._clients(cfg)[0], items)
        except Exception as e:
            store.set_approval_status(aid, "failed")
            store.log(f"Approval #{aid} failed while deleting: {e}", "error")
            return f"Failed: {e}"
        store.set_approval_status(aid, "approved")
        store.log(f"Approval #{aid}: deleted {len(items)} files", "action")
        skipped = len(a["payload"]) - len(items)
        return f"Deleted {len(items)} files." + (f" {skipped} skipped (no longer candidates)." if skipped else "")

    def stop(self, aid):
        store.set_approval_status(aid, "stopped")
        store.log(f"Approval #{aid} stopped", "warning")

    def _notify(self, cfg, msg):
        try:
            clients.discord(cfg["discord_webhook"], msg)
        except clients.ApiError as e:
            store.log(f"Discord notification failed: {e}", "warning")

    # ---------- scheduler ----------
    def trigger(self):
        self._wake.set()

    def run_forever(self):
        while True:
            self.sync()
            cfg = config.load()
            hour = time.localtime().tm_hour
            last = store.kv_get("last_cleanup", 0)
            if hour == int(cfg["cleanup_hour"]) and time.time() - last > 20 * 3600:
                self.cleanup()
            self._wake.wait(timeout=max(60, int(cfg["sync_interval_minutes"]) * 60))
            if self._wake.is_set():
                self._wake.clear()
                time.sleep(20)  # debounce bursts of webhooks
