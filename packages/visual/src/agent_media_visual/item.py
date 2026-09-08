"""The library item, with the parts the app never reads taken out.

Sasonica's item page fetches `/api/items/<id>?expanded=1&include=rssfeed` and
that is the slowest screen in the app, because of what is in the answer rather
than what the screen does with it. Measured on a 105-turn conversation:

    what the app asks Audiobookshelf for      1267 KB
    the same item, through here                138 KB
    ... with gzip, which ABS does not do        29 KB

Three things account for the difference, and none of them is information the
app has any use for:

* **`media.audioFiles`** — never read, anywhere in the web layer, for a server
  item. Downloading does read it, but natively, from its own fetch
  (`AbsDownloader` → `apiHandler.getLibraryItemWithProgress`), so what crosses
  to the WebView is a copy nothing consults.
* **`libraryFiles`** — read in exactly two places, both looking for *ebooks*:
  the ebook table and the reader. The audio entries are dropped and the rest
  kept, so a book with an epub still opens.
* **the fat on each track** — a track carries its inode, its size, its three
  timestamps, its codec, its bitrate, its channel layout and a second copy of
  its filename. The app reads `index`, `startOffset`, `duration`, `title`,
  `contentUrl` and `mimeType`. On a conversation — one audio file per
  sentence, 485 of them — that difference is half a megabyte.

**Why this is a door and not a proxy.** Everything else the app does with
Audiobookshelf — audio, websockets, progress, downloads — keeps going straight
there. Only this one heavy document comes through here, so a canvas that is
down costs the app a slower item page (it falls back), not its server.

The credential is the caller's own ABS bearer, handed back to ABS the same way
`reply` does it: an item they may not read is one ABS refuses to give us.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# What the app actually reads off a track. `startOffset` is what the scrub bar
# and the track-mapping need; the rest of what ABS sends describes the file on
# disk, which is the server's business.
TRACK_KEYS = frozenset({
    "index", "startOffset", "duration", "title", "contentUrl", "mimeType",
    "localFileId",
})


def slim_track(track: dict) -> dict:
    """One track, down to the fields the app reads.

    `metadata` goes with the rest: on these items its `filename` is the same
    string as `title`, and carrying a long filename twice per track was a
    quarter of what was left after the first cut. Local items keep their own
    copy in the device database, which this never touches.
    """
    return {k: v for k, v in track.items() if k in TRACK_KEYS}


def slim_item(item: dict) -> dict:
    """An ABS library item, carrying only what Sasonica reads.

    Shallow-copied rather than mutated: the caller may be holding the parsed
    response for something else, and a function that quietly empties its
    argument is a bad neighbour.
    """
    if not isinstance(item, dict):
        return item
    out = dict(item)

    # Audio files are listed twice over — once here as library files, once
    # inside media — and read neither time.
    files = out.get("libraryFiles")
    if isinstance(files, list):
        out["libraryFiles"] = [f for f in files
                               if not (isinstance(f, dict)
                                       and f.get("fileType") == "audio")]

    media = out.get("media")
    if isinstance(media, dict):
        media = dict(media)
        media.pop("audioFiles", None)
        tracks = media.get("tracks")
        if isinstance(tracks, list):
            media["tracks"] = [slim_track(t) if isinstance(t, dict) else t
                               for t in tracks]
        out["media"] = media
    return out


def item_for_app(item_id: str, bearer: str) -> tuple[bool, dict]:
    """`(ok, item-or-error)` for `item_id`, asked as the caller.

    The error shapes match `reply`'s deliberately: the app already knows how to
    read them, and a 401 must mean "your token is no good" and nothing else,
    because the app answers a 401 by refreshing and can log the user out if
    that fails.
    """
    from . import reply

    item_id = (item_id or "").strip()
    if not item_id:
        return False, {"error": "no item id", "status": 400}
    if not (bearer or "").strip():
        return False, {"error": "no Audiobookshelf login", "status": 401}
    # Which server this login belongs to — the app may be signed in to a second
    # one, and the item ids of one mean nothing to the other.
    user, status = reply.abs_identity(bearer)
    if not user:
        return False, reply._identity_error(status)
    url = reply.abs_home(bearer)
    if not url:
        return False, {"error": "no Audiobookshelf configured on this host",
                       "status": 503}

    req = urllib.request.Request(
        f"{url}/api/items/{item_id}?expanded=1&include=rssfeed",
        headers={"Authorization": f"Bearer {bearer.strip()}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            item = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, reply._identity_error(e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return False, reply._identity_error(0)
    if not isinstance(item, dict) or not item.get("id"):
        return False, {"error": "Audiobookshelf sent no item", "status": 502}
    out = slim_item(item)
    # Whether this is a conversation the caller may take part in — the same
    # answer `/conversation` gives, settled here so the app can choose the chat
    # layout before it draws anything, instead of drawing a book page and
    # rearranging it once the reply box has asked. Same two gates, so a page
    # that opens as a chat always gets its reply box.
    session = None
    if reply.may_reply(user)[0]:
        session, _why = reply.session_for_path(item.get("path") or "")
    out["conversation"] = bool(session)
    return True, out
