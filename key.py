import discord
from discord import app_commands
import asyncio
import json
import os
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import time
import threading
from flask import Flask, request, jsonify

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
OWNER_IDS = []
owner_ids_str = os.getenv("OWNER_ID", "776883692983156736")
if owner_ids_str:
    for owner_id in owner_ids_str.split(','):
        try:
            OWNER_IDS.append(int(owner_id.strip()))
        except ValueError:
            logger.warning(f"Invalid owner ID: {owner_id}")

ROLE_IDS = []
role_ids_str = os.getenv("ROLE_ID", "")
if role_ids_str:
    for role_id in role_ids_str.split(','):
        try:
            ROLE_IDS.append(int(role_id.strip()))
        except ValueError:
            logger.warning(f"Invalid role ID: {role_id}")

TOKEN = os.getenv("TOKEN", "")

class Storage:
    def __init__(self):
        self.filename = "data.json"
        self.lock = asyncio.Lock()
        self.data = self.load_data()

    def load_data(self):
        """Load data from file, create if doesn't exist"""
        try:
            if os.path.exists(self.filename):
                with open(self.filename, "r") as f:
                    return json.load(f)
            else:
                default_data = {
                    "keys": {},
                    "users": {},
                    "key_role": "KeyManager",
                    "settings": {}
                }
                self.save_sync(default_data)
                return default_data
        except Exception as e:
            logger.error(f"Error loading {self.filename}: {e}")
            return {
                "keys": {},
                "users": {},
                "key_role": "KeyManager",
                "settings": {}
            }

    def save_sync(self, data):
        """Synchronous save"""
        try:
            tmp = self.filename + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.filename)
        except Exception as e:
            logger.error(f"Error saving {self.filename}: {e}")

    async def save(self):
        """Asynchronous save with locking"""
        async with self.lock:
            try:
                tmp = self.filename + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.data, f, indent=2)
                os.replace(tmp, self.filename)
            except Exception as e:
                logger.error(f"Error saving {self.filename}: {e}")

    async def get(self, key: str, default=None):
        """Get value from storage"""
        return self.data.get(key, default)

    async def set(self, key: str, value):
        """Set value in storage"""
        self.data[key] = value
        await self.save()

storage = Storage()

