# Nextarr

A rolling TV library for Sonarr. Nextarr keeps only the episodes people are about to
watch, so your library stays small without anyone running out of episodes.

- Every series always keeps **S01E01**, so anyone can try it.
- Every active viewer has the **next 2 episodes** ready (configurable).
- Episodes that **every active viewer has passed** are deleted after a grace period.
- New episodes of airing shows are only fetched when **someone has caught up**.
- Works with Plex through **Tautulli**, and with **Overseerr** requests (also from Requestrr/Discord).
- Viewers can **start at any season**, by request or with `!start` on Discord.

For people who only watch, see the [guide for viewers](docs/viewers.md). Changes are in the
[changelog](CHANGELOG.md).

Nextarr starts in **dry run**: it only logs what it would do. Nothing is monitored, searched
or deleted until you turn dry run off in the web UI.

## How it decides

| Rule | Default |
| --- | --- |
| Episodes kept ahead of each viewer | 2 |
| A viewer is active for | 120 days after their last activity |
| Delete watched episodes after | 14 days |
| Specials (season 0) | ignored |
| Season packs | kept while someone watches that season |
| Exclude a series | Sonarr tag `keep` (configurable) |

Per series, each Plex user's position is their last fully watched episode (from Tautulli).
A user who requested the series in Overseerr but has not started gets the first episodes of the
lowest season they asked for, so a request for season 29 gives S29E01 + S29E02. A request only
counts while it is newer than the last episode the user finished, and never moves them back to a
season they have already reached.

Overseerr refuses seasons that are already partly available. For those, viewers can write
`!start South Park 29` in a Discord channel (see [below](#discord-start-command)), or you can use
**Start a viewer at a season** on the series page in the web UI. Neither moves a viewer back to a
season they have already reached.
Episodes behind the slowest active viewer are deleted; episodes between viewers are kept;
episodes ahead of everyone are deleted unless they are in the season someone is watching.

## Safety

- **Dry run** by default.
- **Approval:** a cleanup of more than 20 files waits for approval (web UI or Discord link).
  Without an answer nothing is deleted. Approvals expire after 7 days.
- **Availability check:** before deleting, Nextarr asks Sonarr for releases of the season.
  If none is usable, the series is marked *hard to find* and nothing in it is deleted.
- Deletions go through Sonarr, so Sonarr's recycle bin applies.
- A file named `PAUSE` in the config folder stops all activity.
- Only the Python standard library. The image is pinned to an exact Python version and digest.

## Install

Images for amd64 and arm64 (Raspberry Pi, most NAS) are published on GitHub Container Registry.
Pin an exact version; Nextarr never updates itself.

```sh
mkdir -p nextarr/config && sudo chown 1000:1000 nextarr/config   # the container runs as uid 1000
```

```yaml
services:
  nextarr:
    image: ghcr.io/osakesans/nextarr:0.1.9
    container_name: nextarr
    restart: unless-stopped
    user: "1000:1000"
    environment:
      - TZ=Etc/UTC   # your time zone, used for the nightly cleanup hour
    volumes:
      - ./config:/config
    ports:
      - "8790:8790"
    labels:
      com.centurylinklabs.watchtower.enable: "false"
```

Open `http://<host>:8790` and follow the welcome message: fill in Sonarr and Tautulli
(required), Plex and Overseerr (optional) under **Settings**, save, and press
**Test connections**. Use the names your containers have on the Docker network (for example
`http://sonarr:8989`), or IP addresses. Plex usually needs `https://plex:32400` with
certificate verification turned off.

Everything is configured in the web UI and stored in `/config/config.json`; nothing is
tied to a particular machine. Nextarr starts in **dry run**, so you can look at the plan
under *Series* and *Cleanup* before anything happens.

Requirements: Sonarr v4, Tautulli, and Plex. Overseerr is optional.

### Webhooks (recommended)

Nextarr syncs every 15 minutes. Webhooks make it react within seconds. The exact URLs with
your token are shown on the Settings page.

- **Tautulli:** Settings → Notification Agents → Webhook. URL `.../webhook/tautulli?token=…`,
  method POST, trigger *Watched*, condition *Media Type is episode*.
- **Sonarr:** Settings → Connect → Webhook. URL `.../webhook/sonarr?token=…`, method POST,
  trigger *On Series Add*.

### Overseerr and Requestrr

Overseerr starts a search for the whole series when a request is approved. Turn off
**Enable Automatic Search** on the Sonarr server in Overseerr once Nextarr is live, so Nextarr
decides what to download. Series with the exclude tag get a full search from Nextarr instead.

To make Discord requests count for the right person, import your Plex users in Overseerr and
fill in each user's Discord ID. Requestrr then requests in their name.

### Discord start command

`!start <series> <season>` lets viewers start at a season themselves, also one Overseerr refuses.
It needs Overseerr, because viewers are matched through the Discord ID on their Overseerr user.

1. Open your bot in the [Discord Developer Portal](https://discord.com/developers/applications) and turn
   on **Message Content Intent** under *Bot*. You can use a new bot or the one Requestrr already uses.
2. Make sure the bot can view the channel, read its message history and send messages there.
3. Under **Settings → Notifications** in Nextarr, fill in the bot token, the channel ID and the reply
   language (`en` or `da`), and save.
4. Write `!start <a series> <a season>` in the channel. The bot answers within about 30 seconds.

Nextarr polls the channel over the REST API and needs no extra libraries. On its first run it only
remembers where the channel is, so old messages are never acted on. Leave the channel ID empty to turn
the command off. If you change the bot's token, also update it in Nextarr.

Send your viewers the [guide for viewers](docs/viewers.md).

### Homepage

```yaml
labels:
  homepage.group: Media
  homepage.name: Nextarr
  homepage.href: http://nextarr.example
  homepage.widget.type: customapi
  homepage.widget.url: http://nextarr:8790/api/status
  homepage.widget.mappings[0].field: managed
  homepage.widget.mappings[0].label: Series
  homepage.widget.mappings[1].field: episodes_kept
  homepage.widget.mappings[1].label: Kept ready
  homepage.widget.mappings[2].field: delete_candidates
  homepage.widget.mappings[2].label: Can delete
  homepage.widget.mappings[3].field: pending_approvals
  homepage.widget.mappings[3].label: Waiting
```

### Discord

Create a webhook in an admin channel and paste it under Settings → Notifications. Set
*Public URL* to the address you reach Nextarr on, so the links in Discord work.

## API

| Path | |
| --- | --- |
| `/api/status` | Counts as JSON (no login, for dashboards) |
| `/api/plan` | Full plan per series |
| `/health` | 200 when the scheduler is alive |

## License

MIT
