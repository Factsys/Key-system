import discord
from discord import app_commands
import asyncio
import json
import os
import logging
import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import time
import threading
from flask import Flask, request, jsonify
import shutil
from discord import File
import aiohttp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'),
              logging.StreamHandler()])
logger = logging.getLogger(__name__)

# Rate limiting for HTTP requests
request_times = []
MAX_REQUESTS_PER_MINUTE = 5

def can_make_request():
    """Check if we can make an HTTP request without hitting rate limits"""
    global request_times
    now = time.time()
    # Remove requests older than 1 minute
    request_times = [t for t in request_times if now - t < 60]

    if len(request_times) >= MAX_REQUESTS_PER_MINUTE:
        return False

    request_times.append(now)
    return True

async def add_key_to_cloudflare_safe(key: str, duration_days: int = 365):
    """Safely add key to Cloudflare with rate limiting"""
    if not can_make_request():
        logger.warning(f"Rate limit reached, skipping Cloudflare sync for key {key}")
        return True  # Return True to not block local operations

    try:
        url = "https://key-checker.yunoblasesh.workers.dev/add?token=secretkey123"
        expires = (datetime.utcnow() + timedelta(days=duration_days)).isoformat() + "Z"
        payload = {"key": key, "expires": expires}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("success"):
                        logger.info(f"Key {key} stored in Cloudflare")
                        return True
                    else:
                        logger.error(f"Cloudflare rejected key: {data.get('error')}")
                        return False
                else:
                    logger.error(f"Cloudflare HTTP {response.status}")
                    return False
    except Exception as e:
        logger.error(f"Cloudflare error: {e}")
        return False

async def delete_key_from_cloudflare_safe(key: str):
    """Safely delete key from Cloudflare with rate limiting"""
    if not can_make_request():
        logger.warning(f"Rate limit reached, skipping Cloudflare delete for key {key}")
        return True  # Return True to not block local operations

    try:
        url = "https://key-checker.yunoblasesh.workers.dev/delete?token=secretkey123"
        payload = {"key": key}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("success"):
                        logger.info(f"Key {key} deleted from Cloudflare")
                        return True
                    else:
                        logger.error(f"Cloudflare delete rejected: {data.get('error')}")
                        return False
                else:
                    logger.error(f"Cloudflare delete HTTP {response.status}")
                    return False
    except Exception as e:
        logger.error(f"Cloudflare delete error: {e}")
        return False

