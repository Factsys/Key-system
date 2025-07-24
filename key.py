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
import requests

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

# Configuration - Dual URL support for redundancy
CLOUDFLARE_URLS = [
    "https://key-checker.yunoblasesh.workers.dev/add?token=secretkey123",
    "https://factsy.yunoblasesh.workers.dev/add?token=secretkey123"
]

def add_key_to_cloudflare(key: str, duration_days: int = 365):
    """
    Sends a generated key with expiry date to multiple Cloudflare Worker APIs for redundancy.
    """
    expires = (datetime.utcnow() + timedelta(days=duration_days)).isoformat() + "Z"
    payload = {
        "key": key,
        "expires": expires
    }
    
    success_count = 0
    total_urls = len(CLOUDFLARE_URLS)
    
    for i, url in enumerate(CLOUDFLARE_URLS):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                logger.info(f"[OK] Key {key} stored in Cloudflare URL {i+1}/{total_urls}. Expires: {expires}")
                success_count += 1
            else:
                logger.error(f"[ERROR] Failed to store key {key} at URL {i+1}/{total_urls}. Response: {response.text}")
        except Exception as e:
            logger.error(f"[ERROR] Cloudflare request failed for URL {i+1}/{total_urls}: {e}")
    
    # Consider it successful if at least one URL worked
    if success_count > 0:
        logger.info(f"[OK] Key {key} successfully stored in {success_count}/{total_urls} Cloudflare endpoints")
        return True
    else:
        logger.error(f"[ERROR] Failed to store key {key} in all {total_urls} Cloudflare endpoints")
        return False

OWNER_IDS = []
owner_ids_str = os.getenv("OWNER_ID", "776883692983156736,829256979716898826,1334138321412296725")
if owner_ids_str:
    for owner_id in owner_ids_str.split(','):
        try:
            OWNER_IDS.append(int(owner_id.strip()))
        except ValueError:
            logger.warning(f"Invalid owner ID: {owner_id}")

ROLE_IDS = []
role_ids_str = os.getenv("ROLE_ID", "1378078542457344061")
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
                    "settings": {
                        "max_keys_per_user": 3,
                        "default_key_duration": "1y",
                        "auto_expire_cleanup": True,
                        "require_hwid_verification": True,
                        "allow_key_sharing": False,
                        "maintenance_mode": False,
                        "key_generation_cooldown": 300,
                        "max_reset_attempts": 7,
                        "backup_enabled": True,
                        "audit_log_enabled": True
                    }
                }
                self.save_sync(default_data)
                return default_data
        except Exception as e:
            logger.error(f"Error loading {self.filename}: {e}")
            return {
                "keys": {},
                "users": {},
                "key_role": "KeyManager",
                "settings": {
                    "max_keys_per_user": 3,
                    "default_key_duration": "1y",
                    "auto_expire_cleanup": True,
                    "require_hwid_verification": True,
                    "allow_key_sharing": False,
                    "maintenance_mode": False,
                    "key_generation_cooldown": 300,
                    "max_reset_attempts": 7,
                    "backup_enabled": True,
                    "audit_log_enabled": True
                }
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
                 expires_at: datetime, created_at: datetime, name: str = "", status: str = "deactivated", resets_left: int = 3):
        self.key_id = key_id
        self.key_type = key_type
        self.user_id = user_id
        self.hwid = hwid
        self.expires_at = expires_at
        self.created_at = created_at
        self.name = name
        self.status = status  # "activated" or "deactivated"
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
    async def create_key(key_type: str, user_id: int, hwid: str, duration_days: int, name: str = "", resets_left: int = None, unlimited_resets: bool = False) -> LicenseKey:
        users_data = await storage.get("users", {})
        user_key = str(user_id)
        # Check resets_left for this user/key_type
        if not unlimited_resets:
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
        if resets_left is None:
            if not unlimited_resets:
                if user_key in users_data:
                    resets_info = users_data[user_key].get("resets_left", {})
                    resets_left = resets_info.get(key_type, 7)
                else:
                    resets_left = 7
            else:
                resets_left = 999999
        license_key = LicenseKey(
            key_id=key_id,
            key_type=key_type,
            user_id=user_id,
            hwid=hwid,
            expires_at=expires_at,
            created_at=created_at,
            name=name,
            status="deactivated",
            resets_left=resets_left
        )
        keys_data = await storage.get("keys", {})
        keys_data[key_id] = license_key.to_dict()
        await storage.set("keys", keys_data)
        if user_key not in users_data:
            users_data[user_key] = {"discord_id": user_id, "keys": {}, "hwids": [], "resets_left": {}}
        users_data[user_key]["keys"][key_id] = {
            "key_type": key_type,
            "expires_at": expires_at.isoformat(),
            "hwid": hwid,
            "status": "deactivated"
        }
        if hwid not in users_data[user_key]["hwids"]:
            users_data[user_key]["hwids"].append(hwid)

        # Save resets_left for this key_type
        if "resets_left" not in users_data[user_key]:
            users_data[user_key]["resets_left"] = {}
        if not unlimited_resets:
            users_data[user_key]["resets_left"].setdefault(key_type, 7)
        else:
            users_data[user_key]["resets_left"][key_type] = 999999
        await storage.set("users", users_data)
        
        # Store key in Cloudflare
        cloudflare_success = add_key_to_cloudflare(key_id, duration_days)
        if cloudflare_success:
            logger.info(f"Key {key_id} successfully stored in both local DB and Cloudflare")
        else:
            logger.warning(f"Key {key_id} stored locally but failed to store in Cloudflare")
        
        logger.info(f"Created {key_type} key {key_id} for user {user_id}")
        return license_key

    @staticmethod
    async def activate_key(key_id: str) -> bool:
        keys_data = await storage.get("keys", {})
        if key_id in keys_data:
            keys_data[key_id]["status"] = "activated"
            await storage.set("keys", keys_data)
            users_data = await storage.get("users", {})
            user_id = str(keys_data[key_id]["user_id"])
            if user_id in users_data and key_id in users_data[user_id]["keys"]:
                users_data[user_id]["keys"][key_id]["status"] = "activated"
                await storage.set("users", users_data)
            return True
        return False

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

    @staticmethod
    async def reset_key(key_id: str, unlimited_resets: bool = False) -> bool:
        """Reset a license key: delete the key and decrement resets_left for the user/key_type."""
        keys_data = await storage.get("keys", {})
        if key_id in keys_data:
            key_info = keys_data[key_id]
            user_id = str(key_info["user_id"])
            key_type = key_info["key_type"]
            # Remove from keys
            del keys_data[key_id]
            await storage.set("keys", keys_data)
            # Remove from users
            users_data = await storage.get("users", {})
            if user_id in users_data and key_id in users_data[user_id]["keys"]:
                del users_data[user_id]["keys"][key_id]
                # Decrement resets_left for this key_type
                if not unlimited_resets:
                    resets_info = users_data[user_id].setdefault("resets_left", {})
                    resets_info[key_type] = max(0, resets_info.get(key_type, 7) - 1)
                await storage.set("users", users_data)
            return True
        return False

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id in OWNER_IDS

