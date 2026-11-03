# Nextarr

A rolling TV library for Sonarr. Nextarr keeps only the episodes people are about to
watch, so your library stays small without anyone running out of episodes.

- Every series always keeps **S01E01**, so anyone can try it.
- Every active viewer has the **next 2 episodes** ready (configurable).
- Episodes that **every active viewer has passed** are deleted after a grace period.
- New episodes of airing shows are only fetched when **someone has caught up**.
- Works with Plex through **Tautulli**, and with **Overseerr** requests (also from Requestrr/Discord).

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
A user who requested the series in Overseerr but has not started gets S01E01 + 2.
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

Clone the repository and build the image (a published image will follow):

```sh
git clone https://github.com/osakesans/nextarr.git
cd nextarr
docker build -t nextarr:0.1.2 .
```

```yaml
services:
  nextarr:
    image: nextarr:0.1.2
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

Open `http://<host>:8790/settings` and fill in Sonarr, Tautulli, Plex and (optionally)
Overseerr. Plex inside Docker usually needs `https://plex:32400` with certificate
verification turned off.

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
