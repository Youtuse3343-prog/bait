from __future__ import annotations
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from bot.config import settings
from bot.store import store
from bot.utils import embed, safe_dm


def moderation_enabled(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild_id and store.get_guild(interaction.guild_id)["features"]["moderation"])


def can_moderate(interaction: discord.Interaction) -> bool:
    return bool(moderation_enabled(interaction) and (interaction.user.id == settings.owner_id or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.moderate_members)))


async def mod_log(guild: discord.Guild, title: str, description: str):
    cfg = store.get_guild(guild.id)
    if not cfg["features"]["logs"]:
        return
    cid = cfg["channels"]["mod_logs"] or cfg["channels"]["logs"]
    ch = guild.get_channel(int(cid)) if cid else None
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=embed(title, description))
        except discord.HTTPException:
            pass


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _dm_action(self, guild: discord.Guild, member: discord.Member, action: str, reason: str):
        cfg = store.get_guild(guild.id)
        if cfg["moderation"].get("dm_on_action", True):
            await safe_dm(member, f"You received a moderation action in **{guild.name}**.\nAction: **{action}**\nReason: {reason}")

    @app_commands.command(name="warn", description="Warn a member and save the warning.")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not can_moderate(interaction):
            return await interaction.response.send_message("Moderate Members permission required.", ephemeral=True)
        if member.id == interaction.user.id or member.bot:
            return await interaction.response.send_message("That member cannot be warned with this command.", ephemeral=True)
        wid = store.add_warning(interaction.guild_id, member.id, interaction.user.id, reason)
        await self._dm_action(interaction.guild, member, f"Warning #{wid}", reason)
        await mod_log(interaction.guild, "Member Warned", f"**Member:** {member.mention} (`{member.id}`)\n**Moderator:** {interaction.user.mention}\n**Warning:** `#{wid}`\n**Reason:** {reason}")
        await interaction.response.send_message(f"Warned {member.mention}. Warning `#{wid}`.", ephemeral=True)

    @app_commands.command(name="warnings", description="Show warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not can_moderate(interaction):
            return await interaction.response.send_message("Moderate Members permission required.", ephemeral=True)
        rows = store.list_warnings(interaction.guild_id, member.id)
        if not rows:
            return await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        body = "\n".join(f"`#{r['id']}` <@{r['moderator_id']}> · {str(r['reason'])[:180]}" for r in rows[-15:])
        await interaction.response.send_message(embed=embed(f"Warnings · {member}", body), ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear all saved warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not can_moderate(interaction):
            return await interaction.response.send_message("Moderate Members permission required.", ephemeral=True)
        count = store.clear_warnings(interaction.guild_id, member.id)
        await mod_log(interaction.guild, "Warnings Cleared", f"**Member:** {member.mention}\n**Moderator:** {interaction.user.mention}\n**Removed:** {count}")
        await interaction.response.send_message(f"Cleared **{count}** warnings for {member.mention}.", ephemeral=True)

    @app_commands.command(name="purge", description="Delete recent messages from this channel.")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200]):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_messages or interaction.user.id == settings.owner_id):
            return await interaction.response.send_message("Manage Messages permission required.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await interaction.channel.purge(limit=int(amount), reason=f"Purge by {interaction.user}")
        await mod_log(interaction.guild, "Messages Purged", f"**Moderator:** {interaction.user.mention}\n**Channel:** {interaction.channel.mention}\n**Deleted:** {len(deleted)}")
        await interaction.followup.send(f"Deleted **{len(deleted)}** messages.", ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not can_moderate(interaction):
            return await interaction.response.send_message("Moderate Members permission required.", ephemeral=True)
        try:
            await member.timeout(timedelta(minutes=int(minutes)), reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.response.send_message(f"Could not timeout that member: {exc}", ephemeral=True)
        await self._dm_action(interaction.guild, member, f"Timeout ({minutes} minutes)", reason)
        await mod_log(interaction.guild, "Member Timed Out", f"**Member:** {member.mention}\n**Moderator:** {interaction.user.mention}\n**Duration:** {minutes} minutes\n**Reason:** {reason}")
        await interaction.response.send_message(f"Timed out {member.mention} for **{minutes} minutes**.", ephemeral=True)

    @app_commands.command(name="untimeout", description="Remove a member timeout.")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not can_moderate(interaction):
            return await interaction.response.send_message("Moderate Members permission required.", ephemeral=True)
        await member.timeout(None, reason=reason)
        await mod_log(interaction.guild, "Timeout Removed", f"**Member:** {member.mention}\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}")
        await interaction.response.send_message(f"Removed timeout from {member.mention}.", ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member.")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.kick_members or interaction.user.id == settings.owner_id):
            return await interaction.response.send_message("Kick Members permission required.", ephemeral=True)
        await self._dm_action(interaction.guild, member, "Kick", reason)
        await member.kick(reason=reason)
        await mod_log(interaction.guild, "Member Kicked", f"**Member:** {member} (`{member.id}`)\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}")
        await interaction.response.send_message(f"Kicked **{member}**.", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member.")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_message_days: app_commands.Range[int, 0, 7] = 0):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.ban_members or interaction.user.id == settings.owner_id):
            return await interaction.response.send_message("Ban Members permission required.", ephemeral=True)
        await self._dm_action(interaction.guild, member, "Ban", reason)
        await interaction.guild.ban(member, reason=reason, delete_message_seconds=int(delete_message_days) * 86400)
        await mod_log(interaction.guild, "Member Banned", f"**Member:** {member} (`{member.id}`)\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}")
        await interaction.response.send_message(f"Banned **{member}**.", ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by Discord user ID.")
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.ban_members or interaction.user.id == settings.owner_id):
            return await interaction.response.send_message("Ban Members permission required.", ephemeral=True)
        try:
            uid = int(user_id)
            user = await self.bot.fetch_user(uid)
            await interaction.guild.unban(user, reason=reason)
        except (ValueError, discord.NotFound):
            return await interaction.response.send_message("Could not find that banned user ID.", ephemeral=True)
        await mod_log(interaction.guild, "Member Unbanned", f"**User:** {user} (`{uid}`)\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}")
        await interaction.response.send_message(f"Unbanned **{user}**.", ephemeral=True)

    @app_commands.command(name="slowmode", description="Set channel slowmode in seconds (0 disables).")
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_channels or interaction.user.id == settings.owner_id):
            return await interaction.response.send_message("Manage Channels permission required.", ephemeral=True)
        await interaction.channel.edit(slowmode_delay=int(seconds), reason=f"Changed by {interaction.user}")
        await interaction.response.send_message(f"Slowmode set to **{seconds}s**.", ephemeral=True)

    @app_commands.command(name="lock", description="Lock the current channel for @everyone.")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        await self._set_lock(interaction, True)

    @app_commands.command(name="unlock", description="Unlock the current channel for @everyone.")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        await self._set_lock(interaction, False)

    async def _set_lock(self, interaction: discord.Interaction, locked: bool):
        if not moderation_enabled(interaction):
            return await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_channels or interaction.user.id == settings.owner_id):
            return await interaction.response.send_message("Manage Channels permission required.", ephemeral=True)
        ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False if locked else None
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow, reason=f"{'Locked' if locked else 'Unlocked'} by {interaction.user}")
        await interaction.response.send_message(f"Channel {'locked' if locked else 'unlocked'}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
