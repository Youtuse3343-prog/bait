import discord


class VerificationLinkView(discord.ui.View):
    def __init__(self, url: str, label: str = "Verify with Discord"):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
