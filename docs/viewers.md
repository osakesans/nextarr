# Nextarr for viewers

This page is for people who watch on the server. You never need to open Nextarr: you use
Plex, Overseerr and Discord as usual. Admins can copy this text into their own channel.

## How the library works

To save space, the server does not keep every episode of every series.

- Every series always has **S01E01**, so you can always try it.
- When you watch an episode, the **next episodes are fetched for you** automatically.
- Episodes everyone has finished are removed after a while. They come back if someone needs them.
- If an episode isn't there yet, give it a little time. It is on its way.

## Starting a series

| You want to | Do this |
| --- | --- |
| Start from the beginning | Press play on S01E01 in Plex, or request the series |
| Start at a later season that has no episodes on the server | Request that season in Overseerr or on Discord |
| Start at a season that already has some episodes | Use `!start` on Discord (below) |

Overseerr doesn't allow a request for a season that already has some episodes, for instance because
someone else is watching it. That is what `!start` is for.

## The `!start` command

Write this in the channel your admin has chosen:

```
!start South Park 29
```

`!start South Park season 29` also works. Within about a minute the bot replies, and the first
episodes of the season are fetched for you. After that the next ones follow as you watch.
Other people's episodes and progress are not affected.

| The bot says | What it means |
| --- | --- |
| The first episodes are on their way | All good. They appear in Plex once downloaded |
| You've already reached... | You are at that season or later, so your next episodes come by themselves. The command can't take you back to an earlier season |
| Which one? | Several series match. Write the full name |
| Isn't in the library | Request the series in Overseerr first |
| No aired episodes in season... | That season doesn't exist yet. The reply lists the seasons there are |
| Can't match your Discord account | Ask your admin to add your Discord ID to your Overseerr user |
