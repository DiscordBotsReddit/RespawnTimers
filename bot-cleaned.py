import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import List, Literal, Optional

import aiohttp
import aiosqlite
import discord
from aiosqlite import IntegrityError, OperationalError
from discord import app_commands
from discord.ext import commands, tasks
from pytimeparse.timeparse import timeparse

TOKEN = ""
DATABASE = "boss_spawn_timers.db"
IMGUR_ID = ""
IMGUR_HEADERS = {"Authorization": f"Client-ID {IMGUR_ID}"}
PREFIX = ">>"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)


@bot.event
async def on_ready():
    if not os.path.exists(DATABASE):
        with open(DATABASE, "w") as f:
            f.write("")
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            """
        CREATE TABLE IF NOT EXISTS
        boss_spawn_timers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            guild_id BIGINT NOT NULL,
            name TEXT NOT NULL UNIQUE,
            boss_pic TEXT NULLABLE,
            map_name TEXT NOT NULL,
            spawn_pic TEXT NULLABLE,
            description TEXT NOT NULL,
            respawn INTEGER NOT NULL,
            ping_before INTEGER NOT NULL,
            boss_role BIGINT NOT NULL,
            channel_ping BIGINT NOT NULL);
        """
        )
        await db.execute(
            """
        CREATE TABLE IF NOT EXISTS
        current_timers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ping_sent INTEGER NOT NULL DEFAULT 0,
            guild_id BIGINT NOT NULL,
            respawns_at INTEGER NOT NULL,
            time_to_ping INTEGER NOT NULL,
            boss_id INTEGER,
            FOREIGN KEY (boss_id) REFERENCES boss_spawn_timers (id));
        """
        )
        await db.commit()
    print(f"Logged in as {bot.user}!")
    await check_if_time_to_ping.start()


