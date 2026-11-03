"""Small HTTP clients for Sonarr, Tautulli, Plex, Overseerr and Discord (stdlib only)."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

UA = "Nextarr/0.1 (+https://github.com/osakesans/nextarr)"


class ApiError(Exception):
    pass


def _request(method, url, headers=None, body=None, timeout=60, verify=True):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        ctx = None if verify or not url.startswith("https") else ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise ApiError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"{method} {url.split('?')[0]} -> {e}") from None


class Sonarr:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/api/v3"
        self.h = {"X-Api-Key": key}

    def get(self, path, timeout=60, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return _request("GET", self.base + path + q, self.h, timeout=timeout)

    def send(self, method, path, body=None, timeout=60):
        return _request(method, self.base + path, self.h, body, timeout=timeout)

    def status(self):
        return self.get("/system/status")

    def series(self):
        return self.get("/series")

    def one_series(self, sid):
        return self.get(f"/series/{sid}")

    def episodes(self, sid):
        return self.get("/episode", seriesId=sid)

    def tags(self):
        return {t["id"]: t["label"] for t in self.get("/tag")}

    def set_monitored(self, ids, monitored):
        if ids:
            self.send("PUT", "/episode/monitor", {"episodeIds": list(ids), "monitored": monitored})

    def update_series(self, series):
        return self.send("PUT", f"/series/{series['id']}", series)

    def search_episodes(self, ids):
        if ids:
            self.send("POST", "/command", {"name": "EpisodeSearch", "episodeIds": list(ids)})

    def search_series(self, sid):
        self.send("POST", "/command", {"name": "SeriesSearch", "seriesId": sid})

    def releases(self, episode_id):
        # Interactive search: asks every indexer, can take a minute.
        return self.get("/release", timeout=180, episodeId=episode_id) or []

    def delete_files(self, file_ids):
        if file_ids:
            self.send("DELETE", "/episodefile/bulk", {"episodeFileIds": list(file_ids)})


class Tautulli:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/api/v2"
        self.key = key

    def call(self, cmd, **params):
        params.update(apikey=self.key, cmd=cmd)
        r = _request("GET", self.base + "?" + urllib.parse.urlencode(params), timeout=60)
        resp = (r or {}).get("response", {})
        if resp.get("result") != "success":
            raise ApiError(f"Tautulli {cmd}: {resp.get('message')}")
        return resp.get("data")

    def episode_history(self, after_date):
        rows, start = [], 0
        while True:
            d = self.call("get_history", media_type="episode", after=after_date,
                          length=1000, start=start, order_column="date", order_dir="desc")
            batch = d.get("data", [])
            rows.extend(batch)
            start += len(batch)
            if not batch or start >= d.get("recordsFiltered", 0):
                return rows


class Plex:
    def __init__(self, url, token, verify_ssl=True):
        self.base = url.rstrip("/")
        self.h = {"X-Plex-Token": token}
        self.verify = verify_ssl

    def get(self, path):
        return (_request("GET", self.base + path, self.h, verify=self.verify) or {}).get("MediaContainer", {})

    def show_tvdb_map(self):
        """ratingKey -> tvdb id for every show in every TV library."""
        out = {}
        for sec in self.get("/library/sections").get("Directory", []):
            if sec.get("type") != "show":
                continue
            for m in self.get(f"/library/sections/{sec['key']}/all?includeGuids=1").get("Metadata", []):
                for g in m.get("Guid", []):
                    if g.get("id", "").startswith("tvdb://"):
                        try:
                            out[int(m["ratingKey"])] = int(g["id"][7:])
                        except ValueError:
                            pass
        return out


class Overseerr:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/api/v1"
        self.h = {"X-Api-Key": key}

    def tv_requests(self, take=200):
        r = _request("GET", f"{self.base}/request?take={take}&skip=0&filter=all&sort=added", self.h)
        out = []
        for x in (r or {}).get("results", []):
            if x.get("type") != "tv":
                continue
            by = x.get("requestedBy") or {}
            out.append({
                "tvdb_id": (x.get("media") or {}).get("tvdbId"),
                "created": x.get("createdAt"),
                "user": by.get("plexUsername") or by.get("username") or by.get("email"),
            })
        return out


def discord(webhook, content):
    if not webhook:
        return
    _request("POST", webhook + ("&" if "?" in webhook else "?") + "wait=true",
             body={"content": content[:1900], "username": "Nextarr"})
