import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import yt_dlp as youtube_dl
import asyncio
import logging
import json

# Bot setup
intents = discord.Intents.all()
bot = discord.Bot(intents=intents)
volumes = {}

@bot.event
async def on_ready():
    print(f"Bot is ready and online as {bot.user}")

# Load configuration
with open('config.json', 'r') as config_file:
    config = json.load(config_file)

token = config.get("TOKEN")

# YT-DLP options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'quiet': True,
    'default_search': 'ytsearch1',
}
ffmpeg_options = {
    'options': '-vn'
}
ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

song_queue = {}  # Guild-specific song queue
current_song = {}  # Tracks currently playing songs and their details


async def search_youtube(query):
    """Search YouTube for the given query and return the audio URL, title, and duration."""
    try:
        info = ytdl.extract_info(query, download=False)
        if info and 'entries' in info:
            entry = info['entries'][0]
            return entry['url'], entry['title'], entry.get('duration', 0)
        return (info['url'], info['title'], info.get('duration', 0)) if 'url' in info else (None, None, 0)
    except Exception as e:
        logging.error(f"Error searching YouTube: {e}")
        return None, None, 0


class MusicView(View):
    def __init__(self, ctx, duration, guild_id):
        super().__init__(timeout=None)
        self.ctx = ctx
        self.duration = duration
        self.guild_id = guild_id
        self.progress = 0

    @tasks.loop(seconds=1)
    async def update_progress(self):
        """Update progress bar every second."""
        if self.progress >= self.duration:
            self.stop()
        else:
            self.progress += 1
            await self.update_embed()

    def format_duration(self, seconds):
        """Format seconds into MM:SS."""
        mins, secs = divmod(seconds, 60)
        return f"{mins}:{secs:02d}"

    def progress_bar(self):
        """Generate the progress bar."""
        total_slots = 20
        filled_slots = int((self.progress / self.duration) * total_slots)
        bar = "▬" * filled_slots + "🔘" + "▬" * (total_slots - filled_slots)
        return f"[{bar}]"

    async def update_embed(self):
        vc = discord.utils.get(bot.voice_clients, guild__id=self.guild_id)
        if not vc or not vc.is_playing():
            self.stop()
            return

        embed = discord.Embed(
            description=f"Now playing: **{current_song[self.guild_id]['title']}**\n**Duration**\n[{self.progress_bar()}] {self.format_duration(self.progress)} / {self.format_duration(self.duration)}\n[Video Link]({current_song[self.guild_id]['url']})",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=current_song[self.guild_id]['thumbnail'])

        await self.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Volume", style=discord.ButtonStyle.secondary)
    async def change_volume(self, button: Button, interaction: discord.Interaction):
        await interaction.response.send_message("Use `/volume` to change the volume.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary)
    async def skip(self, button: Button, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        vc = discord.utils.get(bot.voice_clients, guild__id=guild_id)

        if not vc or not vc.is_playing():
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
        else:
            vc.stop()
            await interaction.response.send_message("⏭️ Song skipped!")
            await play_next_song(guild_id)

    @discord.ui.button(label="Disconnect", style=discord.ButtonStyle.danger)
    async def disconnect(self, button: Button, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        vc = discord.utils.get(bot.voice_clients, guild__id=guild_id)

        if vc and vc.is_connected():
            await vc.disconnect()
            await interaction.response.send_message("Bot disconnected.")
        else:
            await interaction.response.send_message("Bot is not connected to a voice channel.", ephemeral=True)


async def play_next_song(guild_id):
    """Plays the next song in the queue for the given guild."""
    vc = discord.utils.get(bot.voice_clients, guild__id=guild_id)

    if not vc or not vc.is_connected():
        return

    if guild_id in song_queue and song_queue[guild_id]:
        next_song = song_queue[guild_id].pop(0)
        current_song[guild_id] = next_song

        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(next_song["url"], **ffmpeg_options))
        source.volume = volumes.get(guild_id, 1.0)

        vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
            play_next_song(guild_id), bot.loop
        ).result())

        # Create and update progress view
        ctx = next_song["ctx"]
        view = MusicView(ctx, next_song["duration"], guild_id)
        
        embed = discord.Embed(
            description=f"Now playing: **{next_song['title']}**\n**Duration**\n[▬🔘▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬] 0:00 / {view.format_duration(next_song['duration'])}\n[Video Link]({next_song['url']})",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=next_song['thumbnail'])
        avatar_url = ctx.user.avatar.url if ctx.user.avatar else None
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text=f"Stock's MM Services", icon_url=ctx.bot.user.avatar.url)
        embed.timestamp = discord.utils.utcnow()

        view.message = await ctx.respond(embed=embed, view=view)
        view.update_progress.start()
    else:
        await vc.disconnect()


@bot.slash_command(name="play", description="Play a song from YouTube.")
async def play_command(ctx: discord.ApplicationContext, query: str):
    if not ctx.user.voice:
        await ctx.respond("You must be in a voice channel to use this command.", ephemeral=True)
        return

    await ctx.defer()
    voice_channel = ctx.user.voice.channel
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)

    if not vc or not vc.is_connected():
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    song_url, song_title, duration = await search_youtube(query)
    if not song_url:
        await ctx.followup.send("No results found on YouTube.")
        return

    thumbnail = f"https://i.ytimg.com/vi/{song_url.split('?v=')[-1]}/hqdefault.jpg"
    guild_id = ctx.guild.id

    if guild_id not in song_queue:
        song_queue[guild_id] = []

    song_queue[guild_id].append({
        "url": song_url,
        "title": song_title,
        "duration": duration,
        "thumbnail": thumbnail,
        "ctx": ctx
    })

    if not vc.is_playing():
        await play_next_song(guild_id)

@bot.slash_command(name="volume", description="Change the volume of the current song.")
async def volume(ctx: discord.ApplicationContext, volume: discord.Option(int, description="Volume (1-100)", min_value=1, max_value=100, required=True)): # type: ignore
    guild_id = ctx.guild.id
    volumes[guild_id] = volume / 100

    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    embed = discord.Embed(
        title="Volume Updated",
        description=f"🔊 Volume set to {volume}%.",
        color=discord.Color.gold()
    )
    if vc and vc.is_playing() and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = volumes[guild_id]
    else:
        embed.description += " This will take effect on the next playback."
    await ctx.respond(embed=embed)


@bot.slash_command(name="skip", description="Skip the current song.")
async def skip_command(ctx: discord.ApplicationContext):
    guild_id = ctx.guild.id
    vc = discord.utils.get(bot.voice_clients, guild__id=guild_id)

    if not vc or not vc.is_playing():
        embed = discord.Embed(
            title="Error",
            description="Nothing is currently playing.",
            color=discord.Color.red()
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    vc.stop()
    embed = discord.Embed(
        title="Song Skipped",
        description="⏭️ Skipped to the next song.",
        color=discord.Color.orange()
    )
    await ctx.respond(embed=embed)
    await play_next_song(guild_id)

bot.run(token)