class BossPanelButton(discord.ui.Button):
    def __init__(self, boss_name: str):
        super().__init__()
        self.label = f"🔪 {boss_name"'
        self.style = discord.ButtonStyle.danger

    async def callback(self, interaction: discord.Interaction):
        boss_kill_timestamp = int(
            (datetime.now(tz=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
        )
        await interaction.response.defer(thinking=True)

        def wait_for_check(m: discord.Message):
            if m.author == interaction.user and isinstance(timeparse(m.content), int):
                return True

        try:
            unix_timestamp = int(
                (
                    datetime.now(tz=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
                ).total_seconds()
            )
            time_out = 60
            time_to_respond = unix_timestamp + time_out
            how_long_msg = await interaction.channel.send(
                content=f"{interaction.user.mention}, how long ago did you kill the boss?\nSend `0s` if you want to use right now, or let timer run out to use the time you pressed the button.\n\n**This interaction times out <t:{time_to_respond}:R>.**"
            )
            ago = await bot.wait_for("message", timeout=time_out, check=wait_for_check)
            try:
                await ago.delete()
                await how_long_msg.delete()
            except:
                print("cant delete")
        except asyncio.TimeoutError:
            ago = None
            try:
                await how_long_msg.delete()
            except:
                print("cant delete")
        boss_name = " ".join(self.label.split(" ")[1:])
        async with aiosqlite.connect(DATABASE) as db:
            results = await db.execute_fetchall(
                f"SELECT id,respawn,ping_before FROM boss_spawn_timers WHERE name LIKE '{boss_name}%' AND guild_id={interaction.guild_id};"
            )
            boss_id = results[0][0]  # type: ignore
            respawn = results[0][1]  # type: ignore
            ping_before = results[0][2]  # type: ignore
            respawn = int(boss_kill_timestamp + respawn)
            ping_before = int(respawn - ping_before)
            if ago is not None:
                ago_msg = ago.content
                ago = timeparse(ago.content)
                respawn = int(respawn - ago)
                ping_before = int(ping_before - ago)
            try:
                await db.execute_insert(
                    "INSERT INTO current_timers (respawns_at,time_to_ping,boss_id,guild_id) VALUES(?,?,?,?);",
                    (respawn, ping_before, boss_id, interaction.guild_id),
                )
                await db.commit()
            except IntegrityError:
                current_timer = await db.execute_fetchall(
                    "SELECT time_to_ping FROM current_timers WHERE boss_id=? AND guild_id=?;",
                    (boss_id, interaction.guild_id),
                )
                return await interaction.edit_original_response(content=f"There is currently a respawn timer for `{boss_name}` that ends <t:{current_timer[0][0]}:R>.", delete_after=60)  # type: ignore
        if ago is None:
            msg = await interaction.edit_original_response(
                content=f"{interaction.user.mention} started the respawn timer for `{boss_name}`.\nNext notification <t:{ping_before}:R>."
            )
        else:
            msg = await interaction.edit_original_response(
                content=f"{interaction.user.mention} started the respawn timer for `{boss_name}` with a time ago amount of `{ago_msg}`.\nNext notification <t:{ping_before}:R>."
            )
        unix_timestamp = int(
            (datetime.now(tz=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
        )
        sleep_time = ping_before - unix_timestamp
        if sleep_time < 0:
            sleep_time = abs(sleep_time)
        await asyncio.sleep(sleep_time)
        try:
            await msg.delete()
        except:
            pass


class BossPanelView(discord.ui.View):
    def __init__(self, boss_names: list[str]):
        super().__init__(timeout=None)

        for name in boss_names:
            button = BossPanelButton(boss_name=name)
            self.add_item(button)


class NewBossBtns(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.message and len(interaction.message.embeds) == 1:
            boss_name = interaction.message.embeds[0].title.split("'")[1]  # type: ignore
            async with aiosqlite.connect(DATABASE) as db:
                who_entered = await db.execute_fetchall(
                    "SELECT user_id FROM boss_spawn_timers WHERE name=? AND guild_id=?;",
                    (boss_name, interaction.guild_id),
                )
                if interaction.user.id == int(who_entered[0][0]):  # type: ignore
                    await db.execute(
                        "DELETE FROM boss_spawn_timers WHERE name=? AND guild_id=?;",
                        (boss_name, interaction.guild_id),
                    )
                    await db.commit()
                    for child in self.children:
                        if type(child) == discord.ui.Button:
                            child.disabled = True
                    await interaction.response.edit_message(
                        view=self,
                        embed=None,
                        content=f"Deleted `{boss_name}` from timers.",
                    )
                else:
                    return await interaction.response.send_message(
                        "You are not able to delete that boss.",
                        ephemeral=True,
                        delete_after=60,
                    )

    @discord.ui.button(label="Killed / Start Timer", style=discord.ButtonStyle.green)
    async def killed_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.message and len(interaction.message.embeds) == 1:
            boss_name = interaction.message.embeds[0].title.split("'")[1]  # type: ignore
            async with aiosqlite.connect(DATABASE) as db:
                results = await db.execute_fetchall(
                    f"SELECT id,respawn,ping_before FROM boss_spawn_timers WHERE name LIKE '{boss_name}%' AND guild_id={interaction.guild_id};"
                )
                boss_id = results[0][0]  # type: ignore
                respawn = results[0][1]  # type: ignore
                ping_before = results[0][2]  # type: ignore
                unix_timestamp = int(
                    (
                        datetime.now(tz=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
                    ).total_seconds()
                )
                respawn = int(unix_timestamp + respawn)
                ping_before = int(respawn - ping_before)
                await db.execute_insert(
                    "INSERT INTO current_timers (respawns_at,time_to_ping,boss_id,guild_id) VALUES(?,?,?,?);",
                    (respawn, ping_before, boss_id, interaction.guild_id),
                )
                await db.commit()
            await interaction.response.edit_message(view=self)
            msg = await interaction.followup.send(
                content=f"{interaction.user.mention} started the respawn timer for `{boss_name}`.\nNext notification <t:{ping_before}:R>.",
                wait=True,
            )
            await asyncio.sleep(int(results[0][1]) + 1)
            await msg.delete()


async def boss_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    async with aiosqlite.connect(DATABASE) as db:
        results = await db.execute_fetchall(
            f"SELECT name FROM boss_spawn_timers WHERE name LIKE '{current}%' AND guild_id={interaction.guild_id};"
        )
    return [
        app_commands.Choice(name=solution[0], value=solution[0]) for solution in results
    ]


@tasks.loop(seconds=1)
async def check_if_time_to_ping():
    unix_timestamp = int(
        (datetime.now(tz=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
    )
    async with aiosqlite.connect(DATABASE) as db:
        things_to_ping = await db.execute_fetchall(
            "SELECT boss_id,guild_id,respawns_at,ping_sent,id FROM current_timers WHERE time_to_ping<=?;",
            (unix_timestamp,),
        )
    if len(things_to_ping) > 0:
        for boss in things_to_ping:
            if unix_timestamp >= boss[2]:
                async with aiosqlite.connect(DATABASE) as db:
                    await db.execute(
                        "DELETE FROM current_timers WHERE id=?;", (boss[4],)
                    )
                    await db.commit()
                    continue
            if boss[3] == 0:
                respawns_at = boss[2]
                ping_guild = bot.get_guild(boss[1])
                async with aiosqlite.connect(DATABASE) as db:
                    boss_info = await db.execute_fetchall(
                        "SELECT name,boss_pic,map_name,spawn_pic,description,boss_role,channel_ping FROM boss_spawn_timers WHERE id=?;",
                        (boss[0],),
                    )
                if len(boss_info) > 0:
                    boss_name = boss_info[0][0]
                    boss_pic_url = boss_info[0][1]
                    map_name = boss_info[0][2]
                    spawn_pic_url = boss_info[0][3]
                    boss_description = boss_info[0][4]
                    ping_role = ping_guild.get_role(boss_info[0][5])
                    ping_channel = ping_guild.get_channel(boss_info[0][6])
                    boss_embed = discord.Embed(
                        title=f"{boss_name}",
                        description=f"Respawns <t:{respawns_at}:R>",
                    )
                    boss_embed.add_field(name="Map Name", value=map_name, inline=False)
                    boss_embed.add_field(
                        name="Description", value=boss_description, inline=False
                    )
                    if spawn_pic_url != "None":
                        boss_embed.set_image(url=spawn_pic_url)
                    if boss_pic_url != "None":
                        boss_embed.set_thumbnail(url=boss_pic_url)
                    await ping_channel.send(
                        content=f"{ping_role.mention} - Respawn alert!",
                        embed=boss_embed,
                    )
                    async with aiosqlite.connect(DATABASE) as db:
                        await db.execute(
                            "UPDATE current_timers SET ping_sent=1 WHERE id=?;",
                            (boss[4],),
                        )
                        await db.commit()
                else:
                    continue


@bot.tree.command(
    name="reset",
    description='Reset a boss timer.  Pass "yes" to the "all" option to reset all.',
)
@app_commands.describe(all_bosses="Reset _ALL_ boss timers.")
@app_commands.describe(boss_name="Name of boss who's timer you want to reset.")
@app_commands.autocomplete(boss_name=boss_name_autocomplete)
async def reset_all_boss_timers(
    interaction: discord.Interaction,
    all_bosses: Optional[Literal["No", "Yes"]],
    boss_name: Optional[str],
):
    if all_bosses == "Yes":
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute(
                "DELETE FROM current_timers WHERE guild_id=?;", (interaction.guild_id,)
            )
            await db.commit()
        return await interaction.response.send_message(
            "All bosses are reset.  Go get'em!"
        )
    if boss_name is not None:
        async with aiosqlite.connect(DATABASE) as db:
            boss_id = await db.execute_fetchall(
                "SELECT id FROM boss_spawn_timers WHERE name=? AND guild_id=?;",
                (boss_name, interaction.guild_id),
            )
            await db.execute("DELETE FROM current_timers WHERE boss_id=? AND guild_id=?;", (boss_id[0][0], interaction.guild_id))  # type: ignore
            await db.commit()
        return await interaction.response.send_message(
            f"All timers for `{boss_name}` have been reset."
        )


@bot.tree.command(
    name="when", description="Get the current respawn timer for the boss."
)
@app_commands.autocomplete(name=boss_name_autocomplete)
async def when_spawn(interaction: discord.Interaction, name: str):
    async with aiosqlite.connect(DATABASE) as db:
        boss_info = await db.execute_fetchall(
            "SELECT id,name,map_name FROM boss_spawn_timers WHERE name=? AND guild_id=?;",
            (name, interaction.guild_id),
        )
        boss_id = boss_info[0][0]
        boss_name = boss_info[0][1]
        map_name = boss_info[0][2]
        respawn_info = await db.execute_fetchall(
            "SELECT respawns_at,time_to_ping FROM current_timers WHERE boss_id=? AND guild_id=? ORDER BY respawns_at ASC LIMIT 1;",
            (boss_id, interaction.guild_id),
        )
        if len(respawn_info) > 0:
            respawns_at = respawn_info[0][0]
            time_to_ping = respawn_info[0][1]
            await interaction.response.send_message(
                f"`{boss_name}` on `{map_name}` Current Timers:\nRespawn: <t:{respawns_at}:R>\nBot will ping: <t:{time_to_ping}:R>"
            )
        else:
            await interaction.response.send_message(
                f"`{boss_name}` on `{map_name}` has NO current timers.",
                ephemeral=True,
                delete_after=30,
            )


@bot.tree.command(name="panel", description="Brings up the boss kill panel.")
async def show_panel(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    boss_names = list()
    async with aiosqlite.connect(DATABASE) as db:
        all_bosses = await db.execute_fetchall(
            "SELECT id,name,map_name,respawn FROM boss_spawn_timers WHERE guild_id=?",
            (interaction.guild_id,),
        )
        if len(all_bosses) == 0:
            await interaction.followup.send(content="No bosses saved.")
        else:
            boss_panel = discord.Embed(
                color=discord.Color.random(),
                title="Boss Kill Panel",
                description="**Boss Name** | Map name | Respawn length | Next spawn time (your time zone)",
            )
            if interaction.guild.icon is not None:
                boss_panel.set_thumbnail(url=interaction.guild.icon.url)
            boss_panel.set_footer(
                text="Respawn timers are 'days, hours : minutes : seconds'"
            )
            for boss in all_bosses:
                boss_name = boss[1]
                map_name = boss[2]
                respawn_seconds = timedelta(seconds=boss[3])
                respawn_length = "Respawns: " + str(respawn_seconds) + ""
                boss_timer = await db.execute_fetchall(
                    "SELECT respawns_at FROM current_timers WHERE boss_id=? ORDER BY respawns_at ASC LIMIT 1;",
                    (boss[0],),
                )
                next_spawn = (
                    f"Next spawn <t:{boss_timer[0][0]}:T>"
                    if len(boss_timer) > 0
                    else "No current timer."
                )
                boss_panel.add_field(
                    name="",
                    value=f"**{boss_name}** | {map_name} | {respawn_length} | {next_spawn}",
                    inline=False,
                )
                boss_names.append(boss_name)
            all_boss_btns = BossPanelView(boss_names)
            await interaction.edit_original_response(
                embed=boss_panel, content="Which boss did you kill?", view=all_boss_btns
            )


@bot.tree.command(
    name="bosses", description="Get a list of all the bosses and their current timers."
)
async def list_bosses(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    async with aiosqlite.connect(DATABASE) as db:
        all_bosses = await db.execute_fetchall(
            "SELECT name,map_name,respawn FROM boss_spawn_timers WHERE guild_id=?",
            (interaction.guild_id,),
        )
        if len(all_bosses) == 0:
            await interaction.followup.send(content="No bosses saved.")
        else:
            boss_panel = discord.Embed(
                color=discord.Color.random(),
                title="Boss List Panel",
                description="**Boss Name** | Map name | Respawn length",
            )
            if interaction.guild.icon is not None:
                boss_panel.set_thumbnail(url=interaction.guild.icon.url)
            boss_panel.set_footer(
                text="Respawn lengths are 'days, hours : minutes : seconds'"
            )
            for boss in all_bosses:
                boss_name = boss[0]
                map_name = boss[1]
                respawn_seconds = timedelta(seconds=boss[2])
                respawn_length = "Respawns every " + str(respawn_seconds) + ""
                boss_panel.add_field(
                    name="",
                    value=f"**{boss_name}** | {map_name} | {respawn_length}",
                    inline=False,
                )
            await interaction.edit_original_response(embed=boss_panel, content="")


@bot.tree.command(
    name="killed", description="Mark a boss as killed and start their respawn timer."
)
@app_commands.autocomplete(name=boss_name_autocomplete)
@app_commands.describe(
    ago='Put a space between each unit, not between the length and unit(ie: "1h 30m" not "1 h 30 m").'
)
async def start_boss_timer(
    interaction: discord.Interaction, name: str, ago: Optional[str]
):
    async with aiosqlite.connect(DATABASE) as db:
        results = await db.execute_fetchall(
            f"SELECT id,respawn,ping_before FROM boss_spawn_timers WHERE name LIKE '{name}%' AND guild_id={interaction.guild_id};"
        )
        boss_id = results[0][0]  # type: ignore
        respawn = results[0][1]  # type: ignore
        ping_before = results[0][2]  # type: ignore
        unix_timestamp = int(
            (datetime.now(tz=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
        )
        respawn = int(unix_timestamp + respawn)
        ping_before = int(respawn - ping_before)
        if ago is not None:
            ago = timeparse(ago)
            respawn = int(respawn - ago)
            ping_before = int(ping_before - ago)
        try:
            await db.execute_insert(
                "INSERT INTO current_timers (respawns_at,time_to_ping,boss_id,guild_id) VALUES(?,?,?,?);",
                (respawn, ping_before, boss_id, interaction.guild_id),
            )
            await db.commit()
        except IntegrityError:
            current_timer = await db.execute_fetchall(
                "SELECT time_to_ping FROM current_timers WHERE boss_id=?;", (boss_id,)
            )
            return await interaction.response.send_message(f"There is currently a respawn timer for `{name}` that ends <t:{current_timer[0][0]}:R>.", ephemeral=True, delete_after=60)  # type: ignore
    await interaction.response.send_message(
        f"{interaction.user.mention} started the respawn timer for `{name}`.\nNotification added <t:{ping_before}:R>."
    )
    msg = [msg async for msg in interaction.channel.history(limit=1)]
    await asyncio.sleep(ping_before - unix_timestamp)
    await msg[0].delete()


@bot.tree.command(
    name="remove", description="Remove a boss kill timer from the database."
)
@app_commands.describe(name="The name of the boss you want to remove (case-sensitive).")
@app_commands.autocomplete(name=boss_name_autocomplete)
async def remove_boss_timer(interaction: discord.Interaction, name: str):
    async with aiosqlite.connect(DATABASE) as db:
        who_entered = await db.execute_fetchall(
            "SELECT user_id FROM boss_spawn_timers WHERE name=? AND guild_id=?;",
            (name, interaction.guild_id),
        )
        if interaction.user.id == int(who_entered[0][0]):  # type: ignore
            await db.execute(
                "DELETE FROM boss_spawn_timers WHERE name=? AND guild_id=?;",
                (name, interaction.guild_id),
            )
            await db.commit()
            return await interaction.response.send_message(
                f"Deleted `{name}` from the database.", ephemeral=True, delete_after=60
            )
        else:
            return await interaction.response.send_message(
                "You did not enter that boss, therefore you can not delete it.",
                ephemeral=True,
                delete_after=60,
            )


@bot.tree.command(name="add", description="Add a boss kill timer to the database.")
@app_commands.describe(
    respawn='Put a space between each unit, not between the length and unit(ie: "1h 30m" not "1 h 30 m").'
)
@app_commands.describe(
    ping_before='Put a space between each unit, not between the length and unit(ie: "1h 30m" not "1 h 30 m").'
)
@app_commands.describe(name="The name of the boss.")
@app_commands.describe(boss_pic="The IMGUR link for the picture of the boss.")
@app_commands.describe(map_name="Name of the map the boss spawns on.")
@app_commands.describe(spawn_pic="The IMGUR link for the picture of the boss spawn.")
@app_commands.describe(description="A description of the boss.")
@app_commands.describe(boss_role="The role you want pinged when the boss spawns.")
@app_commands.describe(channel_ping="The channel you want pinged when the boss spawns.")
async def add_boss_timer(
    interaction: discord.Interaction,
    name: str,
    map_name: str,
    description: str,
    respawn: str,
    ping_before: str,
    boss_role: discord.Role,
    channel_ping: discord.TextChannel,
    spawn_pic: Optional[str] = None,
    boss_pic: Optional[str] = None,
):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if boss_pic is not None:
        if "gallery" in boss_pic:
            album_hash = boss_pic.split("/")[4]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.imgur.com/3/album/{album_hash}/images",
                    headers=IMGUR_HEADERS,
                ) as response:
                    if response.status == 404:
                        return await interaction.followup.send(
                            content=f"The URL entered for `boss_pic`: `{boss_pic}` is not a valid image url.",
                            ephemeral=True,
                        )
                    json = await response.json()
                    boss_pic_link = json.get("data")[0].get("link")
                    if boss_pic_link is None:
                        boss_pic_link = "None"
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(boss_pic) as response:
                    if response.status == 404:
                        return await interaction.followup.send(
                            content=f"The URL entered for `boss_pic`: `{boss_pic}` is not a valid image url.",
                            ephemeral=True,
                        )
            boss_pic_link = boss_pic
    else:
        boss_pic_link = "None"
    if spawn_pic is not None:
        if "gallery" in spawn_pic:
            album_hash = spawn_pic.split("/")[4]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.imgur.com/3/album/{album_hash}/images",
                    headers=IMGUR_HEADERS,
                ) as response:
                    if response.status == 404:
                        return await interaction.followup.send(
                            content=f"The URL entered for `spawn_pic`: `{spawn_pic}` is not a valid image url.",
                            ephemeral=True,
                        )
                    json = await response.json()
                    spawn_pic_link = json.get("data")[0].get("link")
                    if spawn_pic_link is None:
                        spawn_pic_link = "None"
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(spawn_pic) as response:
                    if response.status == 404:
                        return await interaction.followup.send(
                            content=f"The URL entered for `spawn_pic`: `{spawn_pic}` is not a valid image url.",
                            ephemeral=True,
                        )
            spawn_pic_link = spawn_pic
    else:
        spawn_pic_link = "None"
    respawn_timer = respawn.split(" ")
    respawn_integer = int()
    for timer in respawn_timer:
        try:
            respawn_integer += timeparse(timer)
        except:
            return await interaction.followup.send(
                content=f"The `respawn` timer entered is not valid.\nPlease run the command again (press the up arrow to get the command back in your edit box) and change the `respawn` parameter.",
                ephemeral=True,
            )
    ping_before_timer = ping_before.split(" ")
    ping_before_integer = int()
    for timer in ping_before_timer:
        try:
            ping_before_integer += timeparse(timer)
        except:
            return await interaction.followup.send(
                content=f"The `ping_before` timer entered is not valid.\nPlease run the command again (press the up arrow to get the command back in your edit box) and change the `ping_before` parameter.",
                ephemeral=True,
            )
    async with aiosqlite.connect(DATABASE) as db:
        try:
            await db.execute_insert(
                """
            INSERT INTO boss_spawn_timers(user_id, guild_id, name, boss_pic, map_name, spawn_pic,
                                    description, respawn, ping_before, boss_role, channel_ping)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?);
                                    """,
                (
                    interaction.user.id,
                    interaction.guild_id,
                    name,
                    boss_pic_link,
                    map_name,
                    spawn_pic_link,
                    description,
                    int(respawn_integer),
                    int(ping_before_integer),
                    boss_role.id,
                    channel_ping.id,
                ),
            )
            await db.commit()
        except IntegrityError:
            return await interaction.followup.send(
                content=f"There is already a boss timer for a boss named `{name}`.\nPlease run the command again (press the up arrow to get the command back in your edit box) and change the `name` parameter.",
                ephemeral=True,
            )
        except OperationalError as e:
            return await interaction.followup.send(
                content=f"OperationalError: `{e}`\nYou should never see this message.  If you do, contact the bot developer.",
                ephemeral=True,
            )
    new_boss_embed = discord.Embed(
        title=f"Added timer for: '{name}'", description=description
    )
    new_boss_embed.add_field(name="Map Name", value=map_name, inline=False)
    new_boss_embed.add_field(name="Respawn Length", value=respawn, inline=False)
    new_boss_embed.add_field(name="Ping Before Length", value=ping_before, inline=False)
    new_boss_embed.add_field(name="Role to Ping", value=boss_role.mention, inline=False)
    new_boss_embed.add_field(
        name="Channel to Ping", value=channel_ping.mention, inline=False
    )
    new_boss_embed.set_author(
        name=interaction.user.display_name, url=interaction.user.display_avatar.url
    )
    if boss_pic_link != "None":
        new_boss_embed.set_thumbnail(url=boss_pic_link)
    boss_btns = NewBossBtns()
    await interaction.followup.send(
        embed=new_boss_embed, view=boss_btns, ephemeral=False
    )


# https://about.abstractumbra.dev/discord.py/2023/01/29/sync-command-example.html
@bot.command()  # type: ignore
@commands.guild_only()
@commands.is_owner()
async def sync(
    ctx: commands.Context,
    guilds: commands.Greedy[discord.Object],
    spec: Optional[Literal["~", "*", "^", "x"]] = None,
) -> None:
    await ctx.reply("Sync request received.")
    if not guilds:
        if spec == "~":
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
        elif spec == "*":
            ctx.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
        elif spec == "^":
            ctx.bot.tree.clear_commands(guild=ctx.guild)
            await ctx.bot.tree.sync(guild=ctx.guild)
            synced = []
        elif spec == "x":
            ctx.bot.tree.clear_commands(guild=None)
            await ctx.bot.tree.sync()
            await ctx.send("Cleared all global commands.")
            return
        else:
            synced = await ctx.bot.tree.sync()

        await ctx.send(
            f"Synced {len(synced)} commands {'globally' if spec is None else 'to the current guild.'}"
        )
        return

    ret = 0
    for guild in guilds:
        try:
            await ctx.bot.tree.sync(guild=guild)
        except discord.HTTPException:
            pass
        else:
            ret += 1

    await ctx.send(f"Synced the tree to {ret}/{len(guilds)}.")


bot.run(TOKEN, log_level=logging.WARN)
