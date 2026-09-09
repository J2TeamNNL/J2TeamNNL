#!/usr/bin/env python3
"""Refresh the profile now-playing card from Spotify and/or YouTube Music."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARD_PATH = ROOT / "assets" / "now-playing.svg"
README_PATH = ROOT / "README.md"
SPOTIFY_USER_URL = "https://open.spotify.com/user/31ghget3jspvgpjwbv5pcwli3smab"
YTMUSIC_HOME_URL = "https://music.youtube.com"
MARKER_START = "<!-- NOW_PLAYING:START -->"
MARKER_END = "<!-- NOW_PLAYING:END -->"

CTX = ssl.create_default_context()


def xml_escape(text: str) -> str:
    return xml.sax.saxutils.escape(text or "", {'"': "&quot;"})


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def http_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, method: str | None = None) -> tuple[int, object]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, context=CTX, timeout=20) as response:
            raw = response.read()
            if not raw:
                return response.status, {}
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        return exc.code, payload


def http_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "J2TeamNNL-now-playing"})
    with urllib.request.urlopen(request, context=CTX, timeout=20) as response:
        return response.read()


def fetch_spotify() -> dict | None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = (
        os.environ.get("SPOTIFY_SECRET_ID")
        or os.environ.get("SPOTIFY_CLIENT_SECRET")
        or ""
    ).strip()
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        return None

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    status, token_payload = http_json(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
    if status != 200 or not access_token:
        print(f"Spotify token refresh failed: HTTP {status}", file=sys.stderr)
        return None

    auth = {"Authorization": f"Bearer {access_token}"}
    status, now = http_json(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers=auth,
    )
    item = now.get("item") if isinstance(now, dict) else None
    if status == 200 and item:
        return track_from_spotify_item(item, is_playing=bool(now.get("is_playing")), recent=False)

    status, recent = http_json(
        "https://api.spotify.com/v1/me/player/recently-played?limit=1",
        headers=auth,
    )
    items = recent.get("items") if isinstance(recent, dict) else None
    if status == 200 and items:
        return track_from_spotify_item(items[0].get("track") or {}, is_playing=False, recent=True)
    return None


def track_from_spotify_item(item: dict, *, is_playing: bool, recent: bool) -> dict | None:
    if not item:
        return None
    artists = item.get("artists") or []
    images = ((item.get("album") or {}).get("images")) or []
    image_url = ""
    if len(images) > 1:
        image_url = images[1].get("url") or ""
    elif images:
        image_url = images[0].get("url") or ""
    return {
        "title": item.get("name") or "Unknown track",
        "artist": artists[0].get("name") if artists else "Unknown artist",
        "image_url": image_url,
        "track_url": ((item.get("external_urls") or {}).get("spotify")) or SPOTIFY_USER_URL,
        "source": "spotify",
        "source_label": "Spotify Playing" if is_playing else "Recently on Spotify",
        "is_playing": is_playing,
        "recent": recent,
    }


def fetch_youtube_music() -> dict | None:
    raw_headers = os.environ.get("YTMUSIC_HEADERS", "").strip()
    browser_json = os.environ.get("YTM_BROWSER", "").strip()
    if not (raw_headers or browser_json):
        return None
    try:
        from ytmusicapi import YTMusic
        from ytmusicapi import setup as yt_setup
    except ImportError:
        print("ytmusicapi is not installed; skip YouTube Music", file=sys.stderr)
        return None

    auth_path = Path("/tmp/ytmusic-auth.json")
    try:
        if browser_json:
            auth_path.write_text(browser_json, encoding="utf-8")
        else:
            yt_setup(filepath=str(auth_path), headers_raw=raw_headers)
        yt = YTMusic(str(auth_path))
        history = yt.get_history() or []
    except Exception as exc:  # pragma: no cover - network/auth failures
        print(f"YouTube Music history failed: {exc}", file=sys.stderr)
        return None
    if not history:
        return None
    item = history[0]
    artists = item.get("artists") or []
    thumbs = item.get("thumbnails") or []
    video_id = item.get("videoId") or ""
    return {
        "title": item.get("title") or "Unknown track",
        "artist": artists[0].get("name") if artists else "YouTube Music",
        "image_url": thumbs[-1].get("url") if thumbs else "",
        "track_url": f"https://music.youtube.com/watch?v={video_id}" if video_id else YTMUSIC_HOME_URL,
        "source": "youtube-music",
        "source_label": "YouTube Music",
        "is_playing": False,
        "recent": True,
    }


def fetch_lastfm() -> dict | None:
    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    user = os.environ.get("LASTFM_USER", "").strip()
    if not (api_key and user):
        return None
    query = urllib.parse.urlencode(
        {
            "method": "user.getrecenttracks",
            "user": user,
            "api_key": api_key,
            "format": "json",
            "limit": "1",
        }
    )
    status, payload = http_json(f"https://ws.audioscrobbler.com/2.0/?{query}")
    tracks = (((payload or {}).get("recenttracks") or {}).get("track")) if status == 200 else None
    if not tracks:
        return None
    item = tracks[0] if isinstance(tracks, list) else tracks
    images = item.get("image") or []
    image_url = ""
    for image in reversed(images):
        if image.get("#text"):
            image_url = image["#text"]
            break
    attr = item.get("@attr") or {}
    is_playing = str(attr.get("nowplaying", "")).lower() == "true"
    url = item.get("url") or ""
    source = "youtube-music" if "youtube" in url.lower() else "lastfm"
    return {
        "title": item.get("name") or "Unknown track",
        "artist": (item.get("artist") or {}).get("#text") or "Unknown artist",
        "image_url": image_url,
        "track_url": url or YTMUSIC_HOME_URL,
        "source": source,
        "source_label": "YouTube Music" if source == "youtube-music" else ("Now Playing" if is_playing else "Recently played"),
        "is_playing": is_playing,
        "recent": not is_playing,
    }


def choose_track(tracks: list[dict]) -> dict | None:
    playing = [track for track in tracks if track.get("is_playing")]
    if playing:
        return playing[0]
    return tracks[0] if tracks else None


def load_cover_data_uri(image_url: str) -> str:
    if not image_url:
        return ""
    try:
        content = http_bytes(image_url)
    except Exception as exc:  # pragma: no cover - remote image failures
        print(f"Cover download failed: {exc}", file=sys.stderr)
        return ""
    mime = "image/jpeg"
    if image_url.lower().endswith(".png"):
        mime = "image/png"
    elif image_url.lower().endswith(".webp"):
        mime = "image/webp"
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def bar_animations() -> str:
    parts = []
    for index in range(7):
        x = index * 5
        duration = round(random.uniform(0.8, 1.2), 2)
        parts.append(
            f"""    <rect x="{x}" y="8" width="3" height="8" rx="1">
      <animate attributeName="height" values="5;16;5" dur="{duration}s" repeatCount="indefinite"/>
      <animate attributeName="y" values="11;0;11" dur="{duration}s" repeatCount="indefinite"/>
    </rect>"""
        )
    return "\n".join(parts)


def render_svg(track: dict | None) -> str:
    if track:
        title = truncate(track["title"], 28)
        artist = truncate(track["artist"], 32)
        label = track.get("source_label") or "Now Playing"
        cover = load_cover_data_uri(track.get("image_url") or "")
        aria = f"{title} — {artist}"
    else:
        title = "Nothing playing"
        artist = "J2TeamNNL"
        label = "Now Playing"
        cover = ""
        aria = "Nothing playing"

    if cover:
        art = f"""  <defs>
    <clipPath id="cover">
      <rect x="16" y="16.5" width="100" height="100" rx="6"/>
    </clipPath>
  </defs>
  <image x="16" y="16.5" width="100" height="100" href="{cover}" clip-path="url(#cover)"/>"""
    else:
        art = """  <rect x="16" y="16.5" width="100" height="100" rx="6" fill="#1DB954"/>
  <g transform="translate(36.5, 37)" fill="#181818">
    <path d="M39.48 20.04c-8.4-5.04-22.32-5.52-30.36-3.06-.72.24-1.44-.18-1.68-.84-.24-.72.18-1.44.84-1.68 9.24-2.76 24.6-2.22 34.32 3.54.66.36.9 1.2.48 1.86-.36.6-1.2.84-1.86.48zm-1.74 4.8c-.36.54-1.02.72-1.56.36-7.2-4.44-18.18-5.76-26.7-3.12-.6.18-1.26-.12-1.44-.72-.18-.6.12-1.26.72-1.44 9.72-2.94 21.84-1.56 30.18 3.6.54.3.72 1.02.36 1.56v.76zm-2.04 4.62c-.3.42-.78.54-1.2.3-6.3-3.84-14.22-4.68-23.58-2.52-.48.12-.96-.18-1.08-.66-.12-.48.18-.96.66-1.08 10.2-2.16 18.9-1.2 25.92 2.94.48.24.54.78.3 1.2l-.02.82z"/>
  </g>"""

    accent = "#FF0000" if track and track.get("source") == "youtube-music" else "#1DB954"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="480" height="133" viewBox="0 0 480 133" role="img" aria-label="{xml_escape(aria)}">
  <title>{xml_escape(aria)}</title>
  <rect width="480" height="133" rx="8" fill="#181818"/>
{art}
  <text x="140" y="42" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif" font-size="13" font-weight="600" fill="{accent}">{xml_escape(label)}</text>
  <text x="140" y="70" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif" font-size="22" font-weight="600" fill="#e6e6e6">{xml_escape(title)}</text>
  <text x="140" y="94" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif" font-size="16" fill="#b3b3b3">{xml_escape(artist)}</text>
  <g fill="{accent}" transform="translate(140, 108)">
{bar_animations()}
  </g>
</svg>
"""


