#!/usr/bin/env python3
"""tg2fedi: mirrors new posts from a Telegram channel to a GoToSocial account.

What it does:
  - listens to the channel through a bot (long polling, no inbound ports);
  - reposts regular posts (text, photos, albums of up to 10 photos);
  - ignores rich messages ("articles") and edits;
  - leaves history alone: it only sees posts made after the bot was added.

Settings come from environment variables, see tg2fedi.env.example.
"""
import json
import logging
import os
import re
import time

import requests

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHANNEL = os.environ["TG_CHANNEL"].lstrip("@").lower()
GTS_URL = os.environ["GTS_URL"].rstrip("/")
GTS_TOKEN = os.environ["GTS_TOKEN"]
VISIBILITY = os.environ.get("VISIBILITY", "private")
PLAIN = os.environ.get("FORMAT", "markdown") == "plain"

# seconds of silence after the last part before an album counts as complete
ALBUM_WAIT = int(os.environ.get("ALBUM_WAIT", "10"))
RETRY_WAIT = 30     # seconds to pause after a failed delivery
STATE_FILE = os.path.join(os.environ.get("STATE_DIRECTORY", "."), "state.json")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TG_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tg2fedi")


class Fatal(Exception):
    """An error a retry will not fix: the post gets dropped."""


def safe(e):
    """Error text with the tokens removed (requests likes to print full URLs)."""
    return str(e).replace(TG_TOKEN, "***").replace(GTS_TOKEN, "***")


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("pending", [])   # queue: list of posts, a post = list of messages
    st.setdefault("done", [])      # message_ids already delivered (duplicate guard)
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------- Telegram ----------

def tg(method, http_timeout=30, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=http_timeout)
    data = r.json()
    if not data.get("ok"):
        err = f"Telegram {method}: {data.get('description')}"
        if r.status_code == 400:
            raise Fatal(err)
        raise RuntimeError(err)
    return data["result"]


def is_ours(chat):
    return TG_CHANNEL in (str(chat.get("id")), (chat.get("username") or "").lower())


# ---------- text: Telegram entities -> markdown ----------

RAW = {"url", "mention", "hashtag", "cashtag", "bot_command", "email", "phone_number"}
MARK = {"bold": "**", "italic": "*", "strikethrough": "~~"}


def esc(s):
    """Escapes whatever markdown would mistake for markup in plain text."""
    s = re.sub(r"([\\*_`\[\]~<])", r"\\\1", s)
    return re.sub(r"^([ \t]*)(#{1,6}\s|>|[-=]{3,}[ \t]*$)", r"\1\\\2", s, flags=re.M)


def emph(mark, inner):
    """Wraps line by line with no spaces inside the markers, or markdown breaks."""
    out = []
    for line in inner.split("\n"):
        core = line.strip()
        if not core:
            out.append(line)
            continue
        left = line[: len(line) - len(line.lstrip())]
        right = line[len(line.rstrip()):]
        out.append(f"{left}{mark}{core}{mark}{right}")
    return "\n".join(out)


def wrap(e, raw, inner):
    kind = e["type"]
    if PLAIN:
        return f"{inner()} ({e['url']})" if kind == "text_link" else inner()
    if kind in RAW:
        return raw
    if kind in MARK:
        return emph(MARK[kind], inner())
    if kind == "code":
        return f"`{raw}`"
    if kind == "pre":
        return f"\n```{e.get('language', '')}\n{raw}\n```\n"
    if kind == "text_link":
        return f"[{inner()}]({e['url']})"
    if kind in ("blockquote", "expandable_blockquote"):
        return "\n".join("> " + line for line in inner().split("\n")) + "\n"
    return inner()   # underline, spoiler, custom_emoji and the rest: plain text


