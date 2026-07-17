#!/usr/bin/env python3
"""
Morning Brief — TTS & Publisher (GitHub Actions)
=================================================
1. Reads podcast_script.txt (with [SECTION: Title | URL] markers)
2. Generates stereo MP3 (ALEX = British male, SAM = American female)
3. Builds chapters.json (Podcasting 2.0) with timestamps + article URLs
4. Saves transcript as plain text
5. Uploads MP3 to GitHub Releases
6. Pushes chapters.json + transcript.txt to gh-pages
7. Updates feed.xml with episode, itunes:image, podcast:chapters, podcast:transcript
"""

import asyncio, base64, json, os, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import edge_tts
from pydub import AudioSegment

# ── Config ────────────────────────────────────────────────────
CAIRO_TZ     = timezone(timedelta(hours=2))
VOICE_ALEX   = "en-GB-RyanNeural"
VOICE_SAM    = "en-US-EmmaNeural"
GITHUB_REPO  = os.environ["GITHUB_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

PAUSE_SWITCH_MS = 460   # pause between different speakers
PAUSE_SAME_MS   = 260   # pause between same speaker


# ── Script parsing ────────────────────────────────────────────

def parse_script(raw: str):
    """
    Returns:
      turns:    [(speaker, text), ...]
      sections: [(turn_index, title, url), ...]
    """
    turns    = []
    sections = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Section marker: [SECTION: Title | https://url]
        m = re.match(r'\[SECTION:\s*(.+?)(?:\s*\|\s*(https?://\S+))?\]', line, re.IGNORECASE)
        if m:
            sections.append((len(turns), m.group(1).strip(), (m.group(2) or "").strip()))
            continue

        if line.startswith("ALEX:"):
            speaker, text = "ALEX", line[5:].strip()
        elif line.startswith("SAM:"):
            speaker, text = "SAM", line[4:].strip()
        else:
            continue

        if not text:
            continue

        # Merge consecutive same-speaker lines
        if turns and turns[-1][0] == speaker:
            turns[-1] = (speaker, turns[-1][1] + " " + text)
        else:
            turns.append([speaker, text])

    return turns, sections


# ── Audio generation ──────────────────────────────────────────

async def segment_to_mp3(text: str, voice: str, path: str):
    comm = edge_tts.Communicate(text, voice, rate="+5%")
    await comm.save(path)


async def script_to_mp3(turns, output_path: str):
    """
    Convert parsed turns to merged MP3.
    Returns (duration_ms, segment_offsets_ms) where segment_offsets_ms[i]
    is the start time in ms of turns[i].
    """
    tmp = Path("/tmp/segs")
    tmp.mkdir(exist_ok=True)

    print(f"Generating {len(turns)} audio segments...")
    for i, (speaker, text) in enumerate(turns):
        voice    = VOICE_ALEX if speaker == "ALEX" else VOICE_SAM
        seg_path = str(tmp / f"s{i:03d}.mp3")
        await segment_to_mp3(text, voice, seg_path)

    combined = AudioSegment.empty()
    offsets  = []
    prev     = None

    for i, (speaker, _) in enumerate(turns):
        seg = AudioSegment.from_mp3(str(tmp / f"s{i:03d}.mp3"))
        if prev is not None:
            pause_ms = PAUSE_SWITCH_MS if speaker != prev else PAUSE_SAME_MS
            combined += AudioSegment.silent(duration=pause_ms)
        offsets.append(len(combined))
        combined += seg
        prev = speaker

    combined.export(output_path, format="mp3", bitrate="128k")
    duration_ms = len(combined)
    print(f"Audio: {output_path} ({duration_ms//60000}m {(duration_ms//1000)%60:02d}s)")
    return duration_ms, offsets


# ── Chapters JSON (Podcasting 2.0) ────────────────────────────

def build_chapters(turns, sections, offsets_ms):
    """
    Build chapters list from section markers.
    Each chapter starts at the turn_index where the section begins.
    """
    chapters = []
    for i, (turn_idx, title, url) in enumerate(sections):
        # Clamp turn_idx to valid range
        idx = min(turn_idx, len(offsets_ms) - 1) if offsets_ms else 0
        start_s = offsets_ms[idx] / 1000.0 if offsets_ms else 0.0

        chapter = {
            "startTime": round(start_s, 2),
            "title":     title,
        }
        if url:
            chapter["url"] = url

        # Optional: end time = start of next chapter
        if i + 1 < len(sections):
            next_idx = min(sections[i+1][0], len(offsets_ms) - 1) if offsets_ms else 0
            chapter["endTime"] = round(offsets_ms[next_idx] / 1000.0, 2)

        chapters.append(chapter)

    # Always start with an intro chapter if first section isn't at turn 0
    if not chapters or chapters[0]["startTime"] > 0:
        chapters.insert(0, {"startTime": 0.0, "title": "Intro"})

    return {"version": "1.2.0", "chapters": chapters}


# ── Transcript ────────────────────────────────────────────────

def build_transcript(turns):
    lines = []
    for speaker, text in turns:
        lines.append(f"{speaker}: {text}\n")
    return "\n".join(lines)


# ── GitHub API ────────────────────────────────────────────────

def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_file_sha(path, branch="gh-pages"):
    import httpx
    r = httpx.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
        headers=github_headers(), params={"ref": branch}, timeout=15,
    )
    if r.status_code == 200:
        return r.json().get("sha")
    return None


def push_text_file(path, content, message, branch="gh-pages"):
    import httpx
    sha  = get_file_sha(path, branch)
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  branch,
    }
    if sha:
        body["sha"] = sha
    r = httpx.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
        headers=github_headers(), json=body, timeout=20,
    )
    r.raise_for_status()
    print(f"  Pushed {path}")


