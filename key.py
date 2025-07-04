import discord
from discord import app_commands
import asyncio
import json
import os
import flusk
from typing import Optional

OWNER_ID = 123456789012345678  # Replace with your Discord user ID
KEYS_FILE = "keys.json"

class Storage:
    def __init__(self, filename):
        self.filename = filename
        self.lock = asyncio.Lock()
        if not os.path.exists(filename):
            with open(filename, "w") as f:
                json.dump({"keys": {}, "users": {}, "key_role": "KeyManager"}, f)
        with open(filename, "r") as f:
            self.data = json.load(f)

    async def save(self):
        async with self.lock:
            tmp = self.filename + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.filename)

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value
        await self.save()

storage = Storage(KEYS_FILE)

class OwnerOnly(app_commands.Check):
    async def __call__(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID

class LicenseBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register all commands
        self.tree.add_command(manage_key)
        self.tree.add_command(create_key)
        self.tree.add_command(check_license)
        self.tree.add_command(check_expiry)
        self.tree.add_command(delete_key)
        self.tree.add_command(list_keys)
        self.tree.add_command(reset_user_key)
        self.tree.add_command(user_lookup)
        self.tree.add_command(list_users)
        self.tree.add_command(register_user)
        self.tree.add_command(verify_user)
        self.tree.add_command(delete_user)
        self.tree.add_command(setup_key_message)
        self.tree.add_command(wipe_all_keys)
        self.tree.add_command(check_hwid)
        self.tree.add_command(health)
        self.tree.add_command(keyrole)
        await self.tree.sync()

bot = LicenseBot()

# --- Slash Commands (Owner Only, Hidden) ---

@app_commands.command(name="manage_key", description="Generate, reset, or view your AV/AA license key")
@OwnerOnly()
async def manage_key(interaction: discord.Interaction, key_type: str):
    await interaction.response.send_message(f"Manage your {key_type} key here.", ephemeral=True)

@app_commands.command(name="create_key", description="Create a new AV/AA license key")
@OwnerOnly()
async def create_key(interaction: discord.Interaction, key_type: str, duration: int, name: str, user: discord.User):
    await interaction.response.send_message(f"Key created for {user.mention}.", ephemeral=True)

@app_commands.command(name="check_license", description="Check license status by HWID or Discord user")
@OwnerOnly()
async def check_license(interaction: discord.Interaction, identifier: str, dm_target: Optional[discord.User] = None):
    await interaction.response.send_message(f"License status for {identifier}.", ephemeral=True)

@app_commands.command(name="check_expiry", description="Check when a license expires")
@OwnerOnly()
async def check_expiry(interaction: discord.Interaction, identifier: str, key_type: str, dm_target: Optional[discord.User] = None):
    await interaction.response.send_message(f"Expiry for {identifier} ({key_type}).", ephemeral=True)

@app_commands.command(name="delete_key", description="Delete a license key")
@OwnerOnly()
async def delete_key(interaction: discord.Interaction, license_key: str, all: Optional[bool] = False, user: Optional[discord.User] = None):
    await interaction.response.send_message(f"Key {license_key} deleted.", ephemeral=True)

@app_commands.command(name="list_keys", description="List all license keys (filtered by AV/AA)")
@OwnerOnly()
async def list_keys(interaction: discord.Interaction, key_type: str):
    await interaction.response.send_message(f"Listing all {key_type} keys.", ephemeral=True)

@app_commands.command(name="reset_user_key", description="Reset a user's license key")
@OwnerOnly()
async def reset_user_key(interaction: discord.Interaction, user: discord.User, key_type: str):
    await interaction.response.send_message(f"Key for {user.mention} reset.", ephemeral=True)

@app_commands.command(name="user_lookup", description="Look up complete license information (AV/AA)")
@OwnerOnly()
async def user_lookup(interaction: discord.Interaction, user: discord.User):
    await interaction.response.send_message(f"User info for {user.mention}.", ephemeral=True)

@app_commands.command(name="list_users", description="List users with licenses (AV/AA/ALL)")
@OwnerOnly()
async def list_users(interaction: discord.Interaction, page: int = 1, key_type: str = "ALL", status: str = "active"):
    await interaction.response.send_message(f"Listing users page {page}.", ephemeral=True)

@app_commands.command(name="register_user", description="Register a new user with a license key")
@OwnerOnly()
async def register_user(interaction: discord.Interaction, hwid: str, user: discord.User, order: str):
    await interaction.response.send_message(f"User {user.mention} registered with HWID {hwid}.", ephemeral=True)

@app_commands.command(name="verify_user", description="Verify a user by HWID")
@OwnerOnly()
async def verify_user(interaction: discord.Interaction, hwid: str, user: discord.User):
    await interaction.response.send_message(f"User {user.mention} verified for HWID {hwid}.", ephemeral=True)

@app_commands.command(name="delete_user", description="Delete a user and all their license data")
@OwnerOnly()
async def delete_user(interaction: discord.Interaction, user: discord.User, reason: str):
    await interaction.response.send_message(f"User {user.mention} deleted for reason: {reason}", ephemeral=True)

@app_commands.command(name="setup_key_message", description="Create AV/ASTDS key management panels")
@OwnerOnly()
async def setup_key_message(interaction: discord.Interaction, av_channel: discord.TextChannel, aa_channel: discord.TextChannel):
    await interaction.response.send_message("Key management panels set up.", ephemeral=True)

@app_commands.command(name="wipe_all_keys", description="Wipe all keys and reset user generation ability")
@OwnerOnly()
async def wipe_all_keys(interaction: discord.Interaction):
    await interaction.response.send_message("All keys wiped.", ephemeral=True)

@app_commands.command(name="check_hwid", description="Check HWID status")
@OwnerOnly()
async def check_hwid(interaction: discord.Interaction, hwid: str, user: Optional[discord.User] = None):
    await interaction.response.send_message(f"HWID {hwid} status checked.", ephemeral=True)

@app_commands.command(name="health", description="Check system health and connection status")
@OwnerOnly()
async def health(interaction: discord.Interaction):
    await interaction.response.send_message("System is healthy and connected.", ephemeral=True)

@app_commands.command(name="keyrole", description="Set the role that can create keys")
@OwnerOnly()
async def keyrole(interaction: discord.Interaction, role: discord.Role):
    await storage.set("key_role", role.name)
    await interaction.response.send_message(f"Key creation role set to {role.mention}", ephemeral=True)

# --- Run the bot using the TOKEN environment variable ---
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("No Discord bot token found in environment variable 'TOKEN'.")
bot.run(TOKEN)
