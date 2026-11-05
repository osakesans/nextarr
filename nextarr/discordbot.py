"""Discord command `!start <series> <season>`: lets a viewer start at a season themselves.

Overseerr refuses seasons that are already partly available, so this is the viewer's own way in.
The channel is polled over the REST API, so no gateway connection or extra library is needed.
The Discord user is matched to a Plex user through the Discord ID stored in Overseerr.
"""
import re
import time

from . import __version__, clients, config, store
from .engine import _norm

API = "https://discord.com/api/v10"
POLL_SECONDS = 30
USERS_CACHE_SECONDS = 600
COMMAND = re.compile(r"^!start\s+(.+?)\s+(?:(?:s|season|s[æa]e?son)\s*)?(\d{1,3})\s*$", re.I)

TEXT = {
    "en": {
        "usage": "Write it like this: `!start South Park 29`",
        "unknown_user": "I can't match your Discord account to a Plex user. Ask the admin to add your Discord ID in Overseerr.",
        "no_series": "**{q}** isn't in the library. Request it in Overseerr first.",
        "many": "Which one? {titles}",
        "not_ready": "I'm still starting up. Try again in a few minutes.",
        "excluded": "**{title}** isn't rolling: every episode stays in the library.",
        "unmanaged": "**{title}** can't be started right now ({status}).",
        "no_season": "**{title}** has no aired episodes in season {season}. Seasons: {seasons}.",
        "already": "You've already reached {pos} in **{title}**, so your next episodes come automatically.",
        "started": "**{title}** season {season}: the first episodes are on their way for {name}. They show up in Plex when downloaded.",
        "error": "Something went wrong. Try again later or ask the admin.",
    },
    "da": {
        "usage": "Skriv det sådan her: `!start South Park 29`",
        "unknown_user": "Jeg kan ikke koble din Discord til en Plex-bruger. Bed administratoren om at tilføje dit Discord-ID i Overseerr.",
        "no_series": "**{q}** findes ikke i biblioteket. Anmod om den i Overseerr først.",
        "many": "Hvilken af dem? {titles}",
        "not_ready": "Jeg er ved at starte op. Prøv igen om et par minutter.",
        "excluded": "**{title}** ruller ikke: alle afsnit bliver liggende i biblioteket.",
        "unmanaged": "**{title}** kan ikke startes lige nu ({status}).",
        "no_season": "**{title}** har ingen udsendte afsnit i sæson {season}. Sæsoner: {seasons}.",
        "already": "Du er allerede nået til {pos} i **{title}**, så dine næste afsnit kommer automatisk.",
        "started": "**{title}** sæson {season}: de første afsnit er på vej til {name}. De dukker op i Plex, når de er hentet.",
        "error": "Noget gik galt. Prøv igen senere, eller spørg administratoren.",
    },
}


def _strip_year(title):
    return re.sub(r"\s*\(\d{4}\)\s*$", "", title or "")