def upload_release(mp3_path: str, date_str: str):
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

    data  = Path(mp3_path).read_bytes()
    fname = f"morning-brief-{date_str}.mp3"

    u = httpx.post(
        f"{upload_url}?name={fname}",
        headers={**github_headers(), "Content-Type": "audio/mpeg"},
        content=data, timeout=180,
    )
    u.raise_for_status()
    url = u.json()["browser_download_url"]
    print(f"  Released: {url}")
    return url, len(data)


def update_rss(audio_url, audio_bytes, duration_ms, date_str, description,
               chapters_url, transcript_url):
    import httpx
    hdr     = github_headers()
    pub     = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    dur_s   = duration_ms // 1000
    dur_fmt = f"{dur_s // 60}:{dur_s % 60:02d}"
    user    = GITHUB_REPO.split("/")[0]
    repo    = GITHUB_REPO.split("/")[1]
    cover   = f"https://{user}.github.io/{repo}/cover.png"

    new_item = f"""  <item>
    <title>Morning Brief · {date_str}</title>
    <pubDate>{pub}</pubDate>
    <guid isPermaLink="false">brief-{date_str}</guid>
    <description><![CDATA[{description}]]></description>
    <enclosure url="{audio_url}" length="{audio_bytes}" type="audio/mpeg"/>
    <itunes:duration>{dur_fmt}</itunes:duration>
    <itunes:image href="{cover}"/>
    <podcast:chapters url="{chapters_url}" type="application/json+chapters"/>
    <podcast:transcript url="{transcript_url}" type="text/plain"/>
  </item>"""

    r = httpx.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/feed.xml",
        headers=hdr, params={"ref": "gh-pages"}, timeout=15,
    )

    if r.status_code == 200:
        feed = base64.b64decode(r.json()["content"]).decode()
        sha  = r.json()["sha"]
    else:
        sha      = None
        feed_url = f"https://{user}.github.io/{repo}/feed.xml"
        feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Seif's Morning Brief</title>
    <link>https://github.com/{GITHUB_REPO}</link>
    <description>Daily morning briefing for Seif — Cairo, Egypt</description>
    <language>en</language>
    <itunes:author>Morning Brief</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{cover}"/>
    <image><url>{cover}</url><title>Seif's Morning Brief</title><link>https://github.com/{GITHUB_REPO}</link></image>
    <atom:link href="{feed_url}" rel="self" type="application/rss+xml"/>
    <!-- ITEMS -->
  </channel>
</rss>"""

    # Ensure Podcasting 2.0 namespace present
    if 'xmlns:podcast' not in feed:
        feed = feed.replace('xmlns:atom=', 'xmlns:podcast="https://podcastindex.org/namespace/1.0"\n     xmlns:atom=')

    updated = feed.replace("<!-- ITEMS -->", f"<!-- ITEMS -->\n{new_item}", 1)

    payload = {
        "message": f"Brief {date_str}",
        "content": base64.b64encode(updated.encode()).decode(),
        "branch":  "gh-pages",
    }
    if sha:
        payload["sha"] = sha

    pr = httpx.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/feed.xml",
        headers=hdr, json=payload, timeout=30,
    )
    pr.raise_for_status()
    print(f"  RSS: https://{user}.github.io/{repo}/feed.xml")


# ── Entrypoint ────────────────────────────────────────────────

async def main():
    now      = datetime.now(CAIRO_TZ)
    date_str = now.strftime("%Y-%m-%d")

    script_path = Path("podcast_script.txt")
    meta_path   = Path("podcast_meta.json")

    if not script_path.exists():
        print("No script file found."); sys.exit(1)

    raw    = script_path.read_text().strip()
    turns, sections = parse_script(raw)

    if not turns:
        print("Script file is empty or has no ALEX:/SAM: lines."); sys.exit(1)

    meta        = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    description = meta.get("description", f"Morning Brief for {now.strftime('%A, %B %-d %Y')}")
    date_str    = meta.get("date", date_str)

    print(f"\n=== Morning Brief TTS: {date_str} ===")
    print(f"Turns: {len(turns)} | Sections: {len(sections)}\n")

    # Generate audio
    mp3_path    = f"/tmp/morning-brief-{date_str}.mp3"
    duration_ms, offsets = await script_to_mp3(turns, mp3_path)

    # Build chapters & transcript
    chapters_data = build_chapters(turns, sections, offsets)
    transcript    = build_transcript(turns)

    print(f"  Chapters: {len(chapters_data['chapters'])}")

    user = GITHUB_REPO.split("/")[0]
    repo = GITHUB_REPO.split("/")[1]
    base = f"https://{user}.github.io/{repo}"

    chapters_path   = f"episodes/{date_str}/chapters.json"
    transcript_path = f"episodes/{date_str}/transcript.txt"

    print("\nPushing chapters + transcript to gh-pages...")
    push_text_file(chapters_path,   json.dumps(chapters_data, indent=2), f"Chapters {date_str}")
    push_text_file(transcript_path, transcript,                           f"Transcript {date_str}")

    chapters_url   = f"{base}/{chapters_path}"
    transcript_url = f"{base}/{transcript_path}"

    print("\nUploading MP3 to GitHub Releases...")
    audio_url, audio_bytes = upload_release(mp3_path, date_str)

    print("\nUpdating RSS feed...")
    update_rss(audio_url, audio_bytes, duration_ms, date_str, description,
               chapters_url, transcript_url)

    print(f"\nDone. Episode {date_str} is live.")
    print(f"  Chapters:   {chapters_url}")
    print(f"  Transcript: {transcript_url}")


if __name__ == "__main__":
    import httpx
    asyncio.run(main())