OWNER_IDS = []
owner_ids_str = os.getenv("OWNER_ID", "776883692983156736,829256979716898826,1334138321412296725")
if owner_ids_str:
    for owner_id in owner_ids_str.split(','):
        try:
            OWNER_IDS.append(int(owner_id.strip()))
        except ValueError:
            logger.warning(f"Invalid owner ID: {owner_id}")

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
                    "settings": {
                        "max_keys_per_user": 3,
                        "default_key_duration": "1y",
                        "max_reset_attempts": 7,
                    }
                }
                self.save_sync(default_data)
                return default_data
        except Exception as e:
            logger.error(f"Error loading {self.filename}: {e}")
            return {"keys": {}, "users": {}, "settings": {"max_reset_attempts": 7}}

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
                 expires_at: datetime, created_at: datetime, name: str = "",
                 status: str = "deactivated", resets_left: int = 3):
        self.key_id = key_id
        self.key_type = key_type
        self.user_id = user_id
        self.hwid = hwid
        self.expires_at = expires_at
        self.created_at = created_at
        self.name = name
        self.status = status
        self.resets_left = resets_left

    def to_dict(self):
        return {
            "key_id": self.key_id,
            "key_type": self.key_type,
            "user_id": self.user_id,
            "hwid": self.hwid,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "name": self.name,
            "status": self.status,
            "resets_left": self.resets_left
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
            name=data.get("name", ""),
            status=data.get("status", "deactivated"),
            resets_left=data.get("resets_left", 3)
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
        users_data = await storage.get("users", {})
        user_key = str(user_id)

        # Check resets_left for this user/key_type
        if user_key in users_data:
            resets_info = users_data[user_key].get("resets_left", {})
            resets_left_for_type = resets_info.get(key_type, 7)
            if resets_left_for_type <= 0:
                raise Exception(f"No resets left for {key_type} key.")

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
            name=name,
            status="deactivated",
            resets_left=7
        )

        keys_data = await storage.get("keys", {})
        keys_data[key_id] = license_key.to_dict()
        await storage.set("keys", keys_data)

        if user_key not in users_data:
            users_data[user_key] = {
                "discord_id": user_id,
                "keys": {},
                "hwids": [],
                "resets_left": {}
            }

        users_data[user_key]["keys"][key_id] = {
            "key_type": key_type,
            "expires_at": expires_at.isoformat(),
            "hwid": hwid,
            "status": "deactivated"
        }

        if hwid not in users_data[user_key]["hwids"]:
            users_data[user_key]["hwids"].append(hwid)

        users_data[user_key]["resets_left"].setdefault(key_type, 7)
        await storage.set("users", users_data)

        # Try to store in Cloudflare (non-blocking)
        await add_key_to_cloudflare_safe(key_id, duration_days)

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
    async def get_user_keys(user_id: int) -> List[LicenseKey]:
        """Get all keys for a user"""
        keys_data = await storage.get("keys", {})
        user_keys = []

        for key_id, key_info in keys_data.items():
            if key_info["user_id"] == user_id:
                user_keys.append(LicenseKey.from_dict(key_info))

        return user_keys

    @staticmethod
    async def reset_key(key_id: str) -> bool:
        """Reset a license key: delete the key from local storage"""
        keys_data = await storage.get("keys", {})
        if key_id in keys_data:
            key_info = keys_data[key_id]
            user_id = str(key_info["user_id"])
            key_type = key_info["key_type"]

            # Try to delete from Cloudflare (non-blocking)
            await delete_key_from_cloudflare_safe(key_id)

            # Remove from local keys storage
            del keys_data[key_id]
            await storage.set("keys", keys_data)

            # Remove from users and decrement resets
            users_data = await storage.get("users", {})
            if user_id in users_data and key_id in users_data[user_id]["keys"]:
                del users_data[user_id]["keys"][key_id]
                # Decrement resets_left for this key_type
                resets_info = users_data[user_id].setdefault("resets_left", {})
                resets_info[key_type] = max(0, resets_info.get(key_type, 7) - 1)
                await storage.set("users", users_data)

            logger.info(f"Key {key_id} reset successfully")
            return True
        return False

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id in OWNER_IDS

def create_embed(title: str, description: str, color: int = 0xff69b4) -> discord.Embed:
    """Create a Discord embed with pink color"""
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = datetime.now()
    return embed

def create_error_embed(title: str, description: str) -> discord.Embed:
    """Create an error embed"""
    return create_embed(title, description, color=0xff0000)

def parse_duration(duration_str: str) -> Optional[int]:
    """Parse duration string like '1y2m3d4h' into total days (int). Returns None if invalid."""
    if duration_str.lower() in ("permanent", "never", "0"):
        return 0
    pattern = r"(?:(\d+)y)?(?:(\d+)m)?(?:(\d+)d)?(?:(\d+)h)?"
    match = re.fullmatch(pattern, duration_str.strip().lower())
    if not match:
        return None
    years, months, days, hours = match.groups(default="0")
    total_days = int(years) * 365 + int(months) * 30 + int(days)
    total_days += int(hours) / 24
    return int(total_days) if total_days > 0 else 0

def safe_send_response(interaction, *args, **kwargs):
    try:
        if not interaction.response.is_done():
            return interaction.response.send_message(*args, **kwargs)
        else:
            return interaction.followup.send(*args, **kwargs)
    except Exception:
        pass

class LicenseBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
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

# Simple web server for keeping alive
app = Flask(__name__)

