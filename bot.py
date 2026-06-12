import discord
from discord.ext import commands
import aiohttp
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "YOUR_API_KEY_HERE")
DB_FILE = "channels.json"

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

# ── Colours ───────────────────────────────────────────────────────────────────
RED    = 0xFF4444
GREEN  = 0x00C853
YELLOW = 0xFFD600
BLUE   = 0x2196F3
PURPLE = 0x9C27B0

# ── Database (JSON flat-file) ─────────────────────────────────────────────────
def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {"channels": {}}
    with open(DB_FILE) as f:
        return json.load(f)

def save_db(data: dict):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── YouTube helpers ───────────────────────────────────────────────────────────
YT_BASE = "https://www.googleapis.com/youtube/v3"

async def yt_get(session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict:
    params["key"] = YOUTUBE_API_KEY
    async with session.get(f"{YT_BASE}/{endpoint}", params=params) as r:
        return await r.json()

def published_after(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def is_short(item: dict) -> bool:
    """Best-effort Short detection via duration in contentDetails."""
    dur = item.get("contentDetails", {}).get("duration", "")
    # ISO 8601: PT#M#S — Shorts are ≤ 60 s
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur)
    if not m:
        return False
    h, mn, s = (int(v or 0) for v in m.groups())
    return (h * 3600 + mn * 60 + s) <= 60

def fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

async def resolve_channel_id(session, query: str) -> tuple[str | None, str | None]:
    """Return (channel_id, channel_title) from a URL, handle, or name."""
    # Already a channel ID
    if query.startswith("UC") and len(query) == 24:
        data = await yt_get(session, "channels", {"part": "snippet", "id": query})
        items = data.get("items", [])
        if items:
            return query, items[0]["snippet"]["title"]
        return None, None

    # Handle @username or full URL
    handle = query
    for prefix in ("https://www.youtube.com/@", "https://youtube.com/@", "@"):
        if query.startswith(prefix):
            handle = "@" + query.split("@")[-1].split("/")[0]
            break

    if handle.startswith("@"):
        data = await yt_get(session, "channels", {"part": "snippet", "forHandle": handle.lstrip("@")})
        items = data.get("items", [])
        if items:
            return items[0]["id"], items[0]["snippet"]["title"]

    # Fallback: search by name
    data = await yt_get(session, "search", {"part": "snippet", "type": "channel", "q": query, "maxResults": 1})
    items = data.get("items", [])
    if items:
        cid = items[0]["snippet"]["channelId"]
        title = items[0]["snippet"]["channelTitle"]
        return cid, title
    return None, None

async def fetch_trending(session, query: str, days: int, video_type: str, max_results: int = 8) -> list[dict]:
    """
    Search for trending videos matching query.
    video_type: 'short', 'video', or 'both'
    Returns list of enriched video dicts.
    """
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "viewCount",
        "publishedAfter": published_after(days),
        "maxResults": 25,  # fetch more so we can filter
        "videoDuration": "short" if video_type == "short" else "any",
    }
    data = await yt_get(session, "search", params)
    items = data.get("items", [])
    if not items:
        return []

    ids = ",".join(i["id"]["videoId"] for i in items)
    details = await yt_get(session, "videos", {
        "part": "statistics,contentDetails,snippet",
        "id": ids,
    })
    videos = details.get("items", [])

    # Filter by type
    if video_type == "short":
        videos = [v for v in videos if is_short(v)]
    elif video_type == "video":
        videos = [v for v in videos if not is_short(v)]

    # Sort by view count descending
    videos.sort(key=lambda v: int(v.get("statistics", {}).get("viewCount", 0)), reverse=True)
    return videos[:max_results]

async def fetch_channel_stats(session, channel_id: str) -> dict | None:
    data = await yt_get(session, "channels", {
        "part": "snippet,statistics",
        "id": channel_id,
    })
    items = data.get("items", [])
    return items[0] if items else None

# ── Trending commands ─────────────────────────────────────────────────────────

def trending_type_from_args(args: tuple) -> tuple[str, str]:
    """Return (query, video_type) — video_type is 'short','video', or 'both'."""
    parts = list(args)
    video_type = "both"
    if parts and parts[-1].lower() in ("shorts", "short"):
        video_type = "short"
        parts = parts[:-1]
    elif parts and parts[-1].lower() in ("videos", "video"):
        video_type = "video"
        parts = parts[:-1]
    return " ".join(parts) if parts else "roblox", video_type

