# tg2fedi

Mirrors new posts from a Telegram channel to a GoToSocial account.

[Русская версия](README.ru.md)

## What it is

A single Python file. A bot sits in your channel as an administrator, the
script receives new posts from Telegram and publishes them under your name
on your GoToSocial server.

What gets mirrored:

- text with formatting (bold, italic, strikethrough, code, quotes, links);
- photos, including albums of up to 10, as a single post;
- images sent as files.

What does not:

- Telegram rich messages ("articles"): skipped entirely;
- video, polls, stickers, voice messages (a post with a video sends only
  its caption);
- edits and deletions: once a post is sent, it stays as it was;
- old posts: the bot only sees what is published after it was added.

It is built for one channel and one account. One instance cannot handle
two channels, but you can run two instances with different bots.

## How it works

```
Telegram channel ──> bot (Bot API, long polling) ──> tg2fedi ──> GoToSocial API
```

- The script polls Telegram for updates itself. No inbound ports, no
  webhook, no public IP needed.
- An album arrives from Telegram as several separate messages. The script
  waits until they stop coming and merges them into one post.
- Telegram formatting is converted to markdown, which GoToSocial renders.
- The queue and the "already sent" marks live in a small state file, so
  a restart neither loses nor duplicates anything.

## Requirements

- A Linux server with systemd, Python 3.8+ and the `requests` package.
  The machine that runs GoToSocial is the convenient choice, but any
  machine works.
- A Telegram channel where you can appoint administrators.
- A GoToSocial account and access to its settings panel. Raising the
  attachment limit also needs access to the server config.

## Installation

### 1. Telegram bot