class LicenseKey:
    def __init__(self, key_id: str, key_type: str, user_id: int, hwid: str, 
                 expires_at: datetime, created_at: datetime, name: str = ""):
        self.key_id = key_id
        self.key_type = key_type
        self.user_id = user_id
        self.hwid = hwid
        self.expires_at = expires_at
        self.created_at = created_at
        self.name = name

    def to_dict(self):
        return {
            "key_id": self.key_id,
            "key_type": self.key_type,
            "user_id": self.user_id,
            "hwid": self.hwid,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "name": self.name
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        return cls(
            key_id=data["key_id"],
            key_type=data["key_type"],
            user_id=data["user_id"],
            hwid=data["hwid"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            name=data.get("name", "")
        )

    def is_expired(self):
        if self.expires_at.year >= 9999:
            return False
        return datetime.now() > self.expires_at

    def days_until_expiry(self):
        if self.expires_at.year >= 9999:
            return '∞'
        delta = self.expires_at - datetime.now()
        return delta.days

class KeyManager:
    @staticmethod
    def generate_key(key_type: str, user_id: int, hwid: str) -> str:
        """Generate a unique license key"""
        timestamp = str(int(time.time()))
        unique_str = f"{key_type}-{user_id}-{hwid}-{timestamp}"
        hash_obj = hashlib.sha256(unique_str.encode())
        key_hash = hash_obj.hexdigest()[:16].upper()
        return f"{key_type}-{key_hash}"

    @staticmethod
    async def create_key(key_type: str, user_id: int, hwid: str, duration_days: int, name: str = "") -> LicenseKey:
        key_id = KeyManager.generate_key(key_type, user_id, hwid)
        if duration_days == 0:
            expires_at = datetime(year=9999, month=12, day=31)
        else:
            expires_at = datetime.now() + timedelta(days=duration_days)
        created_at = datetime.now()
        license_key = LicenseKey(
            key_id=key_id,
            key_type=key_type,
            user_id=user_id,
            hwid=hwid,
            expires_at=expires_at,
            created_at=created_at,
            name=name
        )
        keys_data = await storage.get("keys", {})
        keys_data[key_id] = license_key.to_dict()
        await storage.set("keys", keys_data)
        users_data = await storage.get("users", {})
        user_key = str(user_id)
        if user_key not in users_data:
            users_data[user_key] = {"discord_id": user_id, "keys": {}, "hwids": []}
        users_data[user_key]["keys"][key_id] = {
            "key_type": key_type,
            "expires_at": expires_at.isoformat(),
            "hwid": hwid
        }
        if hwid not in users_data[user_key]["hwids"]:
            users_data[user_key]["hwids"].append(hwid)
        await storage.set("users", users_data)
        logger.info(f"Created {key_type} key {key_id} for user {user_id}")
        return license_key

    @staticmethod
    async def get_key(key_id: str) -> Optional[LicenseKey]:
        """Get a license key by ID"""
        keys_data = await storage.get("keys", {})
        if key_id in keys_data:
            return LicenseKey.from_dict(keys_data[key_id])
        return None

    @staticmethod
    async def delete_key(key_id: str) -> bool:
        """Delete a license key"""
        keys_data = await storage.get("keys", {})
        if key_id in keys_data:
            key_info = keys_data[key_id]
            user_id = str(key_info["user_id"])
            
            del keys_data[key_id]
            await storage.set("keys", keys_data)
            
            users_data = await storage.get("users", {})
            if user_id in users_data and key_id in users_data[user_id]["keys"]:
                del users_data[user_id]["keys"][key_id]
                await storage.set("users", users_data)
            
            logger.info(f"Deleted key {key_id}")
            return True
        return False

    @staticmethod
    async def get_user_keys(user_id: int) -> List[LicenseKey]:
        """Get all keys for a user"""
        keys_data = await storage.get("keys", {})
        user_keys = []
        
        for key_id, key_info in keys_data.items():
            if key_info["user_id"] == user_id:
                user_keys.append(LicenseKey.from_dict(key_info))
        
        return user_keys

    @staticmethod
    async def get_keys_by_type(key_type: str) -> List[LicenseKey]:
        """Get all keys of a specific type"""
        keys_data = await storage.get("keys", {})
        type_keys = []
        
        for key_id, key_info in keys_data.items():
            if key_info["key_type"] == key_type:
                type_keys.append(LicenseKey.from_dict(key_info))
        
        return type_keys

    @staticmethod
    async def validate_hwid(hwid: str, user_id: int) -> bool:
        """Validate if HWID belongs to user"""
        users_data = await storage.get("users", {})
        user_key = str(user_id)
        
        if user_key in users_data:
            return hwid in users_data[user_key]["hwids"]
        return False

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id in OWNER_IDS

async def has_key_role(interaction: discord.Interaction) -> bool:
    """Check if user has the key management role"""
    if is_owner(interaction):
        return True
    
    # Check if user has any of the configured role IDs
    if ROLE_IDS and hasattr(interaction, 'guild') and interaction.guild:
        member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            user_role_ids = [role.id for role in member.roles]
            if any(role_id in user_role_ids for role_id in ROLE_IDS):
                return True
    
    # Legacy role name check
    key_role_name = await storage.get("key_role", "KeyManager")
    if hasattr(interaction, 'guild') and interaction.guild:
        member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            user_roles = [role.name for role in member.roles]
            return key_role_name in user_roles
    
    return False

# Only allow this role or owners
ASTDS_ROLE_ID = 1378078542457344061

def has_astds_access(interaction: discord.Interaction) -> bool:
    if is_owner(interaction):
        return True
    if hasattr(interaction, 'guild') and interaction.guild:
        member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id == ASTDS_ROLE_ID for role in member.roles)
    return False

def create_embed(title: str, description: str, color: int = 0x00ff00) -> discord.Embed:
    """Create a Discord embed"""
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = datetime.now()
    return embed

def create_error_embed(title: str, description: str) -> discord.Embed:
    """Create an error embed"""
    return create_embed(title, description, color=0xff0000)

def parse_duration(duration_str: str) -> Optional[int]:
    """Parse duration string like '1y2m3d4h' into total days (int). Returns None if invalid."""
    import re
    if duration_str.lower() in ("permanent", "never", "0"):
        return 0
    pattern = r"(?:(\d+)y)?(?:(\d+)m)?(?:(\d+)d)?(?:(\d+)h)?"
    match = re.fullmatch(pattern, duration_str.strip().lower())
    if not match:
        return None
    years, months, days, hours = match.groups(default="0")
    total_days = int(years) * 365 + int(months) * 30 + int(days)
    # We'll store hours as a float fraction of a day
    total_days += int(hours) / 24
    return int(total_days) if total_days > 0 else 0

class LicenseBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        """Setup and sync commands"""
        logger.info("Setting up bot commands...")
        await self.tree.sync()
        logger.info("Commands synced successfully")

    async def on_ready(self):
        if self.user:
            logger.info(f'Bot logged in as {self.user} (ID: {self.user.id})')
        logger.info(f'Connected to {len(self.guilds)} guilds')

    async def on_error(self, event, *args, **kwargs):
        logger.error(f'Error in {event}: {args}', exc_info=True)

class ASTDSPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Manage Your ASTDS License Key", style=discord.ButtonStyle.primary, custom_id="manage_astds_key")
    async def manage_astds_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_astds_access(interaction):
            await interaction.response.send_message(
                "You don't have the required role to manage ASTDS keys.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select an option to manage your ASTDS license key:",
            view=ASTDSOptionsView(),
            ephemeral=True
        )

class ASTDSOptionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(ASTDSGenerateKeyButton())
        self.add_item(ASTDSResetKeyButton())
        self.add_item(ASTDSViewKeyButton())

class ASTDSGenerateKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Generate Key", style=discord.ButtonStyle.primary, custom_id="generate_astds_key")
    async def callback(self, interaction: discord.Interaction):
        if not has_astds_access(interaction):
            await interaction.response.send_message("You don't have the required role to generate ASTDS keys.", ephemeral=True)
            return
        # Generate a new key for the user (if not already active)
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == "ASTDS" and not k.is_expired()]
        if existing_keys:
            await interaction.response.send_message("You already have an active ASTDS key.", ephemeral=True)
            return
        # Generate key for 1 year by default
        duration = "1y"
        days = parse_duration(duration)
        hwid = str(user.id)  # You can change this to a real HWID if needed
        license_key = await KeyManager.create_key("ASTDS", user.id, hwid, days, name="Auto-generated")
        expires_str = "Never" if days == 0 else license_key.expires_at.strftime('%Y-%m-%d %H:%M:%S')
        # DM the user the key
        try:
            dm_embed = create_embed(
                f"New ASTDS License Key",
                f"You have been granted a new ASTDS license key.\n\n"
                f"**Key ID:** `{license_key.key_id}`\n"
                f"**Duration:** {duration}\n"
                f"**Expires:** {expires_str}\n\n"
                f"Keep this key safe and do not share it with others."
            )
            await user.send(embed=dm_embed)
            await interaction.response.send_message("Key generated and sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Could not DM you the key. Please check your DM settings.", ephemeral=True)

class ASTDSResetKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Key", style=discord.ButtonStyle.danger, custom_id="reset_astds_key")
    async def callback(self, interaction: discord.Interaction):
        if not has_astds_access(interaction):
            await interaction.response.send_message("You don't have the required role to reset ASTDS keys.", ephemeral=True)
            return
        await interaction.response.send_message("[Placeholder] Key reset coming soon!", ephemeral=True)

class ASTDSViewKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="View Key", style=discord.ButtonStyle.secondary, custom_id="view_astds_key")
    async def callback(self, interaction: discord.Interaction):
        if not has_astds_access(interaction):
            await interaction.response.send_message("You don't have the required role to view ASTDS keys.", ephemeral=True)
            return
        await interaction.response.send_message("[Placeholder] Key view coming soon!", ephemeral=True)

# Start a simple web server for port support (useful for web services like Replit)
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

@app.route("/check", methods=["POST"])
def check_key():
    data = request.get_json()
    key = data.get("key", "")
    # Check if the key exists in your storage
    keys_data = storage.data.get("keys", {})
    valid = key in keys_data
    return jsonify({"valid": valid})

def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Start the web server in a background thread
web_thread = threading.Thread(target=run_web)
web_thread.daemon = True
web_thread.start()

bot = LicenseBot()

# --- Slash Commands ---