async def send_trending(ctx, days: int, label: str, *args):
    query, video_type = trending_type_from_args(args)
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            videos = await fetch_trending(session, query, days, video_type)

    if not videos:
        await ctx.send(embed=discord.Embed(
            description=f"No results found for **{query}** in the last {label}.",
            color=RED))
        return

    type_label = {"short": "🩳 Shorts", "video": "🎬 Videos", "both": "📹 All"}[video_type]
    embed = discord.Embed(
        title=f"🔥 Trending {type_label} — {query}",
        description=f"Last **{label}** · sorted by views",
        color=PURPLE,
        timestamp=datetime.now(timezone.utc),
    )

    for i, v in enumerate(videos, 1):
        sn = v["snippet"]
        st = v.get("statistics", {})
        views = fmt_num(int(st.get("viewCount", 0)))
        likes = fmt_num(int(st.get("likeCount", 0)))
        vid_id = v["id"]
        short_flag = "🩳 " if is_short(v) else ""
        embed.add_field(
            name=f"{i}. {short_flag}{sn['title'][:60]}",
            value=f"👁 {views} views · ❤️ {likes} likes\n[Watch](https://youtu.be/{vid_id}) · {sn['channelTitle']}",
            inline=False,
        )

    embed.set_footer(text=f"Filter: {type_label} · Use !trending {query} shorts/videos to filter")
    await ctx.send(embed=embed)

@bot.command(name="trending")
async def trending_24h(ctx, *args):
    """!trending [query] [shorts|videos] — Trending in last 24 hours"""
    await send_trending(ctx, 1, "24 hours", *args)

@bot.command(name="trendingweek")
async def trending_week(ctx, *args):
    """!trendingweek [query] [shorts|videos] — Trending in last 7 days"""
    await send_trending(ctx, 7, "7 days", *args)

@bot.command(name="trendingmonth")
async def trending_month(ctx, *args):
    """!trendingmonth [query] [shorts|videos] — Trending in last 30 days"""
    await send_trending(ctx, 30, "30 days", *args)

# ── Channel management ────────────────────────────────────────────────────────

@bot.command(name="add")
async def add_channel(ctx, *, channel_query: str):
    """!add <channel URL, @handle, or name> — Add a channel to the known list"""
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            cid, title = await resolve_channel_id(session, channel_query.strip())

    if not cid:
        await ctx.send(embed=discord.Embed(
            description=f"❌ Couldn't find a channel for **{channel_query}**.",
            color=RED))
        return

    db = load_db()
    if cid in db["channels"]:
        await ctx.send(embed=discord.Embed(
            description=f"**{title}** is already in the known list.",
            color=YELLOW))
        return

    db["channels"][cid] = {"title": title, "added_by": str(ctx.author), "added_at": datetime.utcnow().isoformat()}
    save_db(db)

    embed = discord.Embed(
        title="✅ Channel Added",
        description=f"**{title}** has been added to the known leaderboard.",
        color=GREEN,
    )
    embed.add_field(name="Channel ID", value=cid)
    embed.add_field(name="Added by", value=str(ctx.author))
    await ctx.send(embed=embed)

# ── Leaderboard helpers ───────────────────────────────────────────────────────

SORT_OPTIONS = {
    "24h": ("recent", 1),
    "week": ("recent", 7),
    "month": ("recent", 30),
    "views": ("views", None),
    "subs": ("subs", None),
}

def sort_channels(channels: list[dict], mode: str) -> list[dict]:
    if mode == "subs":
        return sorted(channels, key=lambda c: int(c.get("subs", 0)), reverse=True)
    return sorted(channels, key=lambda c: int(c.get("views", 0)), reverse=True)

