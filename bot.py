import discord
from discord.ext import commands
import requests
import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "YOUR_API_KEY_HERE")
DB_FILE = "channels.json"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
executor = ThreadPoolExecutor()

# ── Colours ───────────────────────────────────────────────────────────────────
RED    = 0xFF4444
GREEN  = 0x00C853
YELLOW = 0xFFD600
BLUE   = 0x2196F3
PURPLE = 0x9C27B0
ORANGE = 0xFF6D00

# ── Database ──────────────────────────────────────────────────────────────────
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

def yt_get(endpoint: str, params: dict) -> dict:
    params["key"] = YOUTUBE_API_KEY
    r = requests.get(f"{YT_BASE}/{endpoint}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()

async def yt_get_async(endpoint: str, params: dict) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: yt_get(endpoint, params))

def published_after(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def is_short(item: dict) -> bool:
    dur = item.get("contentDetails", {}).get("duration", "")
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

def calc_vph(views: int, published_at: str) -> int:
    """Calculate views per hour since publish."""
    try:
        pub = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        hours = max((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 1)
        return int(views / hours)
    except Exception:
        return 0

def is_outlier(vph: int, all_vphs: list[int]) -> bool:
    """Flag as outlier if VPH is significantly above average."""
    if not all_vphs or len(all_vphs) < 3:
        return False
    avg = sum(all_vphs) / len(all_vphs)
    return vph > avg * 2.5

def get_tags(item: dict) -> str:
    tags = item.get("snippet", {}).get("tags", [])
    if not tags:
        return ""
    # Show up to 4 tags as hashtags
    return " ".join(f"#{t.replace(' ', '')}" for t in tags[:4])

def get_thumbnail(item: dict) -> str:
    thumbs = item.get("snippet", {}).get("thumbnails", {})
    for quality in ("high", "medium", "default"):
        if quality in thumbs:
            return thumbs[quality]["url"]
    return ""

async def fetch_trending(query: str, days: int, video_type: str, max_results: int = 20) -> list:
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "viewCount",
        "publishedAfter": published_after(days),
        "maxResults": 50,
        "videoDuration": "short" if video_type == "short" else "any",
    }
    data = await yt_get_async("search", params)
    items = data.get("items", [])
    if not items:
        return []

    ids = ",".join(i["id"]["videoId"] for i in items)
    details = await yt_get_async("videos", {
        "part": "statistics,contentDetails,snippet",
        "id": ids,
    })
    videos = details.get("items", [])

    if video_type == "short":
        videos = [v for v in videos if is_short(v)]
    elif video_type == "video":
        videos = [v for v in videos if not is_short(v)]

    videos.sort(key=lambda v: int(v.get("statistics", {}).get("viewCount", 0)), reverse=True)
    return videos[:max_results]

# ── Paginated trending view ───────────────────────────────────────────────────

ITEMS_PER_PAGE = 5

def build_trending_embed(videos: list, query: str, label: str, video_type: str,
                          page: int, total_pages: int) -> discord.Embed:
    type_label = {"short": "YouTube Shorts", "video": "YouTube Videos", "both": "YouTube Videos"}[video_type]
    short_icon = "🩳 " if video_type == "short" else "📹 "

    # Compute all VPHs for outlier detection
    all_vphs = []
    for v in videos:
        views = int(v.get("statistics", {}).get("viewCount", 0))
        pub = v["snippet"].get("publishedAt", "")
        all_vphs.append(calc_vph(views, pub))

    embed = discord.Embed(
        title=f"🔥 Top {len(videos)} Trending {type_label}: {query} (Page {page}/{total_pages})",
        color=ORANGE,
        timestamp=datetime.now(timezone.utc),
    )

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_videos = videos[start:end]

    for i, v in enumerate(page_videos, start + 1):
        sn = v["snippet"]
        st = v.get("statistics", {})
        views = int(st.get("viewCount", 0))
        likes = int(st.get("likeCount", 0))
        vid_id = v["id"]
        pub = sn.get("publishedAt", "")
        vph = calc_vph(views, pub)
        outlier_flag = " 🚀 **OUTLIER**" if is_outlier(vph, all_vphs) else ""
        tags = get_tags(v)
        channel = sn.get("channelTitle", "Unknown")
        title_text = sn["title"]
        short_flag = "🩳 " if is_short(v) else ""

        field_name = f"**{channel}**"
        field_value = (
            f"{short_flag}**#{i} - [{title_text[:60]}](https://youtu.be/{vid_id})**{outlier_flag}\n"
            f"{tags}\n"
            f"📊 **Views** `{fmt_num(views)}`  "
            f"👍 **Likes** `{fmt_num(likes)}`  "
            f"📈 **VPH** `{fmt_num(vph)}`{outlier_flag}\n"
            f"*{label}*"
        )
        embed.add_field(name=field_name, value=field_value, inline=False)

    # Set thumbnail to first video on the page
    if page_videos:
        thumb = get_thumbnail(page_videos[0])
        if thumb:
            embed.set_thumbnail(url=thumb)

    embed.set_footer(text=f"{'🩳 Shorts' if video_type == 'short' else '📹 Videos'} · Use buttons to navigate")
    return embed

# ── Pagination view ───────────────────────────────────────────────────────────

class TrendingView(discord.ui.View):
    def __init__(self, videos: list, query: str, label: str, video_type: str):
        super().__init__(timeout=120)
        self.videos = videos
        self.query = query
        self.label = label
        self.video_type = video_type
        self.page = 1
        self.total_pages = max(1, (len(videos) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.total_pages
        self.page_btn.label = f"{self.page}/{self.total_pages}"

    def get_embed(self) -> discord.Embed:
        return build_trending_embed(
            self.videos, self.query, self.label,
            self.video_type, self.page, self.total_pages
        )

    @discord.ui.button(label="◀", style=discord.ButtonStyle.grey)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.blurple, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.grey)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

# ── Trending helpers ──────────────────────────────────────────────────────────

def trending_type_from_args(args: tuple) -> tuple:
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
        videos = await fetch_trending(query, days, video_type)

    if not videos:
        await ctx.send(embed=discord.Embed(
            description=f"No results found for **{query}** in the last {label}.",
            color=RED))
        return

    view = TrendingView(videos, query, label, video_type)
    await ctx.send(embed=view.get_embed(), view=view)

# ── Trending commands ─────────────────────────────────────────────────────────

@bot.command(name="trending")
async def trending_24h(ctx, *args):
    await send_trending(ctx, 1, "Last 24 hours", *args)

@bot.command(name="trendingweek")
async def trending_week(ctx, *args):
    await send_trending(ctx, 7, "Last 7 days", *args)

@bot.command(name="trendingmonth")
async def trending_month(ctx, *args):
    await send_trending(ctx, 30, "Last 30 days", *args)

# ── Channel management ────────────────────────────────────────────────────────

async def resolve_channel_id(query: str) -> tuple:
    query = query.strip()
    if query.startswith("UC") and len(query) == 24:
        data = await yt_get_async("channels", {"part": "snippet", "id": query})
        items = data.get("items", [])
        if items:
            return query, items[0]["snippet"]["title"]
        return None, None

    handle = query
    for prefix in ("https://www.youtube.com/@", "https://youtube.com/@", "@"):
        if query.startswith(prefix):
            handle = "@" + query.split("@")[-1].split("/")[0]
            break

    if handle.startswith("@"):
        data = await yt_get_async("channels", {"part": "snippet", "forHandle": handle.lstrip("@")})
        items = data.get("items", [])
        if items:
            return items[0]["id"], items[0]["snippet"]["title"]

    data = await yt_get_async("search", {"part": "snippet", "type": "channel", "q": query, "maxResults": 1})
    items = data.get("items", [])
    if items:
        return items[0]["snippet"]["channelId"], items[0]["snippet"]["channelTitle"]
    return None, None

@bot.command(name="add")
async def add_channel(ctx, *, channel_query: str):
    async with ctx.typing():
        cid, title = await resolve_channel_id(channel_query)

    if not cid:
        await ctx.send(embed=discord.Embed(
            description=f"❌ Couldn't find a channel for **{channel_query}**.", color=RED))
        return

    db = load_db()
    if cid in db["channels"]:
        await ctx.send(embed=discord.Embed(
            description=f"**{title}** is already in the known list.", color=YELLOW))
        return

    db["channels"][cid] = {
        "title": title,
        "added_by": str(ctx.author),
        "added_at": datetime.utcnow().isoformat()
    }
    save_db(db)

    embed = discord.Embed(title="✅ Channel Added",
                          description=f"**{title}** added to the known leaderboard.", color=GREEN)
    embed.add_field(name="Channel ID", value=cid)
    embed.add_field(name="Added by", value=str(ctx.author))
    await ctx.send(embed=embed)

# ── Leaderboard helpers ───────────────────────────────────────────────────────

SORT_OPTIONS = {"24h", "week", "month", "views", "subs"}

def sort_channels(channels: list, mode: str) -> list:
    if mode == "subs":
        return sorted(channels, key=lambda c: int(c.get("subs", 0)), reverse=True)
    return sorted(channels, key=lambda c: int(c.get("views", 0)), reverse=True)

def build_leaderboard_embed(channels: list, title: str, sort: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=BLUE, timestamp=datetime.now(timezone.utc))
    medals = ["🥇", "🥈", "🥉"]
    for i, ch in enumerate(channels[:10]):
        medal = medals[i] if i < 3 else f"**{i+1}.**"
        views = fmt_num(int(ch.get("views", 0)))
        subs  = fmt_num(int(ch.get("subs", 0)))
        status = " · 💀 TERMINATED" if ch.get("terminated") else ""
        embed.add_field(
            name=f"{medal} {ch['title']}{status}",
            value=f"👁 {views} views · 👥 {subs} subs",
            inline=False,
        )
    embed.set_footer(text=f"Sorted by: {sort} · !leaderboard [24h|week|month|views|subs]")
    return embed

async def fetch_stats_for_ids(channel_ids: list) -> list:
    results = []
    for chunk in [channel_ids[i:i+50] for i in range(0, len(channel_ids), 50)]:
        data = await yt_get_async("channels", {
            "part": "snippet,statistics",
            "id": ",".join(chunk),
            "maxResults": 50,
        })
        returned_ids = set()
        for item in data.get("items", []):
            st = item.get("statistics", {})
            returned_ids.add(item["id"])
            results.append({
                "id": item["id"],
                "title": item["snippet"]["title"],
                "views": int(st.get("viewCount", 0)),
                "subs":  int(st.get("subscriberCount", 0)),
                "terminated": False,
            })
        for cid in chunk:
            if cid not in returned_ids:
                results.append({"id": cid, "title": cid, "views": 0, "subs": 0, "terminated": True})
    return results

@bot.command(name="leaderboard")
async def leaderboard_auto(ctx, sort: str = "subs"):
    sort = sort.lower()
    if sort not in SORT_OPTIONS:
        await ctx.send(f"❌ Unknown sort. Use: `{'`, `'.join(SORT_OPTIONS)}`")
        return
    async with ctx.typing():
        data = await yt_get_async("search", {
            "part": "snippet", "q": "roblox", "type": "channel",
            "order": "relevance", "maxResults": 100,
        })
        channel_ids = [item["snippet"]["channelId"] for item in data.get("items", [])]
        channels = await fetch_stats_for_ids(channel_ids)
    channels = sort_channels(channels, sort)
    await ctx.send(embed=build_leaderboard_embed(channels, "📊 Roblox Leaderboard (Auto)", sort))

@bot.command(name="leaderboardknown")
async def leaderboard_known(ctx, sort: str = "subs"):
    sort = sort.lower()
    if sort not in SORT_OPTIONS:
        await ctx.send(f"❌ Unknown sort. Use: `{'`, `'.join(SORT_OPTIONS)}`")
        return
    db = load_db()
    channel_ids = list(db["channels"].keys())
    if not channel_ids:
        await ctx.send(embed=discord.Embed(
            description="No channels added yet. Use `!add <channel>` to add one.", color=YELLOW))
        return
    async with ctx.typing():
        channels = await fetch_stats_for_ids(channel_ids)
    for ch in channels:
        if ch["terminated"] and ch["id"] in db["channels"]:
            ch["title"] = db["channels"][ch["id"]].get("title", ch["id"])
    channels = sort_channels(channels, sort)
    await ctx.send(embed=build_leaderboard_embed(channels, "📊 Known Channels Leaderboard", sort))

@bot.command(name="term")
async def show_terminated(ctx):
    db = load_db()
    channel_ids = list(db["channels"].keys())
    if not channel_ids:
        await ctx.send(embed=discord.Embed(description="No channels in the known list yet.", color=YELLOW))
        return
    async with ctx.typing():
        channels = await fetch_stats_for_ids(channel_ids)
    dead = [ch for ch in channels if ch["terminated"]]
    if not dead:
        await ctx.send(embed=discord.Embed(
            title="✅ No Terminated Channels",
            description="All known channels are still active!", color=GREEN))
        return
    embed = discord.Embed(title=f"💀 Terminated Channels ({len(dead)})", color=RED,
                          timestamp=datetime.now(timezone.utc))
    for ch in dead:
        entry = db["channels"].get(ch["id"], {})
        embed.add_field(
            name=f"💀 {entry.get('title', ch['id'])}",
            value=f"Added by: {entry.get('added_by', 'Unknown')}\nID: `{ch['id']}`",
            inline=False,
        )
    await ctx.send(embed=embed)

@bot.command(name="ythelp")
async def ythelp(ctx):
    embed = discord.Embed(title="📺 YouTube Bot Commands", color=PURPLE)
    embed.add_field(name="🔥 Trending", value=(
        "`!trending [query] [shorts|videos]` — Last 24h\n"
        "`!trendingweek [query] [shorts|videos]` — Last 7 days\n"
        "`!trendingmonth [query] [shorts|videos]` — Last 30 days\n"
        "*Default query: roblox · Use ◀ ▶ buttons to page through results*"
    ), inline=False)
    embed.add_field(name="📊 Leaderboard", value=(
        "`!leaderboard [sort]` — Auto Roblox channels\n"
        "`!leaderboardknown [sort]` — Your added channels\n"
        "*Sort: `24h` `week` `month` `views` `subs`*"
    ), inline=False)
    embed.add_field(name="📋 Management", value=(
        "`!add <channel>` — Add channel (URL, @handle, or name)\n"
        "`!term` — Show terminated channels"
    ), inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

bot.run(os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE"))