def render(text, entities):
    if not text:
        return ""
    # Telegram counts offset/length in UTF-16 code units
    b = text.encode("utf-16-le")
    plain = (lambda s: s) if PLAIN else esc

    def sl(a, z):
        return b[2 * a: 2 * z].decode("utf-16-le")

    def walk(lo, hi, es):
        out, pos, i = [], lo, 0
        while i < len(es):
            e = es[i]
            s, t = e["offset"], e["offset"] + e["length"]
            if s < pos or t > hi:        # malformed overlap: skip it
                i += 1
                continue
            j = i + 1
            while j < len(es) and es[j]["offset"] < t:
                j += 1
            kids = [x for x in es[i + 1: j] if x["offset"] + x["length"] <= t]
            out.append(plain(sl(pos, s)))
            out.append(wrap(e, sl(s, t), lambda: walk(s, t, kids)))
            pos, i = t, j
        out.append(plain(sl(pos, hi)))
        return "".join(out)

    ents = sorted(entities, key=lambda e: (e["offset"], -e["length"]))
    res = walk(0, len(b) // 2, ents).strip()
    # two trailing spaces = hard line break, the way Telegram shows it
    return res if PLAIN else res.replace("\n", "  \n")


# ---------- GoToSocial ----------

def gts(path, **kw):
    r = requests.post(f"{GTS_URL}{path}", timeout=180,
                      headers={"Authorization": f"Bearer {GTS_TOKEN}"}, **kw)
    if r.status_code >= 400:
        err = f"GoToSocial {path}: HTTP {r.status_code} {r.text[:300]}"
        if r.status_code in (400, 404, 413, 422):
            raise Fatal(err)
        raise RuntimeError(err)
    return r.json()


def upload(file_id):
    info = tg("getFile", file_id=file_id)
    r = requests.get(f"{TG_FILE}/{info['file_path']}", timeout=180)
    r.raise_for_status()
    name = os.path.basename(info["file_path"])
    return gts("/api/v1/media", files={"file": (name, r.content)})["id"]


def publish(parts):
    text, ents, files = "", [], []
    for p in parts:
        if not text and (p.get("text") or p.get("caption")):
            text = p.get("text") or p.get("caption")
            ents = p.get("entities") or p.get("caption_entities") or []
        if p.get("photo"):
            files.append(p["photo"][-1]["file_id"])        # the largest size
        elif p.get("document", {}).get("mime_type", "").startswith("image/"):
            files.append(p["document"]["file_id"])         # image sent as a file
    mid = parts[0]["message_id"]
    if not text and not files:
        log.info("post %s: no text and no images, skipping", mid)
        return
    media_ids = [upload(f) for f in files]
    gts("/api/v1/statuses", json={
        "status": render(text, ents),
        "media_ids": media_ids,
        "visibility": VISIBILITY,
        "content_type": "text/plain" if PLAIN else "text/markdown",
    })
    log.info("post %s sent (images: %d)", mid, len(media_ids))


def deliver(st):
    while st["pending"]:
        parts = st["pending"][0]
        mid = parts[0]["message_id"]
        if mid not in st["done"]:
            try:
                publish(parts)
            except Fatal as e:
                log.error("post %s dropped: %s", mid, safe(e))
            except Exception as e:
                log.warning("post %s failed, will retry: %s", mid, safe(e))
                time.sleep(RETRY_WAIT)
                return
            st["done"] = (st["done"] + [mid])[-500:]
        st["pending"].pop(0)
        save_state(st)


# ---------- main loop ----------

def main():
    st = load_state()
    albums = {}   # media_group_id -> {"parts": [...], "t": time of the last part}
    log.info("start: channel %s -> %s (%s)", TG_CHANNEL, GTS_URL, VISIBILITY)
    while True:
        wait = 2 if albums else 50
        try:
            updates = tg("getUpdates", http_timeout=wait + 20, offset=st["offset"],
                         timeout=wait, allowed_updates=["channel_post"])
        except Exception as e:
            log.warning("getUpdates: %s", safe(e))
            time.sleep(10)
            continue
        for u in updates:
            st["offset"] = u["update_id"] + 1
            p = u.get("channel_post")
            if not p or not is_ours(p["chat"]):
                continue
            if "rich_message" in p:
                log.info("post %s: rich message, skipping", p["message_id"])
                continue
            gid = p.get("media_group_id")
            if gid:
                a = albums.setdefault(gid, {"parts": []})
                a["parts"].append(p)
                a["t"] = time.monotonic()
            else:
                st["pending"].append([p])
        now = time.monotonic()
        for gid in [g for g, a in albums.items() if now - a["t"] > ALBUM_WAIT]:
            parts = albums.pop(gid)["parts"]
            st["pending"].append(sorted(parts, key=lambda m: m["message_id"]))
        save_state(st)
        deliver(st)


if __name__ == "__main__":
    main()