1. Send `/newbot` to [@BotFather](https://t.me/BotFather) and pick a
   name. You get a token like `123456:ABC-DEF...`.
2. In the channel: Administrators → Add Administrator → find the bot.
   You can switch off every permission; it can still read posts.

One bot cannot be used in two places at once: whoever asks for updates
first takes them. Create a separate bot for the mirror.

### 2. Attachment limit on the server

GoToSocial allows 6 attachments per post by default; a Telegram album can
hold 10. In the server's `config.yaml`:

```yaml
statuses-media-max-files: 10
```

Restart GoToSocial afterwards. The text limit (`statuses-max-chars`,
5000 by default) needs no change: a Telegram post is at most 4096
characters.

If you cannot edit the config, the script still works, but the server
will reject albums above its limit and those posts are lost (see
"Behaviour").

### 3. GoToSocial token

Open the settings panel (`https://your-server/settings`) signed in as the
account **the posts should go to**.

1. Applications → New Application.
2. Application name: anything, e.g. `tg2fedi`.
3. Redirect URIs: paste the address the panel shows in the box at the top
   of the page. It looks like
   `https://your-server/settings/user/applications/callback`.
   Without it you cannot obtain a token from inside the panel.
4. Scopes: `write`.
5. Create, then open the new application and request an access token.
   Confirm the access and copy the token.

### 4. Files and service

```bash
sudo apt install python3-requests      # Debian/Ubuntu
sudo mkdir -p /srv/tg2fedi
sudo cp tg2fedi.py /srv/tg2fedi/
sudo cp tg2fedi.env.example /srv/tg2fedi/tg2fedi.env
sudo chmod 600 /srv/tg2fedi/tg2fedi.env
sudo nano /srv/tg2fedi/tg2fedi.env     # fill in tokens, channel, server address
sudo cp tg2fedi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg2fedi
```

If you install somewhere other than `/srv/tg2fedi`, fix the two paths in
`tg2fedi.service`.

### 5. Check

```bash
journalctl -u tg2fedi -f
```

Right after start you should see:

```
INFO start: channel my_channel -> https://example.social (private)
```

Publish a post with two images in the channel. After 10–15 seconds:

```
INFO post 123 sent (images: 2)
```

## Settings

All settings live in `tg2fedi.env`. After a change:
`sudo systemctl restart tg2fedi`.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TG_TOKEN` | yes | | Bot token from BotFather |
| `TG_CHANNEL` | yes | | Channel username without `@`, or a numeric id like `-1001234567890` |
| `GTS_URL` | yes | | Server address, e.g. `https://example.social` |
| `GTS_TOKEN` | yes | | Application token with the `write` scope |
| `VISIBILITY` | no | `private` | `public`, `unlisted`, `private` (followers only) or `direct` |
| `FORMAT` | no | `markdown` | `plain` sends bare text, links in parentheses |
| `ALBUM_WAIT` | no | `10` | Seconds of silence to wait after the last part of an album |

### Finding the id of a private channel

A private channel has no username, so you need its numeric id.

1. Stop the service: `sudo systemctl stop tg2fedi`.
2. Publish something in the channel.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser
   and find `"chat":{"id":-100...`. That number is the id.
4. Put it into `TG_CHANNEL` and start the service.

## Behaviour

- **Albums.** An album counts as complete when no new parts arrive for
  `ALBUM_WAIT` seconds. If an album gets split into two posts, raise the
  value. The cost is the same delay before every album is published.
- **Server unreachable.** The post stays in the queue and is retried
  every 30 seconds until it goes through. Post order is preserved.
- **Server rejected the post** (HTTP 400, 404, 413 or 422: too many
  attachments, text too long, file too large). A retry would not help,
  so the post is logged as dropped and the queue moves on.
- **Script was down.** Telegram keeps undelivered updates for about a
  day. Posts from that window arrive after start; older ones are lost.
- **Duplicates.** Ids of delivered messages are remembered; a post is
  never sent twice.
- **State** lives in `/var/lib/tg2fedi/state.json`. It is safe to delete:
  only posts waiting in the queue are lost.
- **Other clients.** Your server accepts 10 images, but your followers'
  servers and apps may show fewer (Mastodon's limit is 4).

## Troubleshooting

| In the log | Cause and fix |
|---|---|
| `KeyError: 'TG_TOKEN'` (or another variable) | `tg2fedi.env` is not filled in, or `EnvironmentFile` points to the wrong path |
| `getUpdates: ... Unauthorized` | Wrong bot token |
| `getUpdates: ... Conflict` | Something else uses the same bot, or a webhook is set on it. Remove the webhook: open `https://api.telegram.org/bot<TOKEN>/deleteWebhook` |
| The `start` line is there, posts never appear | The bot is not a channel administrator, or `TG_CHANNEL` names a different channel |
| `GoToSocial ...: HTTP 401` | Wrong token, or the application lacks the `write` scope |
| `post N dropped: ... HTTP 422` | The post exceeds a server limit, most often more attachments than `statuses-media-max-files` |
| Formatting looks wrong | Set `FORMAT=plain` |

## Updating and removing

Update: replace `/srv/tg2fedi/tg2fedi.py` and run
`sudo systemctl restart tg2fedi`.

Remove:

```bash
sudo systemctl disable --now tg2fedi
sudo rm /etc/systemd/system/tg2fedi.service
sudo rm -r /srv/tg2fedi /var/lib/tg2fedi
sudo systemctl daemon-reload
```

Then revoke the application token in the GoToSocial panel and remove the
bot from the channel administrators.

## Security

- `tg2fedi.env` holds two secrets: the bot token and a token that can
  post as you. The file mode is `600`, and it is kept out of git (see
  `.gitignore`). If you back up the server, these tokens end up in the
  backup too.
- The service runs as a separate transient systemd user (`DynamicUser`)
  and can write only to its own state directory.
- Tokens never reach the log: error messages are scrubbed before they
  are written.
- If a token leaks: reissue the bot token in BotFather (`/revoke`),
  delete the application in the GoToSocial panel and create a new one.

## Other servers

The script talks to the Mastodon-compatible API, so it will probably work
with Mastodon and related servers when `FORMAT=plain` is set (markdown is
not supported there and limits are tighter: usually 4 attachments and
500 characters). It has only been tested against GoToSocial.