async def build_leaderboard_embed(channels: list[dict], title: str, sort: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        color=BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    medals = ["🥇", "🥈", "🥉"]
    for i, ch in enumerate(channels[:10]):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        views = fmt_num(int(ch.get("views", 0)))
        subs  = fmt_num(int(ch.get("subs", 0)))
        status = "💀 TERMINATED" if ch.get("terminated") else ""
        embed.add_field(
            name=f"{medal} {ch['title']} {status}",
            value=f"👁 {views} views · 👥 {subs} subs",
            inline=False,
        )
    embed.set_footer(text=f"Sorted by: {sort} · !leaderboard [24h|week|month|views|subs]")
    return embed

async def fetch_stats_for_ids(session, channel_ids: list[str]) -> list[dict]:
    results = []
    for chunk in [channel_ids[i:i+50] for i in range(0, len(channel_ids), 50)]:
        data = await yt_get(session, "channels", {
            "part": "snippet,statistics",
            "id": ",".join(chunk),
            "maxResults": 50,
        })
        for item in data.get("items", []):
            st = item.get("statistics", {})
            results.append({
                "id": item["id"],
                "title": item["snippet"]["title"],
                "views": int(st.get("viewCount", 0)),
                "subs":  int(st.get("subscriberCount", 0)),
                "terminated": False,
            })
        # IDs that didn't come back are likely terminated
        returned_ids = {item["id"] for item in data.get("items", [])}
        for cid in chunk:
            if cid not in returned_ids:
                results.append({"id": cid, "title": cid, "views": 0, "subs": 0, "terminated": True})
    return results

# ── !leaderboard — Auto Roblox ────────────────────────────────────────────────

@bot.command(name="leaderboard")
async def leaderboard_auto(ctx, sort: str = "subs"):
    """!leaderboard [24h|week|month|views|subs] — Top Roblox channels (auto)"""
    sort = sort.lower()
    if sort not in SORT_OPTIONS:
        await ctx.send(f"❌ Unknown sort. Use: `{', '.join(SORT_OPTIONS)}`")
        return

    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            # Search for top Roblox channels
            data = await yt_get(session, "search", {
                "part": "snippet",
                "q": "roblox",
                "type": "channel",
                "order": "relevance",
                "maxResults": 20,
            })
            channel_ids = [item["snippet"]["channelId"] for item in data.get("items", [])]
            channels = await fetch_stats_for_ids(session, channel_ids)

    channels = sort_channels(channels, sort)
    embed = await build_leaderboard_embed(channels, "📊 Roblox Leaderboard (Auto)", sort)
    await ctx.send(embed=embed)

# ── !leaderboardknown — Manual list ──────────────────────────────────────────

@bot.command(name="leaderboardknown")
async def leaderboard_known(ctx, sort: str = "subs"):
    """!leaderboardknown [24h|week|month|views|subs] — Known channels only"""
    sort = sort.lower()
    if sort not in SORT_OPTIONS:
        await ctx.send(f"❌ Unknown sort. Use: `{', '.join(SORT_OPTIONS)}`")
        return

    db = load_db()
    channel_ids = list(db["channels"].keys())

    if not channel_ids:
        await ctx.send(embed=discord.Embed(
            description="No channels added yet. Use `!add <channel>` to add one.",
            color=YELLOW))
        return

    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            channels = await fetch_stats_for_ids(session, channel_ids)

    # Preserve custom titles where YouTube returns nothing (terminated)
    for ch in channels:
        if ch["terminated"] and ch["id"] in db["channels"]:
            ch["title"] = db["channels"][ch["id"]].get("title", ch["id"])

    channels = sort_channels(channels, sort)
    embed = await build_leaderboard_embed(channels, "📊 Known Channels Leaderboard", sort)
    await ctx.send(embed=embed)

# ── !term — Terminated channels ───────────────────────────────────────────────

@bot.command(name="term")
async def terminated(ctx):
    """!term — Show terminated channels from the known list"""
    db = load_db()
    channel_ids = list(db["channels"].keys())

    if not channel_ids:
        await ctx.send(embed=discord.Embed(description="No channels in the known list yet.", color=YELLOW))
        return

    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            channels = await fetch_stats_for_ids(session, channel_ids)

    terminated = [ch for ch in channels if ch["terminated"]]

    if not terminated:
        await ctx.send(embed=discord.Embed(
            title="✅ No Terminated Channels",
            description="All known channels are still active!",
            color=GREEN))
        return

    embed = discord.Embed(
        title=f"💀 Terminated Channels ({len(terminated)})",
        color=RED,
        timestamp=datetime.now(timezone.utc),
    )
    for ch in terminated:
        db_entry = db["channels"].get(ch["id"], {})
        title = db_entry.get("title", ch["id"])
        added_by = db_entry.get("added_by", "Unknown")
        embed.add_field(
            name=f"💀 {title}",
            value=f"Added by: {added_by}\nID: `{ch['id']}`",
            inline=False,
        )
    await ctx.send(embed=embed)

# ── !help override ────────────────────────────────────────────────────────────

@bot.command(name="ythelp")
async def ythelp(ctx):
    embed = discord.Embed(title="📺 YouTube Bot Commands", color=PURPLE)
    embed.add_field(name="🔥 Trending", value=(
        "`!trending [query] [shorts|videos]` — Last 24h\n"
        "`!trendingweek [query] [shorts|videos]` — Last 7 days\n"
        "`!trendingmonth [query] [shorts|videos]` — Last 30 days\n"
        "*Default query: roblox*"
    ), inline=False)
    embed.add_field(name="📊 Leaderboard", value=(
        "`!leaderboard [sort]` — Auto Roblox channels\n"
        "`!leaderboardknown [sort]` — Your added channels\n"
        "*Sort options: `24h` `week` `month` `views` `subs`*"
    ), inline=False)
    embed.add_field(name="📋 Channel Management", value=(
        "`!add <channel>` — Add a channel (URL, @handle, or name)\n"
        "`!term` — Show terminated channels"
    ), inline=False)
    await ctx.send(embed=embed)

# ── Startup ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

bot.run(os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE"))