def update_readme(track: dict | None, cache_buster: str) -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    href = (track or {}).get("track_url") or SPOTIFY_USER_URL
    alt = "Now Playing"
    if track:
        alt = f"{track['title']} — {track['artist']}"
    src = "https://raw.githubusercontent.com/J2TeamNNL/J2TeamNNL/master/assets/now-playing.svg"
    if cache_buster:
        src += f"?t={urllib.parse.quote(cache_buster, safe='')}"
    block = (
        f"{MARKER_START}\n"
        f'[<img src="{src}" alt="{xml_escape(alt)}" width="350" />]({href})\n'
        f"{MARKER_END}"
    )
    if MARKER_START in readme and MARKER_END in readme:
        updated = re.sub(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
            block,
            readme,
            count=1,
            flags=re.S,
        )
    else:
        updated = readme
    README_PATH.write_text(updated, encoding="utf-8")


def main() -> int:
    tracks = []
    for fetcher in (fetch_spotify, fetch_youtube_music, fetch_lastfm):
        track = fetcher()
        if track:
            tracks.append(track)
            print(f"Found {track['source']}: {track['title']} — {track['artist']}")

    chosen = choose_track(tracks)
    CARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    CARD_PATH.write_text(render_svg(chosen), encoding="utf-8")
    cache_buster = os.environ.get("GITHUB_RUN_ID") or os.environ.get("NOW_PLAYING_CACHE") or ""
    update_readme(chosen, cache_buster)
    if not chosen:
        print("No live track yet. Add Spotify or YouTube Music secrets to enable now-playing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