async def has_key_role(interaction: discord.Interaction) -> bool:
    """Check if user has the key management role"""
    if is_owner(interaction):
        return True
    # Always get up-to-date member object
    member = None
    if hasattr(interaction, 'guild') and interaction.guild:
        member = await interaction.guild.fetch_member(interaction.user.id)
    # Check if user has any of the configured role IDs
    if ROLE_IDS and member and member.roles:
        user_role_ids = [role.id for role in member.roles]
        if any(role_id in user_role_ids for role_id in ROLE_IDS):
            return True
    # Legacy role name check
    key_role_name = await storage.get("key_role", "KeyManager")
    if member and member.roles:
        user_roles = [role.name for role in member.roles]
        return key_role_name in user_roles
    return False

# Global role ID lists
MANAGER_ROLE_IDS = []
EXCLUSIVE_ROLE_IDS = []
ASTD_ROLE_ID = 1378078542457344061

async def has_manager_role(interaction: discord.Interaction) -> bool:
    """Check if user has manager role"""
    if hasattr(interaction, 'guild') and interaction.guild:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id in MANAGER_ROLE_IDS for role in member.roles)
    return False

async def has_exclusive_role(interaction: discord.Interaction) -> bool:
    """Check if user has exclusive role"""
    if hasattr(interaction, 'guild') and interaction.guild:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id in EXCLUSIVE_ROLE_IDS for role in member.roles)
    return False

async def has_astd_bypass_role(interaction: discord.Interaction) -> bool:
    """Bypass for ASTD role ID 1378078542457344061"""
    if hasattr(interaction, 'guild') and interaction.guild:
        # Always fetch up-to-date member object
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id == 1378078542457344061 for role in member.roles)
    return False

async def has_astd_access(interaction: discord.Interaction) -> bool:
    """Check if user has access to ASTD features - requires ASTD role"""
    if is_owner(interaction):
        return True
    if await has_astd_bypass_role(interaction):
        return True
    # Check for ASTD role (1393695198940368936) or Mango Prem role (1378078542457344061)
    if hasattr(interaction, 'guild') and interaction.guild:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id in [1393695198940368936, 1378078542457344061] for role in member.roles)
    return False

async def has_als_access(interaction: discord.Interaction) -> bool:
    """Check if user has access to ALS features - requires ALS role"""
    if is_owner(interaction):
        return True
    if await has_astd_bypass_role(interaction):
        return True
    # Check for ALS role (1393695013401264300) or Mango Prem role (1378078542457344061)
    if hasattr(interaction, 'guild') and interaction.guild:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id in [1393695013401264300, 1378078542457344061] for role in member.roles)
    return False

async def has_gag_access(interaction: discord.Interaction) -> bool:
    """Check if user has access to GAG features - requires GAG role"""
    if is_owner(interaction):
        return True
    if await has_astd_bypass_role(interaction):
        return True
    # Check for GAG role (1395810570497687774) or Mango Prem role (1378078542457344061)
    if hasattr(interaction, 'guild') and interaction.guild:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.guild.get_member(interaction.user.id)
        if member and member.roles:
            return any(role.id in [1395810570497687774, 1378078542457344061] for role in member.roles)
    return False

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

# Utility to resolve role from mention or ID
async def resolve_role(guild, role_input):
    if isinstance(role_input, discord.Role):
        return role_input
    if isinstance(role_input, str):
        # Mention format <@&roleid>
        if role_input.startswith('<@&') and role_input.endswith('>'):
            role_id = int(role_input[3:-1])
            return guild.get_role(role_id)
        # Try as ID
        try:
            role_id = int(role_input)
            return guild.get_role(role_id)
        except Exception:
            pass
        # Try by name
        for role in guild.roles:
            if role.name == role_input:
                return role
    return None

# Utility to resolve channel from mention or ID
async def resolve_channel(guild, channel_input):
    if isinstance(channel_input, discord.TextChannel):
        return channel_input
    if isinstance(channel_input, str):
        # Mention format <#channelid>
        if channel_input.startswith('<#') and channel_input.endswith('>'):
            channel_id = int(channel_input[2:-1])
            return guild.get_channel(channel_id)
        # Try as ID
        try:
            channel_id = int(channel_input)
            return guild.get_channel(channel_id)
        except Exception:
            pass
        # Try by name
        for channel in guild.text_channels:
            if channel.name == channel_input:
                return channel
    return None

def safe_send_response(interaction, *args, **kwargs):
    try:
        if not interaction.response.is_done():
            return interaction.response.send_message(*args, **kwargs)
        else:
            return interaction.followup.send(*args, **kwargs)
    except Exception:
        # If both fail, ignore
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

class ASTDPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Manage Your ASTD License Key", style=discord.ButtonStyle.primary, custom_id="manage_astd_key")
    async def manage_astd_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await has_astd_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to manage ASTD keys.", ephemeral=True)
            return
        await safe_send_response(
            interaction,
            "Select an option to manage your ASTD license key:",
            view=ASTDOptionsView(),
            ephemeral=True
        )

class ASTDOptionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Permanent
        self.add_item(ASTDGenerateKeyButton())
        self.add_item(ASTDResetKeyButton())
        self.add_item(ASTDViewKeyButton())

class ASTDGenerateKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Generate Key", style=discord.ButtonStyle.primary, custom_id="generate_astd_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_astd_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to generate ASTD keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == "ASTD" and not k.is_expired()]
        if existing_keys:
            await safe_send_response(interaction, "You already have an active ASTD key.", ephemeral=True)
            return
        duration = "1y"
        days = parse_duration(duration)
        # Generate a computer-based HWID instead of using Discord ID
        import hashlib
        computer_hwid = hashlib.sha256(f"computer_{user.id}_{user.name}".encode()).hexdigest()[:16]
        license_key = await KeyManager.create_key("ASTD", user.id, computer_hwid, days, name="Auto-generated")
        expires_str = "Never" if days == 0 else license_key.expires_at.strftime('%Y-%m-%d %H:%M:%S')
        try:
            dm_embed = create_embed(
                f"New ASTD License Key",
                f"You have been granted a new ASTD license key.\n\n"
                f"**Key ID:** `{license_key.key_id}`\n"
                f"**Duration:** {duration}\n"
                f"**Expires:** {expires_str}\n\n"
                f"Keep this key safe and do not share it with others."
            )
            await user.send(embed=dm_embed)
            await safe_send_response(interaction, "Key generated and sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await safe_send_response(interaction, "Could not DM you the key. Please check your DM settings.", ephemeral=True)

class ASTDResetKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Key", style=discord.ButtonStyle.danger, custom_id="reset_astd_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_astd_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to reset ASTD keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        matching_keys = [k for k in user_keys if k.key_type == "ASTD"]
        if not matching_keys:
            await safe_send_response(interaction, "You don't have an ASTD key to reset.", ephemeral=True)
            return
        unlimited = is_owner(interaction) or await has_manager_role(interaction) or await has_exclusive_role(interaction)

        # Check resets_left before attempting reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(user.id), {})
        current_resets = user_data.get("resets_left", {}).get("ASTD", 7)

        if not unlimited and current_resets <= 0:
            embed = create_error_embed("No Resets Left", "You have no resets left for this key.")
            await safe_send_response(interaction, embed=embed, ephemeral=True)
            return

        reset_results = []
        error = None
        for key in matching_keys:
            try:
                result = await KeyManager.reset_key(key.key_id, unlimited_resets=unlimited)
                reset_results.append((key, result))
            except Exception as e:
                error = str(e)
                reset_results.append((key, False))

        # Fetch updated resets_left after reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(user.id), {})
        resets_left = user_data.get("resets_left", {}).get("ASTD", 7)
        resets_left_display = "∞" if unlimited else resets_left

        if reset_results[0][1]:
            embed = create_embed(
                "Key Reset",
                f"Your ASTD license key has been reset and deleted. You can now generate a new one. Resets Left: {resets_left_display}"
            )
        else:
            if error:
                embed = create_error_embed("Reset Failed", f"Failed to reset key: {error}")
            else:
                embed = create_error_embed("Reset Failed", "Failed to reset key.")
        await safe_send_response(interaction, embed=embed, ephemeral=True)

class ASTDViewKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="View Key", style=discord.ButtonStyle.secondary, custom_id="view_astd_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_astd_access(interaction):
            await interaction.response.send_message("You don't have the required role to view ASTD keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        matching_keys = [k for k in user_keys if k.key_type == "ASTD"]
        if not matching_keys:
            await interaction.response.send_message("You don't have an ASTD key.", ephemeral=True)
            return
        key = matching_keys[0]
        status = "Activated" if key.status == "activated" else ("Expired" if key.is_expired() else "Deactivated")
        days_left = key.days_until_expiry()
        hwid = key.hwid
        resets_left = "∞" if key.resets_left >= 999999 else key.resets_left
        embed = discord.Embed(
            title="\U0001F511 Your ASTD License Key",
            description=f"**License Key**\n`{key.key_id}`",
            color=0xff69b4
        )
        embed.add_field(name="\U0001F4DD Status", value=status, inline=True)
        embed.add_field(name="HWID", value=hwid, inline=True)
        embed.add_field(name="\U0001F551 Expiry", value=key.expires_at.strftime('%a %b %d %H:%M:%S %Y'), inline=True)
        embed.add_field(name="Resets Left", value=str(resets_left), inline=True)
        embed.set_footer(text="You are responsible for your own key! We will not replace it if you share it with others.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ALSPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Manage Your ALS License Key", style=discord.ButtonStyle.primary, custom_id="manage_als_key")
    async def manage_als_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await has_als_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to manage ALS keys.", ephemeral=True)
            return
        await safe_send_response(
            interaction,
            "Select an option to manage your ALS license key:",
            view=ALSOptionsView(),
            ephemeral=True
        )

class ALSOptionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Permanent
        self.add_item(ALSGenerateKeyButton())
        self.add_item(ALSResetKeyButton())
        self.add_item(ALSViewKeyButton())

class ALSGenerateKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Generate Key", style=discord.ButtonStyle.primary, custom_id="generate_als_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_als_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to generate ALS keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == "ALS" and not k.is_expired()]
        if existing_keys:
            await safe_send_response(interaction, "You already have an active ALS key.", ephemeral=True)
            return
        duration = "1y"
        days = parse_duration(duration)
        # Generate a computer-based HWID instead of using Discord ID
        import hashlib
        computer_hwid = hashlib.sha256(f"computer_{user.id}_{user.name}".encode()).hexdigest()[:16]
        license_key = await KeyManager.create_key("ALS", user.id, computer_hwid, days, name="Auto-generated")
        expires_str = "Never" if days == 0 else license_key.expires_at.strftime('%Y-%m-%d %H:%M:%S')
        try:
            dm_embed = create_embed(
                f"New ALS License Key",
                f"You have been granted a new ALS license key.\n\n"
                f"**Key ID:** `{license_key.key_id}`\n"
                f"**Duration:** {duration}\n"
                f"**Expires:** {expires_str}\n\n"
                f"Keep this key safe and do not share it with others."
            )
            await user.send(embed=dm_embed)
            await safe_send_response(interaction, "Key generated and sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await safe_send_response(interaction, "Could not DM you the key. Please check your DM settings.", ephemeral=True)

class ALSResetKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Key", style=discord.ButtonStyle.danger, custom_id="reset_als_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_als_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to reset ALS keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        matching_keys = [k for k in user_keys if k.key_type == "ALS"]
        if not matching_keys:
            await safe_send_response(interaction, "You don't have an ALS key to reset.", ephemeral=True)
            return
        unlimited = is_owner(interaction) or await has_manager_role(interaction) or await has_exclusive_role(interaction)

        # Check resets_left before attempting reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(user.id), {})
        current_resets = user_data.get("resets_left", {}).get("ALS", 7)

        if not unlimited and current_resets <= 0:
            embed = create_error_embed("No Resets Left", "You have no resets left for this key.")
            await safe_send_response(interaction, embed=embed, ephemeral=True)
            return

        reset_results = []
        for key in matching_keys:
            result = await KeyManager.reset_key(key.key_id, unlimited_resets=unlimited)
            reset_results.append((key, result))

        # Fetch updated resets_left after reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(user.id), {})
        resets_left = user_data.get("resets_left", {}).get("ALS", 7)
        resets_left_display = "∞" if unlimited else resets_left

        if reset_results[0][1]:
            embed = create_embed(
                "Key Reset",
                f"Your ALS license key has been reset and deleted. You can now generate a new one. Resets Left: {resets_left_display}"
            )
        else:
            embed = create_error_embed("No Resets Left", "You have no resets left for this key.")
        await safe_send_response(interaction, embed=embed, ephemeral=True)

class ALSViewKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="View Key", style=discord.ButtonStyle.secondary, custom_id="view_als_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_als_access(interaction):
            await interaction.response.send_message("You don't have the required role to view ALS keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        matching_keys = [k for k in user_keys if k.key_type == "ALS"]
        if not matching_keys:
            await interaction.response.send_message("You don't have an ALS key.", ephemeral=True)
            return
        key = matching_keys[0]
        status = "Activated" if key.status == "activated" else ("Expired" if key.is_expired() else "Deactivated")
        days_left = key.days_until_expiry()
        hwid = key.hwid
        resets_left = "∞" if key.resets_left >= 999999 else key.resets_left
        embed = discord.Embed(
            title="\U0001F511 Your ALS License Key",
            description=f"**License Key**\n`{key.key_id}`",
            color=0xff69b4
        )
        embed.add_field(name="\U0001F4DD Status", value=status, inline=True)
        embed.add_field(name="HWID", value=hwid, inline=True)
        embed.add_field(name="\U0001F551 Expiry", value=key.expires_at.strftime('%a %b %d %H:%M:%S %Y'), inline=True)
        embed.add_field(name="Resets Left", value=str(resets_left), inline=True)
        embed.set_footer(text="You are responsible for your own key! We will not replace it if you share it with others.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

class GAGPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Manage Your GAG License Key", style=discord.ButtonStyle.primary, custom_id="manage_gag_key")
    async def manage_gag_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await has_gag_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to manage GAG keys.", ephemeral=True)
            return
        await safe_send_response(
            interaction,
            "Select an option to manage your GAG license key:",
            view=GAGOptionsView(),
            ephemeral=True
        )

class GAGOptionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Permanent
        self.add_item(GAGGenerateKeyButton())
        self.add_item(GAGResetKeyButton())
        self.add_item(GAGViewKeyButton())

class GAGGenerateKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Generate Key", style=discord.ButtonStyle.primary, custom_id="generate_gag_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_gag_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to generate GAG keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == "GAG" and not k.is_expired()]
        if existing_keys:
            await safe_send_response(interaction, "You already have an active GAG key.", ephemeral=True)
            return
        duration = "1y"
        days = parse_duration(duration)
        # Generate a computer-based HWID instead of using Discord ID
        import hashlib
        computer_hwid = hashlib.sha256(f"computer_{user.id}_{user.name}".encode()).hexdigest()[:16]
        license_key = await KeyManager.create_key("GAG", user.id, computer_hwid, days, name="Auto-generated")
        expires_str = "Never" if days == 0 else license_key.expires_at.strftime('%Y-%m-%d %H:%M:%S')
        try:
            dm_embed = create_embed(
                f"New GAG License Key",
                f"You have been granted a new GAG license key.\n\n"
                f"**Key ID:** `{license_key.key_id}`\n"
                f"**Duration:** {duration}\n"
                f"**Expires:** {expires_str}\n\n"
                f"Keep this key safe and do not share it with others."
            )
            await user.send(embed=dm_embed)
            await safe_send_response(interaction, "Key generated and sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await safe_send_response(interaction, "Could not DM you the key. Please check your DM settings.", ephemeral=True)

class GAGResetKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reset Key", style=discord.ButtonStyle.danger, custom_id="reset_gag_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_gag_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to reset GAG keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        matching_keys = [k for k in user_keys if k.key_type == "GAG"]
        if not matching_keys:
            await safe_send_response(interaction, "You don't have a GAG key to reset.", ephemeral=True)
            return
        unlimited = is_owner(interaction) or await has_manager_role(interaction) or await has_exclusive_role(interaction)

        # Check resets_left before attempting reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(user.id), {})
        current_resets = user_data.get("resets_left", {}).get("GAG", 7)

        if not unlimited and current_resets <= 0:
            embed = create_error_embed("No Resets Left", "You have no resets left for this key.")
            await safe_send_response(interaction, embed=embed, ephemeral=True)
            return

        reset_results = []
        for key in matching_keys:
            result = await KeyManager.reset_key(key.key_id, unlimited_resets=unlimited)
            reset_results.append((key, result))

        # Fetch updated resets_left after reset
        users_data = await storage.get("users", {})
        user_data = users_data.get(str(user.id), {})
        resets_left = user_data.get("resets_left", {}).get("GAG", 7)
        resets_left_display = "∞" if unlimited else resets_left

        if reset_results[0][1]:
            embed = create_embed(
                "Key Reset",
                f"Your GAG license key has been reset and deleted. You can now generate a new one. Resets Left: {resets_left_display}"
            )
        else:
            embed = create_error_embed("No Resets Left", "You have no resets left for this key.")
        await safe_send_response(interaction, embed=embed, ephemeral=True)

class GAGViewKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="View Key", style=discord.ButtonStyle.secondary, custom_id="view_gag_key")
    async def callback(self, interaction: discord.Interaction):
        if not await has_gag_access(interaction):
            await interaction.response.send_message("You don't have the required role to view GAG keys.", ephemeral=True)
            return
        user = interaction.user
        user_keys = await KeyManager.get_user_keys(user.id)
        matching_keys = [k for k in user_keys if k.key_type == "GAG"]
        if not matching_keys:
            await interaction.response.send_message("You don't have a GAG key.", ephemeral=True)
            return
        key = matching_keys[0]
        status = "Activated" if key.status == "activated" else ("Expired" if key.is_expired() else "Deactivated")
        days_left = key.days_until_expiry()
        hwid = key.hwid
        resets_left = "∞" if key.resets_left >= 999999 else key.resets_left
        embed = discord.Embed(
            title="\U0001F511 Your GAG License Key",
            description=f"**License Key**\n`{key.key_id}`",
            color=0xff69b4
        )
        embed.add_field(name="\U0001F4DD Status", value=status, inline=True)
        embed.add_field(name="HWID", value=hwid, inline=True)
        embed.add_field(name="\U0001F551 Expiry", value=key.expires_at.strftime('%a %b %d %H:%M:%S %Y'), inline=True)
        embed.add_field(name="Resets Left", value=str(resets_left), inline=True)
        embed.set_footer(text="You are responsible for your own key! We will not replace it if you share it with others.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# Start a simple web server for port support (useful for web services like Replit)
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

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
        else:
            # If key is not activated and HWID matches, activate it
            if hwid and key_info.get("hwid", "") == hwid:
                key_info["status"] = "activated"
                keys_data[key] = key_info
                storage.data["keys"] = keys_data
                
                # Update user data as well
                users_data = storage.data.get("users", {})
                user_id = str(key_info["user_id"])
                if user_id in users_data and key in users_data[user_id]["keys"]:
                    users_data[user_id]["keys"][key]["status"] = "activated"
                    storage.data["users"] = users_data
                storage.save_sync(storage.data)

        try:
            if expires_at.year >= 9999:
                days_left = '∞'
            else:
                delta = expires_at - datetime.now()
                days_left = delta.days
        except Exception:
            days_left = None

        # Get current status (might have been updated above)
        current_status = key_info.get("status", "deactivated")
        
        resp = {
            "valid": True,
            "key_id": key_info.get("key_id", key),
            "key_type": key_info.get("key_type", ""),
            "user_id": key_info.get("user_id", ""),
            "hwid": key_info.get("hwid", ""),
            "status": current_status,
            "expires_at": key_info.get("expires_at", ""),
            "created_at": key_info.get("created_at", ""),
            "name": key_info.get("name", ""),
            "days_left": days_left
        }
        return jsonify(resp)
    return jsonify({"valid": False, "message": "Key not found"})

@app.route("/check_activation", methods=["POST"])
def check_activation():
    """Check if a computer (HWID) already has an activated key"""
    data = request.get_json()
    hwid = data.get("hwid", "")

    if not hwid:
        return jsonify({"activated": False, "message": "HWID required"})

    keys_data = storage.data.get("keys", {})

    # Check if any key with this HWID is activated
    for key_id, key_info in keys_data.items():
        if key_info.get("hwid", "") == hwid and key_info.get("status", "deactivated") == "activated":
            # Check if key is not expired
            try:
                expires_at = datetime.fromisoformat(key_info["expires_at"])
                if expires_at.year >= 9999 or expires_at > datetime.now():
                    return jsonify({
                        "activated": True, 
                        "key_id": key_info.get("key_id", key_id),
                        "key_type": key_info.get("key_type", ""),
                        "expires_at": key_info.get("expires_at", "")
                    })
            except Exception:
                continue

    return jsonify({"activated": False})

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

@app.route("/check_session", methods=["POST"])
def check_session():
    """Check if a specific session still owns the key"""
    data = request.get_json()
    key = data.get("key", "")
    hwid = data.get("hwid", "")

    if not key or not hwid:
        return jsonify({"valid": False, "message": "Key and HWID required"})

    keys_data = storage.data.get("keys", {})
    if key in keys_data:
        key_info = keys_data[key]
        # Check if this session still owns the key
        current_hwid = key_info.get("hwid", "")
        if current_hwid == hwid and key_info.get("status", "deactivated") == "activated":
            return jsonify({"valid": True, "message": "Session is valid"})
        else:
            return jsonify({"valid": False, "message": "Session no longer valid"})

    return jsonify({"valid": False, "message": "Key not found"})

@app.route("/settings", methods=["GET"])
def get_settings():
    """Get key system settings"""
    settings = storage.data.get("settings", {})
    return jsonify(settings)

@app.route("/stats", methods=["GET"])
def get_stats():
    """Get key system statistics"""
    keys_data = storage.data.get("keys", {})
    users_data = storage.data.get("users", {})

    total_keys = len(keys_data)
    active_keys = 0
    expired_keys = 0

    for key_info in keys_data.values():
        try:
            expires_at = datetime.fromisoformat(key_info["expires_at"])
            if expires_at.year < 9999 and expires_at <= datetime.now():
                expired_keys += 1
            else:
                active_keys += 1
        except:
            expired_keys += 1

    return jsonify({
        "total_keys": total_keys,
        "active_keys": active_keys,
        "expired_keys": expired_keys,
        "total_users": len(users_data),
        "key_types": {
            "GAG": len([k for k in keys_data.values() if k.get("key_type") == "GAG"]),
            "ASTD": len([k for k in keys_data.values() if k.get("key_type") == "ASTD"]),
            "ALS": len([k for k in keys_data.values() if k.get("key_type") == "ALS"])
        }
    })

def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Start the web server in a background thread
web_thread = threading.Thread(target=run_web)
web_thread.daemon = True
web_thread.start()

bot = LicenseBot()

# Slash Commands

@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    try:
        is_admin = await has_key_role(interaction)
        owner = is_owner(interaction)
        try:
            admin = await has_exclusive_role(interaction)  # Now ADMIN
        except Exception:
            admin = False
        try:
            manager = await has_manager_role(interaction)
        except Exception:
            manager = False

        access_level = "OWNER" if owner else ("ADMIN" if admin else ("MANAGER" if manager else ("User" if is_admin else "User")))

        embed = discord.Embed(
            title="\U0001F511 License Bot Commands",
            description="Here are all the available commands:",
            color=0xff69b4
        )

        # User Commands - Always shown
        embed.add_field(
            name="\U0001F464 User Commands",
            value=(
                "`/manage_key` - View or reset your GAG/ASTD/ALS license key\n"
                "`/key_stats` - View your personal key statistics\n"
                "`/help` - Show this help message"
            ),
            inline=False
        )

        # Manager Commands - Show for Manager, Admin, and Owner
        if manager or admin or owner:
            embed.add_field(
                name="\U0001F527 Manager Commands",
                value=(
                    "`/create_key` - Create a new GAG/ASTD/ALS license key\n"
                    "`/check_license` - Check license status by HWID or user\n"
                    "`/delete_key` - Delete a license key\n"
                    "`/list_keys` - List all license keys (GAG, ASTD, ALS) with pagination\n"
                    "`/register_user` - Register a user with HWID\n"
                    "`/check_hwid` - Check HWID status\n"
                    "`/health` - Check system health\n"
                    "`/setup_key_message` - Create ASTD/ALS/GAG key management panel\n"
                    "`/system_stats` - View detailed system statistics"
                ),
                inline=False
            )

        # Admin Commands - Show for Admin and Owner
        if admin or owner:
            embed.add_field(
                name="\U0001F6E1\ufe0f Admin Commands",
                value=(
                    "`/delete_all_key` - Delete all keys for a user \u26a0\ufe0f\n"
                    "`/activate_key` - Activate a license key\n"
                    "`/user_lookup` - Look up license info for a user\n"
                    "`/bulk_operations` - Perform bulk operations on keys\n"
                    "`/system_config` - Configure system settings"
                ),
                inline=False
            )

        # Owner Commands - Show only for Owner
        if owner:
            embed.add_field(
                name="\U0001F451 Owner Commands",
                value=(
                    "`/managerrole` - Set the manager role\n"
                    "`/exclus` - Set the exclusive/admin role\n"
                    "`/debug` - Debug the key system\n"
                    "`/backup_system` - Create system backup\n"
                    "`/restore_system` - Restore system from backup\n"
                    "`/deletekeysystem` - \u26a0\ufe0f Wipe all key system data (danger)"
                ),
                inline=False
            )

        embed.add_field(
            name="\u2139\ufe0f Information",
            value=(
                f"**Your Access Level:** {access_level}\n"
                f"**Key Types:** GAG, ASTD, ALS\n"
            ),
            inline=False
        )
        embed.timestamp = datetime.now()
        embed.set_footer(text="Use commands with /")
        await safe_send_response(interaction, embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        embed = create_error_embed("Error", "An error occurred while showing help.")
        await safe_send_response(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="manage_key", description="View or reset your GAG/ASTD/ALS license key")
@app_commands.describe(key_type="Type of key (GAG, ASTD, or ALS)", action="Action to perform")
@app_commands.choices(key_type=[
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ASTD", value="ASTD"),
    app_commands.Choice(name="ALS", value="ALS")
])
@app_commands.choices(action=[
    app_commands.Choice(name="View", value="view"),
    app_commands.Choice(name="Reset", value="reset")
])
async def manage_key(interaction: discord.Interaction, key_type: str, action: str):
    try:
        exclusive = await has_exclusive_role(interaction)
        manager = await has_manager_role(interaction)
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
            resets_left = "∞" if key.resets_left >= 999999 else key.resets_left
            embed = create_embed(
                f"{key_type} License Key",
                f"**Key ID:** `{key.key_id}`\n"
                f"**Status:** {status}\n"
                f"**Days Left:** {days_left}\n"
                f"**HWID:** `{key.hwid}`\n"
                f"**Created:** {key.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"**Expires:** {key.expires_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"**Activation Status:** {key.status.title()}\n"
                f"**Resets Left:** {resets_left}"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        elif action == "reset":
            if not (await has_key_role(interaction) or exclusive or manager):
                embed = create_error_embed("Permission Denied", "You don't have permission to reset keys.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            unlimited = is_owner(interaction) or manager or exclusive
            reset_results = []
            for key in matching_keys:
                result = await KeyManager.reset_key(key.key_id, unlimited_resets=unlimited)
                reset_results.append((key, result))

            # Fetch updated resets_left after reset
            users_data = await storage.get("users", {})
            user_data = users_data.get(str(interaction.user.id), {})
            resets_left = user_data.get("resets_left", {}).get(key_type, 7)
            resets_left_display = "∞" if unlimited else resets_left

            if reset_results[0][1]:
                embed = create_embed(
                    "Key Reset",
                    f"Your {key_type} license key has been reset. Resets Left: {resets_left_display}"
                )
            else:
                embed = create_error_embed("No Resets Left", f"You have no resets left for this key.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in manage_key: {e}")
        embed = create_error_embed("Error", "An error occurred while managing your key.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="create_key", description="Create a new GAG/ALS/ASTD license key")
@app_commands.describe(
    key_type="Type of key (GAG, ALS, or ASTD)",
    duration="Duration (e.g. 1y, 1m, 1d, 1h, permanent)",
    name="Name for the key",
    user="User to create key for",
    hwid="Hardware ID (optional - will auto-generate if not provided)"
)
@app_commands.choices(key_type=[
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ALS", value="ALS"),
    app_commands.Choice(name="ASTD", value="ASTD")
])
async def create_key(interaction: discord.Interaction, key_type: str, duration: str, 
                    name: str, user: discord.User, hwid: Optional[str] = None):
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

        # Auto-generate HWID if not provided
        if not hwid:
            import hashlib
            hwid = hashlib.sha256(f"computer_{user.id}_{user.name}".encode()).hexdigest()[:16]

        user_keys = await KeyManager.get_user_keys(user.id)
        existing_keys = [k for k in user_keys if k.key_type == key_type and not k.is_expired()]
        if existing_keys:
            embed = create_error_embed(
                "Key Already Exists", 
                f"User {user.mention} already has an active {key_type} key."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

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
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ALS", value="ALS"),
    app_commands.Choice(name="ASTD", value="ASTD"),
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

        page = 0
        page_size = 10
        max_page = (len(keys) - 1) // page_size

        def get_page_lines(page):
            start = page * page_size
            end = start + page_size
            lines = []
            for key in keys[start:end]:
                status = "Expired" if key.is_expired() else "Active"
                days_left = key.days_until_expiry()
                lines.append(
                    f"**{key.key_type}:** `{key.key_id}` - {status} ({days_left} days) - <@{key.user_id}>"
                )
            if end < len(keys):
                lines.append(f"\n...and {len(keys) - end} more keys")
            return lines

        embed = create_embed(
            f"{key_type} License Keys ({len(keys)} total)",
            "\n".join(get_page_lines(page))
        )
        msg = await interaction.response.send_message(embed=embed, ephemeral=True)

        # Only add pagination if more than one page
        if max_page > 0:
            # Add cursor emoji for next page
            message = await interaction.original_response()
            await message.add_reaction("🖱️")  # Cursor emoji

            def check(reaction, user):
                return (
                    user.id == interaction.user.id and
                    str(reaction.emoji) == "🖱️" and
                    reaction.message.id == message.id
                )
            try:
                while True:
                    reaction, user = await interaction.client.wait_for("reaction_add", timeout=60.0, check=check)
                    if page < max_page:
                        page += 1
                        embed = create_embed(
                            f"{key_type} License Keys ({len(keys)} total)",
                            "\n".join(get_page_lines(page))
                        )
                        await message.edit(embed=embed)
                    # Remove user's reaction to allow repeated paging
                    await message.remove_reaction("🖱️", user)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        logger.error(f"Error in list_keys: {e}")
        embed = create_error_embed("Error", "An error occurred while listing the keys.")
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

@bot.tree.command(name="managerrole", description="Set the role that can manage keys")
@app_commands.describe(role="Role to set for key management (mention, name, or ID)")
async def managerrole(interaction: discord.Interaction, role: str):
    try:
        if not is_owner(interaction):
            embed = create_error_embed("Permission Denied", "Only the bot owner can set the manager role.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        resolved_role = await resolve_role(interaction.guild, role)
        if not resolved_role:
            embed = create_error_embed("Invalid Role", f"Could not find role for input: {role}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        MANAGER_ROLE_IDS.clear()
        MANAGER_ROLE_IDS.append(resolved_role.id)
        await storage.set("manager_role", resolved_role.name)
        embed = create_embed(
            "Manager Role Updated",
            f"Manager role set to {resolved_role.mention}\nUsers with this role can now create and manage license keys."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in managerrole: {e}")
        embed = create_error_embed("Error", "An error occurred while setting the manager role.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="exclus", description="Set the exclusive role")
@app_commands.describe(role="Role to set as exclusive (mention, name, or ID)")
async def exclus(interaction: discord.Interaction, role: str):
    try:
        if not is_owner(interaction):
            embed = create_error_embed("Permission Denied", "Only the bot owner can set the exclusive role.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        resolved_role = await resolve_role(interaction.guild, role)
        if not resolved_role:
            embed = create_error_embed("Invalid Role", f"Could not find role for input: {role}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        EXCLUSIVE_ROLE_IDS.clear()
        EXCLUSIVE_ROLE_IDS.append(resolved_role.id)
        await storage.set("exclusive_role", resolved_role.name)
        embed = create_embed(
            "Exclusive Role Updated",
            f"Exclusive role set to {resolved_role.mention}\nUsers with this role are now exclusive."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in exclus: {e}")
        embed = create_error_embed("Error", "An error occurred while setting the exclusive role.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_key_message", description="Create ASTD/ALS/GAG key management panel")
@app_commands.describe(
    astd_channel="Channel to post ASTD key management panel (mention, name, or ID)",
    als_channel="Channel to post ALS key management panel (mention, name, or ID)",
    gag_channel="Channel to post GAG key management panel (mention, name, or ID)"
)
async def setup_key_message(
    interaction: discord.Interaction,
    astd_channel: Optional[str] = None,
    als_channel: Optional[str] = None,
    gag_channel: Optional[str] = None
):
    try:
        if not await has_astd_access(interaction):
            await safe_send_response(interaction, "You don't have the required role to setup key panels.", ephemeral=True)
            return

        # Respond immediately to avoid timeout
        await safe_send_response(interaction, "Setting up key panels...", ephemeral=True)

        sent = []
        # ASTD panel
        if astd_channel:
            resolved_astd = await resolve_channel(interaction.guild, astd_channel)
            if resolved_astd:
                astd_embed = create_embed(
                    "🔑 ASTD License Key Management",
                    "Manage your license key for ASTD\n\n"
                    "**Available Options**\n"
                    "• Generate a new license key\n"
                    "• Reset your existing key (Only once)\n"
                    "• View your current key details\n\n"
                    "**Requirements**\n"
                    "You must have the ASTD Premium role to use these features.\n\n"
                    "Click the button below to manage your ASTD license key"
                )
                await resolved_astd.send(embed=astd_embed, view=ASTDPanelView())
                sent.append(f"ASTD panel sent to {resolved_astd.mention}")
        # ALS panel
        if als_channel:
            resolved_als = await resolve_channel(interaction.guild, als_channel)
            if resolved_als:
                als_embed = create_embed(
                    "🔑 ALS License Key Management",
                    "Manage your license key for ALS\n\n"
                    "**Available Options**\n"
                    "• Generate a new license key\n"
                    "• Reset your existing key (Only once)\n"
                    "• View your current key details\n\n"
                    "**Requirements**\n"
                    "You must have the ALS Premium role to use these features.\n\n"
                    "Click the button below to manage your ALS license key"
                )
                await resolved_als.send(embed=als_embed, view=ALSPanelView())
                sent.append(f"ALS panel sent to {resolved_als.mention}")
        # GAG panel
        if gag_channel:
            resolved_gag = await resolve_channel(interaction.guild, gag_channel)
            if resolved_gag:
                gag_embed = create_embed(
                    "🔑 GAG License Key Management",
                    "Manage your license key for GAG\n\n"
                    "**Available Options**\n"
                    "• Generate a new license key\n"
                    "• Reset your existing key (Only once)\n"
                    "• View your current key details\n\n"
                    "**Requirements**\n"
                    "You must have the GAG Premium role to use these features.\n\n"
                    "Click the button below to manage your GAG license key"
                )
                await resolved_gag.send(embed=gag_embed, view=GAGPanelView())
                sent.append(f"GAG panel sent to {resolved_gag.mention}")

        # Send followup response
        if sent:
            await interaction.followup.send("✅ " + "\n".join(sent), ephemeral=True)
        else:
            await interaction.followup.send("❌ No panels sent. Please specify a channel.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in setup_key_message: {e}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)
        except:
            pass

@bot.tree.command(name="delete_all_key", description="Delete all keys for a user")
@app_commands.describe(user="User to delete all keys for")
async def delete_all_key(interaction: discord.Interaction, user: discord.User):
    try:
        # Allow owners, managers, and exclusive users to delete all keys
        if not (is_owner(interaction) or await has_manager_role(interaction) or await has_exclusive_role(interaction)):
            embed = create_error_embed("Permission Denied", "You don't have permission to delete all keys.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        user_keys = await KeyManager.get_user_keys(user.id)
        deleted_count = 0

        for key in user_keys:
            if await KeyManager.delete_key(key.key_id):
                deleted_count += 1

        # Also clear the user's resets_left data
        users_data = await storage.get("users", {})
        user_key = str(user.id)
        if user_key in users_data:
            users_data[user_key]["resets_left"] = {}
            await storage.set("users", users_data)

        embed = create_embed(
            "All Keys Deleted",
            f"Deleted {deleted_count} keys for {user.mention}. Reset counts have been cleared.",
            color=0xFFA500
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Error in delete_all_key: {e}")
        embed = create_error_embed("Error", "An error occurred while deleting all keys.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="debug", description="Debug the key system (Owner only)")
async def debug(interaction: discord.Interaction):
    if not is_owner(interaction):
        embed = create_error_embed("Permission Denied", "Only the bot owner can use debug.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    keys_data = await storage.get("keys", {})
    users_data = await storage.get("users", {})
    embed = create_embed(
        "Key System Debug",
        f"**Total Keys:** {len(keys_data)}\n"
        f"**Total Users:** {len(users_data)}\n"
        f"**Manager Roles:** {MANAGER_ROLE_IDS}\n"
        f"**Exclusive Roles:** {EXCLUSIVE_ROLE_IDS}"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="activate_key", description="Activate a license key (Owner/Manager/Exclusive only)")
@app_commands.describe(license_key="License key to activate")
async def activate_key(interaction: discord.Interaction, license_key: str):
    if not (is_owner(interaction) or await has_manager_role(interaction) or await has_exclusive_role(interaction)):
        embed = create_error_embed("Permission Denied", "You don't have permission to activate keys.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    success = await KeyManager.activate_key(license_key)
    if success:
        embed = create_embed("Key Activated", f"Key `{license_key}` has been activated.")
    else:
        embed = create_error_embed("Key Not Found", f"Key `{license_key}` not found.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="user_lookup", description="Look up license info for a user")
@app_commands.describe(user="User to look up license information for")
async def user_lookup(interaction: discord.Interaction, user: discord.User):
    try:
        if not (await has_exclusive_role(interaction) or is_owner(interaction)):
            embed = create_error_embed("Permission Denied", "You don't have permission to lookup user information.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        users_data = await storage.get("users", {})
        user_key = str(user.id)

        if user_key not in users_data:
            embed = create_error_embed("User Not Found", f"No data found for {user.mention}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        user_info = users_data[user_key]
        user_keys = await KeyManager.get_user_keys(user.id)

        active_keys = [k for k in user_keys if not k.is_expired()]
        expired_keys = [k for k in user_keys if k.is_expired()]

        key_summary = []
        for key in user_keys:
            status = "Expired" if key.is_expired() else "Active"
            key_summary.append(f"**{key.key_type}:** `{key.key_id}` - {status}")

        resets_info = user_info.get("resets_left", {})
        resets_summary = []
        for key_type, resets in resets_info.items():
            resets_display = "∞" if resets >= 999999 else str(resets)
            resets_summary.append(f"{key_type}: {resets_display}")

        embed = create_embed(
            f"User Lookup: {user.display_name}",
            f"**Discord ID:** {user.id}\n"
            f"**Total Keys:** {len(user_keys)}\n"
            f"**Active Keys:** {len(active_keys)}\n"
            f"**Expired Keys:** {len(expired_keys)}\n"
            f"**HWIDs:** {len(user_info.get('hwids', []))}\n"
            f"**Order:** {user_info.get('order', 'N/A')}\n"
            f"**Registered:** {user_info.get('registered_at', 'N/A')}\n\n"
            f"**Resets Left:**\n{chr(10).join(resets_summary) if resets_summary else 'None'}\n\n"
            f"**Keys:**\n{chr(10).join(key_summary) if key_summary else 'No keys'}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Error in user_lookup: {e}")
        embed = create_error_embed("Error", "An error occurred while looking up user information.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="deletekeysystem", description="⚠️ Wipe all key system data (danger)")
async def deletekeysystem(interaction: discord.Interaction):
    try:
        if not is_owner(interaction):
            embed = create_error_embed("Permission Denied", "Only the bot owner can wipe the key system database.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        # Wipe all key system data
        storage.data["keys"] = {}
        storage.data["users"] = {}
        storage.save_sync(storage.data)
        embed = create_embed(
            "⚠️ Key System Deleted",
            "All license keys and user data have been deleted from the database. This action cannot be undone!"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in deletekeysystem: {e}")
        embed = create_error_embed("Error", "An error occurred while deleting the key system.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# New enhanced commands for better system management

@bot.tree.command(name="key_stats", description="View your personal key statistics")
async def key_stats(interaction: discord.Interaction):
    try:
        user_keys = await KeyManager.get_user_keys(interaction.user.id)
        if not user_keys:
            embed = create_error_embed("No Keys", "You don't have any license keys.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        active_keys = [k for k in user_keys if not k.is_expired()]
        expired_keys = [k for k in user_keys if k.is_expired()]

        key_types = {}
        for key in user_keys:
            key_types[key.key_type] = key_types.get(key.key_type, 0) + 1

        users_data = await storage.get("users", {})
        user_data = users_data.get(str(interaction.user.id), {})
        resets_info = user_data.get("resets_left", {})

        embed = create_embed(
            f"Your Key Statistics",
            f"**Total Keys:** {len(user_keys)}\n"
            f"**Active Keys:** {len(active_keys)}\n"
            f"**Expired Keys:** {len(expired_keys)}\n\n"
            f"**Key Types:**\n" + 
            "\n".join([f"• {k_type}: {count}" for k_type, count in key_types.items()]) +
            f"\n\n**Resets Left:**\n" +
            "\n".join([f"• {k_type}: {'∞' if resets >= 999999 else resets}" for k_type, resets in resets_info.items()])
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in key_stats: {e}")
        embed = create_error_embed("Error", "An error occurred while fetching your statistics.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="system_stats", description="View detailed system statistics (Manager+)")
async def system_stats(interaction: discord.Interaction):
    try:
        if not await has_key_role(interaction):
            embed = create_error_embed("Permission Denied", "You don't have permission to view system statistics.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        keys_data = await storage.get("keys", {})
        users_data = await storage.get("users", {})
        settings = await storage.get("settings", {})

        total_keys = len(keys_data)
        total_users = len(users_data)

        key_type_stats = {"GAG": 0, "ASTD": 0, "ALS": 0}
        active_by_type = {"GAG": 0, "ASTD": 0, "ALS": 0}

        for key_info in keys_data.values():
            key = LicenseKey.from_dict(key_info)
            key_type_stats[key.key_type] = key_type_stats.get(key.key_type, 0) + 1
            if not key.is_expired():
                active_by_type[key.key_type] = active_by_type.get(key.key_type, 0) + 1

        embed = create_embed(
            "System Statistics",
            f"**📊 Overall Stats**\n"
            f"• Total Keys: {total_keys}\n"
            f"• Total Users: {total_users}\n"
            f"• Active Keys: {sum(active_by_type.values())}\n"
            f"• Expired Keys: {total_keys - sum(active_by_type.values())}\n\n"
            f"**🔑 By Key Type**\n" +
            "\n".join([f"• {k_type}: {total} total, {active_by_type[k_type]} active" 
                      for k_type, total in key_type_stats.items()]) +
            f"\n\n**⚙️ System Settings**\n"
            f"• Max Keys per User: {settings.get('max_keys_per_user', 3)}\n"
            f"• Default Duration: {settings.get('default_key_duration', '1y')}\n"
            f"• Maintenance Mode: {'🔴 ON' if settings.get('maintenance_mode') else '🟢 OFF'}\n"
            f"• Auto Cleanup: {'✅' if settings.get('auto_expire_cleanup') else '❌'}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in system_stats: {e}")
        embed = create_error_embed("Error", "An error occurred while fetching system statistics.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="backup_system", description="Create system backup (Owner only)")
async def backup_system(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("Only the owner can use this command.", ephemeral=True)
        return
    try:
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(backup_dir, f"data_backup_{timestamp}.json")
        shutil.copy2(storage.filename, backup_file)
        await interaction.response.send_message(f"Backup created: `{backup_file}`", ephemeral=True)
        # Optionally send the file
        await interaction.followup.send(file=File(backup_file), ephemeral=True)
    except Exception as e:
        logger.error(f"Error in backup_system: {e}")
        await interaction.response.send_message(f"Error creating backup: {e}", ephemeral=True)

@bot.tree.command(name="restore_system", description="Restore system from backup (Owner only)")
@app_commands.describe(backup_filename="Backup file name to restore (from backups/ directory)")
async def restore_system(interaction: discord.Interaction, backup_filename: str):
    if not is_owner(interaction):
        await interaction.response.send_message("Only the owner can use this command.", ephemeral=True)
        return
    try:
        backup_dir = "backups"
        backup_file = os.path.join(backup_dir, backup_filename)
        if not os.path.exists(backup_file):
            await interaction.response.send_message(f"Backup file `{backup_filename}` not found.", ephemeral=True)
            return
        shutil.copy2(backup_file, storage.filename)
        storage.data = storage.load_data()
        await interaction.response.send_message(f"System restored from `{backup_filename}`.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in restore_system: {e}")
        await interaction.response.send_message(f"Error restoring backup: {e}", ephemeral=True)

@bot.tree.command(name="system_config", description="Configure system settings (Owner/Admin only)")
@app_commands.describe(setting="Setting to update (e.g. max_keys_per_user)", value="New value for the setting")
async def system_config(interaction: discord.Interaction, setting: str = None, value: str = None):
    # Only owner or admin (exclusive role)
    if not (is_owner(interaction) or await has_exclusive_role(interaction)):
        await interaction.response.send_message("Only the owner or admin can use this command.", ephemeral=True)
        return
    try:
        settings = await storage.get("settings", {})
        if setting and value is not None:
            # Try to cast value to int/bool if possible
            if value.lower() in ("true", "false"):
                value_cast = value.lower() == "true"
            else:
                try:
                    value_cast = int(value)
                except ValueError:
                    value_cast = value
            settings[setting] = value_cast
            await storage.set("settings", settings)
            await interaction.response.send_message(f"Setting `{setting}` updated to `{value_cast}`.", ephemeral=True)
        else:
            # Show all settings
            lines = [f"`{k}`: `{v}`" for k, v in settings.items()]
            embed = create_embed("System Settings", "\n".join(lines))
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error in system_config: {e}")
        await interaction.response.send_message(f"Error updating settings: {e}", ephemeral=True)

@bot.tree.command(name="bulk_operations", description="Perform bulk operations on keys (Admin/Owner only)")
@app_commands.describe(
    action="Action to perform (delete, activate, deactivate)",
    key_type="Type of key (GAG, ALS, ASTD, ALL)",
    user="User to filter by (optional)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Delete", value="delete"),
    app_commands.Choice(name="Activate", value="activate"),
    app_commands.Choice(name="Deactivate", value="deactivate")
])
@app_commands.choices(key_type=[
    app_commands.Choice(name="GAG", value="GAG"),
    app_commands.Choice(name="ALS", value="ALS"),
    app_commands.Choice(name="ASTD", value="ASTD"),
    app_commands.Choice(name="ALL", value="ALL")
])
async def bulk_operations(interaction: discord.Interaction, action: str, key_type: str, user: discord.User = None):
    if not (is_owner(interaction) or await has_exclusive_role(interaction)):
        await interaction.response.send_message("Only the owner or admin can use this command.", ephemeral=True)
        return
    try:
        keys_data = await storage.get("keys", {})
        affected_keys = []
        for key_id, key_info in keys_data.items():
            if (key_type == "ALL" or key_info["key_type"] == key_type) and (not user or key_info["user_id"] == user.id):
                affected_keys.append(key_id)
        count = 0
        if action == "delete":
            for key_id in affected_keys:
                if await KeyManager.delete_key(key_id):
                    count += 1
            await interaction.response.send_message(f"Deleted {count} keys.", ephemeral=True)
        elif action == "activate":
            for key_id in affected_keys:
                if await KeyManager.activate_key(key_id):
                    count += 1
            await interaction.response.send_message(f"Activated {count} keys.", ephemeral=True)
        elif action == "deactivate":
            for key_id in affected_keys:
                keys_data[key_id]["status"] = "deactivated"
                count += 1
            await storage.set("keys", keys_data)
            await interaction.response.send_message(f"Deactivated {count} keys.", ephemeral=True)
        else:
            await interaction.response.send_message("Invalid action.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in bulk_operations: {e}")
        await interaction.response.send_message(f"Error in bulk operations: {e}", ephemeral=True)

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