@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    """Display help information for all bot commands"""
    try:
        is_admin = await has_key_role(interaction)
        owner = is_owner(interaction)
        
        embed = discord.Embed(
            title="🔑 License Bot Commands",
            description="Here are all the available commands:",
            color=0x00ff00
        )
        
        # User commands
        embed.add_field(
            name="👤 User Commands",
            value=(
                "`/manage_key` - View or reset your AV/ASTDS license key\n"
                "`/help` - Show this help message"
            ),
            inline=False
        )
        
        # Admin commands
        if is_admin:
            embed.add_field(
                name="🔧 Admin Commands",
                value=(
                    "`/create_key` - Create a new AV/ASTDS license key\n"
                    "`/check_license` - Check license status by HWID or user\n"
                    "`/delete_key` - Delete a license key\n"
                    "`/list_keys` - List all license keys\n"
                    "`/user_lookup` - Look up license info for a user\n"
                    "`/register_user` - Register a user with HWID\n"
                    "`/check_hwid` - Check HWID status\n"
                    "`/health` - Check system health"
                ),
                inline=False
            )
        
        # Owner commands
        if owner:
            embed.add_field(
                name="👑 Owner Commands",
                value="`/keyrole` - Set the role for key management",
                inline=False
            )
        
        embed.add_field(
            name="ℹ️ Information",
            value=(
                f"**Your Access Level:** {'Owner' if owner else 'Admin' if is_admin else 'User'}\n"
                f"**Key Types:** AV, ASTDS\n"
                f"**Bot Version:** 2.0"
            ),
            inline=False
        )
        
        embed.timestamp = datetime.now()
        embed.set_footer(text="Use commands with /")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        embed = create_error_embed("Error", "An error occurred while showing help.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="manage_key", description="View or reset your AV/ASTDS license key")
@app_commands.describe(key_type="Type of key (AV or ASTDS)", action="Action to perform")
@app_commands.choices(key_type=[
    app_commands.Choice(name="AV", value="AV"),
    app_commands.Choice(name="ASTDS", value="ASTDS")
])
@app_commands.choices(action=[
    app_commands.Choice(name="View", value="view"),
    app_commands.Choice(name="Reset", value="reset")
])
async def manage_key(interaction: discord.Interaction, key_type: str, action: str):
    try:
        user_keys = await KeyManager.get_user_keys(interaction.user.id)
        matching_keys = [k for k in user_keys if k.key_type == key_type]
        
        if action == "view":
            if not matching_keys:
                embed = create_error_embed("No Key Found", f"You don't have a {key_type} license key.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            
            key = matching_keys[0]
            status = "Expired" if key.is_expired() else "Active"
            days_left = key.days_until_expiry()
            
            embed = create_embed(
                f"{key_type} License Key",
                f"**Key ID:** `{key.key_id}`\n"
                f"**Status:** {status}\n"
                f"**Days Left:** {days_left}\n"
                f"**HWID:** `{key.hwid}`\n"
                f"**Created:** {key.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"**Expires:** {key.expires_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        elif action == "reset":
            if not await has_key_role(interaction):
                embed = create_error_embed("Permission Denied", "You don't have permission to reset keys.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            
            for key in matching_keys:
                await KeyManager.delete_key(key.key_id)
            
            embed = create_embed(
                "Key Reset",
                f"Your {key_type} license key has been reset. Contact an administrator for a new key."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
    except Exception as e:
        logger.error(f"Error in manage_key: {e}")
        embed = create_error_embed("Error", "An error occurred while managing your key.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="create_key", description="Create a new AV/ASTDS license key")
@app_commands.describe(
    key_type="Type of key (AV or ASTDS)",
    duration="Duration (e.g. 1y, 1m, 1d, 1h, permanent)",
    name="Name for the key",
    user="User to create key for",
    hwid="Hardware ID"
)
@app_commands.choices(key_type=[
    app_commands.Choice(name="AV", value="AV"),
    app_commands.Choice(name="ASTDS", value="ASTDS")
])
async def create_key(interaction: discord.Interaction, key_type: str, duration: str, 
                    name: str, user: discord.User, hwid: str):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to create keys.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        days = parse_duration(duration)
        if days is None:
            embed = create_error_embed("Invalid Duration", "Duration must be like 1y, 1m, 1d, 1h, permanent, or a number of days.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if days < 0 or days > 3650:
            embed = create_error_embed("Invalid Duration", "Duration must be between 1 hour and 10 years, or 'permanent'.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == key_type and not k.is_expired()]
        if existing_keys:
            embed = create_error_embed(
                "Key Already Exists", 
                f"User {user.mention} already has an active {key_type} key."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        # If days==0, treat as permanent
        license_key = await KeyManager.create_key(key_type, user.id, hwid, days, name)
        expires_str = "Never" if days == 0 else license_key.expires_at.strftime('%Y-%m-%d %H:%M:%S')
        embed = create_embed(
            "Key Created Successfully",
            f"**Key ID:** `{license_key.key_id}`\n"
            f"**Type:** {key_type}\n"
            f"**User:** {user.mention}\n"
            f"**Duration:** {'Permanent' if days == 0 else duration}\n"
            f"**HWID:** `{hwid}`\n"
            f"**Expires:** {expires_str}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        try:
            dm_embed = create_embed(
                f"New {key_type} License Key",
                f"You have been granted a new {key_type} license key.\n\n"
                f"**Key ID:** `{license_key.key_id}`\n"
                f"**Duration:** {'Permanent' if days == 0 else duration}\n"
                f"**Expires:** {expires_str}\n\n"
                f"Keep this key safe and do not share it with others."
            )
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            logger.warning(f"Could not DM user {user.id}")
    except Exception as e:
        logger.error(f"Error in create_key: {e}")
        embed = create_error_embed("Error", "An error occurred while creating the key.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="check_license", description="Check license status by HWID or Discord user")
@app_commands.describe(identifier="HWID or Discord user mention", dm_target="User to DM results to")
async def check_license(interaction: discord.Interaction, identifier: str, 
                       dm_target: Optional[discord.User] = None):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to check licenses.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        user_id = None
        hwid = None
        
        if identifier.startswith("<@") and identifier.endswith(">"):
            user_id = int(identifier[2:-1].replace("!", ""))
        else:
            hwid = identifier
        
        keys_data = await storage.get("keys", {})
        matching_keys = []
        
        for key_id, key_info in keys_data.items():
            if (user_id and key_info["user_id"] == user_id) or \
               (hwid and key_info["hwid"] == hwid):
                matching_keys.append(LicenseKey.from_dict(key_info))
        
        if not matching_keys:
            embed = create_error_embed("No License Found", f"No license found for {identifier}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        response_lines = []
        for key in matching_keys:
            status = "Expired" if key.is_expired() else "Active"
            days_left = key.days_until_expiry()
            response_lines.append(
                f"**{key.key_type} Key:** `{key.key_id}`\n"
                f"**Status:** {status}\n"
                f"**Days Left:** {days_left}\n"
                f"**HWID:** `{key.hwid}`\n"
                f"**User:** <@{key.user_id}>\n"
                f"**Expires:** {key.expires_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
        
        embed = create_embed(
            "License Status",
            "\n".join(response_lines)
        )
        
        if dm_target:
            try:
                await dm_target.send(embed=embed)
                await interaction.response.send_message("License information sent via DM.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
    except Exception as e:
        logger.error(f"Error in check_license: {e}")
        embed = create_error_embed("Error", "An error occurred while checking the license.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="delete_key", description="Delete a license key")
@app_commands.describe(
    license_key="License key to delete",
    all="Delete all keys for user",
    user="User to delete keys for"
)
async def delete_key(interaction: discord.Interaction, license_key: str = "", 
                    all: bool = False, user: Optional[discord.User] = None):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to delete keys.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        deleted_count = 0
        
        if all and user:
            user_keys = await KeyManager.get_user_keys(user.id)
            for key in user_keys:
                if await KeyManager.delete_key(key.key_id):
                    deleted_count += 1
            
            embed = create_embed(
                "Keys Deleted",
                f"Deleted {deleted_count} keys for {user.mention}."
            )
            
        elif license_key:
            if await KeyManager.delete_key(license_key):
                deleted_count = 1
                embed = create_embed(
                    "Key Deleted",
                    f"Successfully deleted key: `{license_key}`"
                )
            else:
                embed = create_error_embed("Key Not Found", f"Key `{license_key}` not found.")
        else:
            embed = create_error_embed("Invalid Parameters", "Please provide either a license key or select 'all' with a user.")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in delete_key: {e}")
        embed = create_error_embed("Error", "An error occurred while deleting the key.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="list_keys", description="List all license keys")
@app_commands.describe(key_type="Type of key to list")
@app_commands.choices(key_type=[
    app_commands.Choice(name="AV", value="AV"),
    app_commands.Choice(name="ASTDS", value="ASTDS"),
    app_commands.Choice(name="All", value="ALL")
])
async def list_keys(interaction: discord.Interaction, key_type: str):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to list keys.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        if key_type == "ALL":
            keys = []
            keys_data = await storage.get("keys", {})
            for key_id, key_info in keys_data.items():
                keys.append(LicenseKey.from_dict(key_info))
        else:
            keys = await KeyManager.get_keys_by_type(key_type)
        
        if not keys:
            embed = create_error_embed("No Keys Found", f"No {key_type} keys found.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        keys.sort(key=lambda x: x.created_at, reverse=True)
        
        response_lines = []
        for key in keys[:10]:
            status = "Expired" if key.is_expired() else "Active"
            days_left = key.days_until_expiry()
            response_lines.append(
                f"**{key.key_type}:** `{key.key_id}` - {status} ({days_left} days) - <@{key.user_id}>"
            )
        
        if len(keys) > 10:
            response_lines.append(f"\n... and {len(keys) - 10} more keys")
        
        embed = create_embed(
            f"{key_type} License Keys ({len(keys)} total)",
            "\n".join(response_lines)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in list_keys: {e}")
        embed = create_error_embed("Error", "An error occurred while listing keys.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="user_lookup", description="Look up license information for a user")
@app_commands.describe(user="User to look up")
async def user_lookup(interaction: discord.Interaction, user: discord.User):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to lookup users.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        user_keys = await KeyManager.get_user_keys(user.id)
        users_data = await storage.get("users", {})
        user_info = users_data.get(str(user.id), {})
        
        if not user_keys and not user_info:
            embed = create_error_embed("User Not Found", f"No data found for {user.mention}.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        response_lines = [f"**User:** {user.mention}"]
        
        if user_info.get("hwids"):
            response_lines.append(f"**HWIDs:** {', '.join(user_info['hwids'])}")
        
        response_lines.append(f"**Keys:** {len(user_keys)}")
        
        for key in user_keys:
            status = "Expired" if key.is_expired() else "Active"
            days_left = key.days_until_expiry()
            response_lines.append(
                f"  • **{key.key_type}:** `{key.key_id}` - {status} ({days_left} days)"
            )
        
        embed = create_embed(
            "User Lookup",
            "\n".join(response_lines)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in user_lookup: {e}")
        embed = create_error_embed("Error", "An error occurred while looking up the user.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="register_user", description="Register a user with HWID")
@app_commands.describe(
    hwid="Hardware ID",
    user="User to register",
    order="Order number or reference"
)
async def register_user(interaction: discord.Interaction, hwid: str, user: discord.User, order: str):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to register users.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        users_data = await storage.get("users", {})
        user_key = str(user.id)
        
        if user_key not in users_data:
            users_data[user_key] = {
                "discord_id": user.id,
                "keys": {},
                "hwids": [],
                "registered_at": datetime.now().isoformat(),
                "order": order
            }
        
        if hwid not in users_data[user_key]["hwids"]:
            users_data[user_key]["hwids"].append(hwid)
        
        users_data[user_key]["order"] = order
        await storage.set("users", users_data)
        
        embed = create_embed(
            "User Registered",
            f"**User:** {user.mention}\n"
            f"**HWID:** `{hwid}`\n"
            f"**Order:** {order}\n"
            f"User is now registered and ready for license key assignment."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in register_user: {e}")
        embed = create_error_embed("Error", "An error occurred while registering the user.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="check_hwid", description="Check HWID status and associated user")
@app_commands.describe(hwid="Hardware ID to check", user="Optional user to verify HWID against")
async def check_hwid(interaction: discord.Interaction, hwid: str, user: Optional[discord.User] = None):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to check HWIDs.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        users_data = await storage.get("users", {})
        matching_users = []
        
        for user_id, user_info in users_data.items():
            if hwid in user_info.get("hwids", []):
                matching_users.append(user_id)
        
        if not matching_users:
            embed = create_error_embed("HWID Not Found", f"HWID `{hwid}` is not registered to any user.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        response_lines = [f"**HWID:** `{hwid}`"]
        
        for user_id in matching_users:
            user_info = users_data[user_id]
            user_keys = await KeyManager.get_user_keys(int(user_id))
            active_keys = [k for k in user_keys if not k.is_expired()]
            
            response_lines.append(
                f"**User:** <@{user_id}>\n"
                f"**Active Keys:** {len(active_keys)}\n"
                f"**Order:** {user_info.get('order', 'N/A')}"
            )
        
        if user:
            if str(user.id) in matching_users:
                response_lines.append(f"\n✅ HWID verified for {user.mention}")
            else:
                response_lines.append(f"\n❌ HWID NOT verified for {user.mention}")
        
        embed = create_embed(
            "HWID Status",
            "\n".join(response_lines)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in check_hwid: {e}")
        embed = create_error_embed("Error", "An error occurred while checking the HWID.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="health", description="Check system health and connection status")
async def health(interaction: discord.Interaction):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to check system health.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        keys_data = await storage.get("keys", {})
        users_data = await storage.get("users", {})
        
        total_keys = len(keys_data)
        total_users = len(users_data)
        
        active_keys = 0
        expired_keys = 0
        
        for key_id, key_info in keys_data.items():
            key = LicenseKey.from_dict(key_info)
            if key.is_expired():
                expired_keys += 1
            else:
                active_keys += 1
        
        embed = create_embed(
            "System Health",
            f"**Bot Status:** Online ✅\n"
            f"**Database Status:** Operational ✅\n"
            f"**Total Keys:** {total_keys}\n"
            f"**Active Keys:** {active_keys}\n"
            f"**Expired Keys:** {expired_keys}\n"
            f"**Total Users:** {total_users}\n"
            f"**Uptime:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in health: {e}")
        embed = create_error_embed("Error", "An error occurred while checking system health.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="keyrole", description="Set the role that can manage keys")
@app_commands.describe(role="Role to set for key management")
async def keyrole(interaction: discord.Interaction, role: discord.Role):
    try:
        if not is_owner(interaction):
            embed = create_error_embed("Permission Denied", "Only the bot owner can set the key management role.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        await storage.set("key_role", role.name)
        
        embed = create_embed(
            "Key Role Updated",
            f"Key management role set to {role.mention}\n"
            f"Users with this role can now create and manage license keys."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in keyrole: {e}")
        embed = create_error_embed("Error", "An error occurred while setting the key role.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_key_message", description="Create ASTDS key management panel")
@app_commands.describe(
    astd_channel="Channel to post ASTDS key management panel"
)
async def setup_key_message(
    interaction: discord.Interaction,
    astd_channel: Optional[discord.TextChannel] = None
):
    try:
        if not has_astds_access(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to use this command.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        sent = []
        if astd_channel:
            astd_embed = discord.Embed(
                title="\U0001F511 ASTDS License Key Management",
                description=(
                    "Manage your license key for ASTDS\n\n"
                    "**Available Options**\n"
                    "• Generate a new license key\n"
                    "• Reset your existing key (Only once)\n"
                    "• View your current key details\n\n"
                    "**Requirements**\n"
                    "You must have the **ASTDS Premium** role to use these features.\n\n"
                    "Click the button below to manage your ASTDS license key"
                ),
                color=0xFEE75C
            )
            await astd_channel.send(embed=astd_embed, view=ASTDSPanelView())
            sent.append(f"ASTDS panel sent to {astd_channel.mention}")
        if sent:
            await interaction.response.send_message("\n".join(sent), ephemeral=True)
        else:
            await interaction.response.send_message("No channel specified.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in setup_key_message: {e}")
        embed = create_error_embed("Error", "An error occurred while setting up the key message panel.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# Run the bot
if __name__ == "__main__":
    if not TOKEN:
        logger.error("No Discord bot token found. Please set the TOKEN environment variable.")
        print("Please set the TOKEN environment variable with your Discord bot token.")
        print("You can get a bot token from https://discord.com/developers/applications")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid Discord bot token. Please check your TOKEN environment variable.")
        print("Invalid Discord bot token. Please check your TOKEN environment variable.")
        exit(1)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"Failed to start bot: {e}")
        exit(1)
