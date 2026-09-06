from __future__ import annotations

import re
import discord


class RoleActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolebtn:(?P<guild_id>\d+):(?P<role_id>\d+):(?P<mode>add|remove|toggle)"):
    def __init__(self, guild_id: int, role_id: int, mode: str = "toggle", *, label: str = "Toggle role", emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        self.guild_id = int(guild_id); self.role_id = int(role_id); self.mode = mode if mode in {"add","remove","toggle"} else "toggle"
        super().__init__(discord.ui.Button(label=label[:80], emoji=emoji or None, style=style, custom_id=f"rolebtn:{self.guild_id}:{self.role_id}:{self.mode}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["guild_id"]), int(match["role_id"]), match["mode"], label=item.label or "Toggle role", emoji=str(item.emoji) if item.emoji else None, style=item.style)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This role button is not available here.", ephemeral=True)
        role = interaction.guild.get_role(self.role_id)
        me = interaction.guild.me
        if not role:
            return await interaction.response.send_message("That role no longer exists.", ephemeral=True)
        if role.managed or not me or role >= me.top_role or not me.guild_permissions.manage_roles:
            return await interaction.response.send_message("The bot cannot manage that role. Check role hierarchy and Manage Roles.", ephemeral=True)
        try:
            if self.mode == "add":
                if role not in interaction.user.roles: await interaction.user.add_roles(role, reason="Message role button")
                text = f"Added {role.mention}."
            elif self.mode == "remove":
                if role in interaction.user.roles: await interaction.user.remove_roles(role, reason="Message role button")
                text = f"Removed {role.mention}."
            elif role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Message role toggle"); text = f"Removed {role.mention}."
            else:
                await interaction.user.add_roles(role, reason="Message role toggle"); text = f"Added {role.mention}."
            await interaction.response.send_message(text, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I cannot manage that role.", ephemeral=True)