@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>License Bot</title>
        <style>
            body { 
                font-family: Arial, sans-serif; 
                background: #2c2f33; 
                color: white; 
                text-align: center; 
                padding: 50px; 
            }
            .status { 
                color: #7289da; 
                font-size: 24px; 
                margin: 20px 0; 
            }
        </style>
    </head>
    <body>
        <h1>🤖 License Bot</h1>
        <div class="status">✅ Bot is running!</div>
        <p>Essential key management only</p>
    </body>
    </html>
    """

@app.route("/check", methods=["POST"])
def check_key():
    data = request.get_json()
    key = data.get("key", "")
    hwid = data.get("hwid", "")

    keys_data = storage.data.get("keys", {})
    if key in keys_data:
        key_info = keys_data[key]

        # Check if key is expired
        try:
            expires_at = datetime.fromisoformat(key_info["expires_at"])
            if expires_at.year < 9999 and expires_at <= datetime.now():
                return jsonify({"valid": False, "message": "Key expired"})
        except Exception:
            return jsonify({"valid": False, "message": "Invalid key data"})

        # If key is activated, check HWID match
        if key_info.get("status", "deactivated") == "activated":
            if hwid and key_info.get("hwid", "") != hwid:
                return jsonify({"valid": False, "message": "Key is registered to another computer"})

        try:
            if expires_at.year >= 9999:
                days_left = '∞'
            else:
                delta = expires_at - datetime.now()
                days_left = delta.days
        except Exception:
            days_left = None

        resp = {
            "valid": True,
            "key_id": key_info.get("key_id", key),
            "key_type": key_info.get("key_type", ""),
            "user_id": key_info.get("user_id", ""),
            "hwid": key_info.get("hwid", ""),
            "status": key_info.get("status", "deactivated"),
            "expires_at": key_info.get("expires_at", ""),
            "created_at": key_info.get("created_at", ""),
            "name": key_info.get("name", ""),
            "days_left": days_left
        }
        return jsonify(resp)
    return jsonify({"valid": False, "message": "Key not found"})

@app.route("/activate", methods=["POST"])
def activate_key_api():
    data = request.get_json()
    key = data.get("key", "")
    hwid = data.get("hwid", "")

    if not hwid:
        return jsonify({"success": False, "message": "HWID required for activation."})

    keys_data = storage.data.get("keys", {})
    if key in keys_data:
        key_info = keys_data[key]

        # Check if key is expired
        try:
            expires_at = datetime.fromisoformat(key_info["expires_at"])
            if expires_at.year < 9999 and expires_at <= datetime.now():
                return jsonify({"success": False, "message": "Key expired."})
        except Exception:
            return jsonify({"success": False, "message": "Invalid key data."})

        # If key is already activated, check HWID match
        if key_info.get("status", "deactivated") == "activated":
            if key_info.get("hwid", "") != hwid:
                return jsonify({"success": False, "message": "Key is already registered to another computer."})
            else:
                return jsonify({"success": True, "message": "Key already activated on this computer."})

        # Key is not activated yet, activate it with this HWID
        key_info["status"] = "activated"
        key_info["hwid"] = hwid
        keys_data[key] = key_info
        storage.save_sync(storage.data)
        return jsonify({"success": True, "message": "Key activated successfully."})

    return jsonify({"success": False, "message": "Key not found."})

def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Start the web server in a background thread
web_thread = threading.Thread(target=run_web)
web_thread.daemon = True
web_thread.start()

bot = LicenseBot()

# SIMPLIFIED SLASH COMMANDS - ONLY ESSENTIALS

@bot.tree.command(name="view_key", description="View your license key")
@app_commands.describe(key_type="Type of key to view")
@app_commands.choices(key_type=[
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ASTD", value="ASTD"),
    app_commands.Choice(name="ALS", value="ALS")
])
async def view_key(interaction: discord.Interaction, key_type: str):
    try:
        user_keys = await KeyManager.get_user_keys(interaction.user.id)
        matching_keys = [k for k in user_keys if k.key_type == key_type]

        if not matching_keys:
            embed = create_error_embed("No Key Found", f"You don't have a {key_type} license key.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        key = matching_keys[0]
        status = "Expired" if key.is_expired() else ("Activated" if key.status == "activated" else "Deactivated")
        days_left = key.days_until_expiry()
        resets_left = "∞" if key.resets_left >= 999999 else key.resets_left

        embed = create_embed(
            f"{key_type} License Key",
            f"**Key ID:** `{key.key_id}`\n"
            f"**Status:** {status}\n"
            f"**Days Left:** {days_left}\n"
            f"**HWID:** `{key.hwid or 'Not Set'}`\n"
            f"**Created:** {key.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Expires:** {key.expires_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Resets Left:** {resets_left}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in view_key: {e}")
        embed = create_error_embed("Error", "An error occurred while viewing your key.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reset_key", description="Reset your license key (USE CAREFULLY - LIMITED RESETS)")
@app_commands.describe(key_type="Type of key to reset")
@app_commands.choices(key_type=[
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ASTD", value="ASTD"),
    app_commands.Choice(name="ALS", value="ALS")
])
async def reset_key(interaction: discord.Interaction, key_type: str):
    try:
        user_keys = await KeyManager.get_user_keys(interaction.user.id)
        matching_keys = [k for k in user_keys if k.key_type == key_type]

        if not matching_keys:
            embed = create_error_embed("No Key Found", f"You don't have a {key_type} key to reset.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Check resets_left before attempting reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(interaction.user.id), {})
        current_resets = user_data.get("resets_left", {}).get(key_type, 7)

        if current_resets <= 0:
            embed = create_error_embed("No Resets Left", "You have no resets left for this key.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        key = matching_keys[0]
        result = await KeyManager.reset_key(key.key_id)

        if result:
            # Fetch updated resets_left after reset
            users_data = await storage.get("users", {})
            user_data = users_data.get(str(interaction.user.id), {})
            resets_left = user_data.get("resets_left", {}).get(key_type, 7)

            embed = create_embed(
                "Key Reset",
                f"Your {key_type} license key has been reset and deleted. You can now generate a new one.\n"
                f"**Resets Left:** {resets_left}"
            )
        else:
            embed = create_error_embed("Reset Failed", "Failed to reset key.")

        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in reset_key: {e}")
        embed = create_error_embed("Error", "An error occurred while resetting your key.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="create_key", description="Create a new license key (Owner only)")
@app_commands.describe(
    key_type="Type of key",
    user="User to create key for", 
    duration="Duration (e.g. 1y, 1m, 1d, permanent)"
)
@app_commands.choices(key_type=[
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ASTD", value="ASTD"), 
    app_commands.Choice(name="ALS", value="ALS")
])
async def create_key(interaction: discord.Interaction, key_type: str, user: discord.User, duration: str):
    try:
        if not is_owner(interaction):
            embed = create_error_embed("Permission Denied", "Only the bot owner can create keys.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        days = parse_duration(duration)
        if days is None:
            embed = create_error_embed("Invalid Duration", "Duration must be like 1y, 1m, 1d, 1h, permanent, or a number of days.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Auto-generate HWID
        hwid = hashlib.sha256(f"computer_{user.id}_{user.name}".encode()).hexdigest()[:16]

        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == key_type and not k.is_expired()]

        if existing_keys:
            embed = create_error_embed("Key Already Exists", f"User {user.mention} already has an active {key_type} key.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        license_key = await KeyManager.create_key(key_type, user.id, hwid, days, "Owner-generated")
        expires_str = "Never" if days == 0 else license_key.expires_at.strftime('%Y-%m-%d %H:%M:%S')

        embed = create_embed(
            "Key Created Successfully",
            f"**Key ID:** `{license_key.key_id}`\n"
            f"**Type:** {key_type}\n"
            f"**User:** {user.mention}\n"
            f"**Duration:** {'Permanent' if days == 0 else duration}\n"
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

@bot.tree.command(name="help", description="Show available commands")
async def help_command(interaction: discord.Interaction):
    try:
        owner = is_owner(interaction)

        embed = discord.Embed(
            title="🔑 License Bot Commands (Essential Mode)",
            description="Simplified bot with only key management features:",
            color=0xff69b4
        )

        # User Commands
        embed.add_field(
            name="👤 User Commands",
            value=(
                "`/view_key` - View your license key details\n"
                "`/reset_key` - Reset your license key ⚠️ LIMITED RESETS\n"
                "`/help` - Show this help message"
            ),
            inline=False
        )

        # Owner Commands
        if owner:
            embed.add_field(
                name="👑 Owner Commands",
                value="`/create_key` - Create a new license key for a user",
                inline=False
            )

        embed.add_field(
            name="⚠️ Important Notes",
            value=(
                "• This bot is in **essential mode** to prevent rate limiting\n"
                "• Only basic key viewing and resetting is available\n"
                "• **Resets are limited** - use them carefully!\n"
                "• All other features are temporarily disabled"
            ),
            inline=False
        )

        embed.timestamp = datetime.now()
        embed.set_footer(text="Essential mode - reduced functionality")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        await interaction.response.send_message("Help command failed due to rate limiting.", ephemeral=True)

# Run the bot
if __name__ == "__main__":
    if not TOKEN:
        logger.error("No Discord bot token found. Please set the TOKEN environment variable.")
        print("Please set the TOKEN environment variable with your Discord bot token.")
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
