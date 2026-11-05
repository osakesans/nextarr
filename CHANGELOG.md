# Changelog

## 0.1.9

- New Discord command `!start <series> <season>`, so viewers can start at a season themselves, also
  one that is partly available. Turn it on with a bot token and channel ID under Settings.
- Replies in English or Danish (`discord_bot.language`).
- New guide for viewers: [docs/viewers.md](docs/viewers.md).

## 0.1.8

- A request for a specific season in Overseerr or Requestrr now starts the viewer at that season
  instead of S01E01. It only counts while it is newer than the last episode they finished, and never
  moves them back to a season they have already reached.
- New **Start a viewer at a season** form on the series page, for seasons Overseerr refuses.

Upgrading: viewers with an open request for a later season, who have not watched since, move to that
season. Their first episodes are fetched at the next sync. Run in dry run first to see the plan.