class Bot:
    def __init__(self, engine):
        self.engine = engine
        self._users = {}
        self._users_ts = 0
        self._warned_empty = False

    # ---------- Discord ----------
    def _call(self, cfg, method, path, body=None):
        headers = {"Authorization": "Bot " + cfg["discord_bot"]["token"],
                   "User-Agent": f"DiscordBot (https://github.com/osakesans/nextarr, {__version__})"}
        return clients._request(method, API + path, headers, body, timeout=20)

    def _reply(self, cfg, message, text):
        self._call(cfg, "POST", f"/channels/{cfg['discord_bot']['channel_id']}/messages",
                   {"content": text, "message_reference": {"message_id": message["id"], "fail_if_not_exists": False},
                    "allowed_mentions": {"parse": []}})

    # ---------- lookups ----------
    def _discord_users(self, cfg):
        if time.time() - self._users_ts > USERS_CACHE_SECONDS:
            o = clients.Overseerr(cfg["overseerr"]["url"], cfg["overseerr"]["api_key"])
            self._users = o.users_by_discord()
            self._users_ts = time.time()
        return self._users

    def _find_series(self, cfg, query):
        sonarr = clients.Sonarr(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
        q = _norm(query)
        exact, partial = {}, {}
        for s in sonarr.series():
            names = [s["title"], _strip_year(s["title"])] + [a.get("title") for a in s.get("alternateTitles", [])]
            norms = [_norm(n) for n in names if n]
            if q in norms:
                exact[s["id"]] = s
            elif len(q) >= 3 and any(q in n for n in norms):
                partial[s["id"]] = s
        return sonarr, list((exact or partial).values())

    # ---------- the command ----------
    def answer(self, cfg, author_id, content):
        """Returns the reply text. Sets the start when everything checks out."""
        t = TEXT.get(cfg["discord_bot"].get("language"), TEXT["en"])
        m = COMMAND.match(content.strip())
        if not m:
            return t["usage"]
        query, season = m.group(1).strip(), int(m.group(2))
        name = self._discord_users(cfg).get(str(author_id))
        if not name:
            return t["unknown_user"]
        if not self.engine.plans:
            return t["not_ready"]
        sonarr, found = self._find_series(cfg, query)
        if not found:
            return t["no_series"].format(q=query)
        if len(found) > 1:
            return t["many"].format(titles=", ".join(f"**{s['title']}**" for s in found[:5]))
        series = found[0]
        title = series["title"]
        plan = self.engine.plans.get(series["id"])
        if not plan:
            return t["not_ready"]
        if plan["status"] == "excluded":
            return t["excluded"].format(title=title)
        if plan["status"] not in ("managed", "hard to find"):
            return t["unmanaged"].format(title=title, status=plan["status"])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        aired = [e for e in sonarr.episodes(series["id"])
                 if e["seasonNumber"] > 0 and e.get("airDateUtc") and e["airDateUtc"] <= now]
        seasons = sorted({e["seasonNumber"] for e in aired})
        if season not in seasons:
            shown = f"{seasons[0]}-{seasons[-1]}" if seasons else "-"
            return t["no_season"].format(title=title, season=season, seasons=shown)
        for v in plan["viewers"]:
            hit = re.match(r"S(\d+)E\d+", v["pos"] or "")
            if v["name"].lower() == name.lower() and hit and int(hit.group(1)) >= season:
                return t["already"].format(pos=v["pos"], title=title)
        store.set_viewer_start(series["tvdbId"], name, season)
        store.log(f"Discord: {name} starts {title} at season {season}", "action")
        self.engine.trigger()
        return t["started"].format(title=title, season=season, name=name)

    def poll(self, cfg):
        channel = cfg["discord_bot"]["channel_id"]
        key = f"discord_bot_last_{channel}"
        last = store.kv_get(key)
        if last is None:  # first run: never act on old messages
            newest = self._call(cfg, "GET", f"/channels/{channel}/messages?limit=1") or []
            store.kv_set(key, newest[0]["id"] if newest else "0")
            store.log("Discord bot: listening for !start", "info")
            return
        messages = self._call(cfg, "GET", f"/channels/{channel}/messages?after={last}&limit=50") or []
        t = TEXT.get(cfg["discord_bot"].get("language"), TEXT["en"])
        for m in sorted(messages, key=lambda x: int(x["id"])):
            author = m.get("author") or {}
            content = m.get("content") or ""
            if not author.get("bot"):
                if not content and not m.get("attachments") and not self._warned_empty:
                    self._warned_empty = True
                    store.log("Discord bot: got an empty message. Is Message Content Intent turned on?", "warning")
                if re.match(r"^!start\b", content.strip(), re.I):
                    try:
                        text = self.answer(cfg, author.get("id"), content)
                    except clients.ApiError as e:
                        store.log(f"Discord command failed: {e}", "error")
                        text = t["error"]
                    try:
                        self._reply(cfg, m, text)
                    except clients.ApiError as e:
                        store.log(f"Discord reply failed: {e}", "error")
            store.kv_set(key, m["id"])

    def run_forever(self):
        last_error = None
        while True:
            cfg = config.load()
            bot = cfg["discord_bot"]
            if bot.get("token") and bot.get("channel_id"):
                try:
                    self.poll(cfg)
                    last_error = None
                except clients.ApiError as e:
                    if str(e) != last_error:  # do not fill the log while Discord is down
                        store.log(f"Discord bot: {e}", "warning")
                    last_error = str(e)
                except Exception as e:
                    store.log(f"Discord bot error: {e}", "error")
            time.sleep(POLL_SECONDS)
