#!/usr/bin/env python3
"""
Morning Brief — TTS & Publisher (GitHub Actions only)
======================================================
Reads a pre-generated podcast script from the repo,
converts it to MP3 with edge-tts, uploads to GitHub Releases,
and updates the RSS feed on gh-pages.

Called by: .github/workflows/morning_brief.yml
Secrets needed: only the built-in GITHUB_TOKEN (no external keys)
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import edge_tts
from pydub import AudioSegment

# ── Config ──────────────────────────────────────────────────
CAIRO_TZ     = timezone(timedelta(hours=2))
VOICE_ALEX   = "en-GB-RyanNeural"    # main anchor, warm British
VOICE_SAM    = "en-US-EmmaNeural"    # co-host, sharp & witty
GITHUB_REPO  = os.environ["GITHUB_REPO"]    # e.g. sshinnawy/DailyPodcast
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]   # built-in Actions token


# ── Audio generation ────────────────────────────────────────

async def segment_to_mp3(text: str, voice: str, path: str):
    comm = edge_tts.Communicate(text, voice, rate="+5%")
    await comm.save(path)


async def script_to_mp3(script: str, output_path: str) -> int:
    """Convert 2-host script to merged MP3. Returns duration in seconds."""
    lines = [l.strip() for l in script.splitlines() if l.strip() and ":" in l]

    # Parse turns, merge consecutive same-speaker lines
    turns = []
    for line in lines:
        if line.startswith("ALEX:"):
            speaker, text = "ALEX", line[5:].strip()
        elif line.startswith("SAM:"):
            speaker, text = "SAM", line[4:].strip()
        else:
            continue
        if not text:
            continue
        if turns and turns[-1][0] == speaker:
            turns[-1] = (speaker, turns[-1][1] + " " + text)
        else:
            turns.append((speaker, text))

    tmp = Path("/tmp/segs")
    tmp.mkdir(exist_ok=True)

    print(f"Generating {len(turns)} audio segments...")
    for i, (speaker, text) in enumerate(turns):
        voice = VOICE_ALEX if speaker == "ALEX" else VOICE_SAM
        seg_path = str(tmp / f"s{i:03d}.mp3")
        await segment_to_mp3(text, voice, seg_path)

    # Merge with natural pauses
    combined = AudioSegment.empty()
    pause_switch = AudioSegment.silent(duration=460)
    pause_same   = AudioSegment.silent(duration=260)
    prev = None

    for i, (speaker, _) in enumerate(turns):
        seg = AudioSegment.from_mp3(str(tmp / f"s{i:03d}.mp3"))
        if prev is not None:
            combined += pause_switch if speaker != prev else pause_same
        combined += seg
        prev = speaker

    combined.export(output_path, format="mp3", bitrate="128k")
    duration = len(combined) // 1000
    print(f"Audio: {output_path} ({duration//60}m {duration%60:02d}s)")
    return duration


# ── GitHub publishing ────────────────────────────────────────

def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def upload_release(mp3_path: str, date_str: str) -> tuple[str, int]:
    """Upload MP3 to a new GitHub Release. Returns (url, bytes)."""
    import httpx
    tag  = f"brief-{date_str}"
    name = f"Morning Brief · {date_str}"

    r = httpx.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=github_headers(),
        json={"tag_name": tag, "name": name, "draft": False, "prerelease": False},
        timeout=30,
    )
    r.raise_for_status()
    upload_url = r.json()["upload_url"].split("{")[0]

    data = Path(mp3_path).read_bytes()
    fname = f"morning-brief-{date_str}.mp3"

    u = httpx.post(
        f"{upload_url}?name={fname}",
        headers={**github_headers(), "Content-Type": "audio/mpeg"},
        content=data,
        timeout=180,
    )
    u.raise_for_status()
    url = u.json()["browser_download_url"]
    print(f"Released: {url}")
    return url, len(data)


def update_rss(audio_url: str, audio_bytes: int, duration_s: int,
               date_str: str, description: str):
    """Prepend new episode to feed.xml on gh-pages branch."""
    import httpx
    hdr     = github_headers()
    pub     = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    dur_fmt = f"{duration_s//60}:{duration_s%60:02d}"
    user    = GITHUB_REPO.split("/")[0]
    repo    = GITHUB_REPO.split("/")[1]

    new_item = f"""  <item>
    <title>Morning Brief · {date_str}</title>
    <pubDate>{pub}</pubDate>
    <guid isPermaLink="false">brief-{date_str}</guid>
    <description><![CDATA[{description}]]></description>
    <enclosure url="{audio_url}" length="{audio_bytes}" type="audio/mpeg"/>
    <itunes:duration>{dur_fmt}</itunes:duration>
  </item>"""

    r = httpx.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/feed.xml",
        headers=hdr, params={"ref": "gh-pages"}, timeout=15,
    )

    if r.status_code == 200:
        existing = base64.b64decode(r.json()["content"]).decode()
        sha      = r.json()["sha"]
        updated  = existing.replace("<!-- ITEMS -->", f"<!-- ITEMS -->\n{new_item}", 1)
    else:
        sha = None
        feed_url = f"https://{user}.github.io/{repo}/feed.xml"
        updated  = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Seif's Morning Brief</title>
    <link>https://github.com/{GITHUB_REPO}</link>
    <description>Daily morning briefing for Seif — Cairo, Egypt</description>
    <language>en</language>
    <itunes:author>Morning Brief</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <atom:link href="{feed_url}" rel="self" type="application/rss+xml"/>
    <!-- ITEMS -->
{new_item}
  </channel>
</rss>"""

    payload = {
        "message": f"Brief {date_str}",
        "content": base64.b64encode(updated.encode()).decode(),
        "branch": "gh-pages",
    }
    if sha:
        payload["sha"] = sha

    pr = httpx.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/feed.xml",
        headers=hdr, json=payload, timeout=30,
    )
    pr.raise_for_status()
    print(f"RSS: https://{user}.github.io/{repo}/feed.xml")


# ── Entrypoint ───────────────────────────────────────────────

async def main():
    now      = datetime.now(CAIRO_TZ)
    date_str = now.strftime("%Y-%m-%d")
    day_fmt  = now.strftime("%A, %B %-d %Y")

    # Read script committed by the Cowork scheduled task
    script_path = Path("podcast_script.txt")
    if not script_path.exists():
        print("No script file found. Did the Cowork task run?")
        sys.exit(1)

    script = script_path.read_text().strip()
    if not script:
        print("Script file is empty.")
        sys.exit(1)

    # Read metadata if present
    meta_path = Path("podcast_meta.json")
    description = f"Morning Brief for {day_fmt}"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        description = meta.get("description", description)
        date_str    = meta.get("date", date_str)

    print(f"\n=== Morning Brief TTS: {date_str} ===\n")
    print(f"Script preview:\n{script[:200]}...\n")

    # Generate audio
    mp3_path   = f"/tmp/morning-brief-{date_str}.mp3"
    duration_s = await script_to_mp3(script, mp3_path)

    # Publish
    print("\nUploading to GitHub Releases...")
    audio_url, audio_bytes = upload_release(mp3_path, date_str)

    print("Updating RSS feed...")
    update_rss(audio_url, audio_bytes, duration_s, date_str, description)

    print(f"\nDone. Episode for {date_str} is live.")


if __name__ == "__main__":
    import httpx  # ensure import available at top level
    asyncio.run(main())
