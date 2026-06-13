"""Configuration management for KickDropsMiner"""
import json
import os
import threading
import uuid
from urllib.parse import urlparse, urlunparse
from utils.helpers import CONFIG_FILE


def normalize_stream_url(url):
    """Return a stable URL used for queue duplicate checks."""
    value = (url or "").strip()
    if not value:
        return ""
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    scheme = (parsed.scheme or "https").lower()
    host = parsed.netloc.lower()
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else ""
    if host in ("kick.com", "www.kick.com"):
        host = "kick.com"
        path = path.lower()
    return urlunparse((scheme, host, path, "", "", ""))


class Config:
    """Manages application configuration and queue items"""
    
    def __init__(self, data_dir=None):
        self._lock = threading.RLock()
        self.data_dir = os.path.abspath(data_dir) if data_dir else None
        self.config_file = (
            os.path.join(self.data_dir, "config.json")
            if self.data_dir
            else CONFIG_FILE
        )
        self.items = []
        self.chromedriver_path = None
        self.extension_path = None
        self.mute = True
        self.hide_player = False
        self.mini_player = False
        self.force_160p = False
        self.dark_mode = True  # Dark by default
        self.language = "tr"  # default language code
        self.auto_start = False  # Auto-start queue on launch
        self.debug = False  # Debug messages disabled by default
        self.load()

    def load(self):
        """Load configuration from file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("Configuration root must be an object")
            except (OSError, ValueError, json.JSONDecodeError):
                self.items = []
                return
            loaded_items = data.get("items", [])
            self.items = []
            seen_items = set()
            for item in loaded_items:
                if not isinstance(item, dict):
                    continue
                normalized_url = normalize_stream_url(item.get("url"))
                campaign_id = item.get("campaign_id")
                item_key = (normalized_url, str(campaign_id) if campaign_id else None)
                if not normalized_url or item_key in seen_items:
                    continue
                item["url"] = normalized_url
                seen_items.add(item_key)
                self.items.append(item)
            # Migrate old items format to new format with campaign info
            migrated = False
            for item in self.items:
                if not item.get("id"):
                    item["id"] = uuid.uuid4().hex[:12]
                    migrated = True
                if "campaign_id" not in item:
                    item["campaign_id"] = None
                if "campaign_channels" not in item:
                    item["campaign_channels"] = []
                if "required_category_id" not in item:
                    item["required_category_id"] = None
                if "is_global_drop" not in item:
                    item["is_global_drop"] = False
                if "cumulative_time" not in item:
                    item["cumulative_time"] = 0
                # Add tried_channels tracking to prevent switching loops
                if "tried_channels" not in item:
                    item["tried_channels"] = []
                item.setdefault("channel_statuses", {})
                item.setdefault("campaign_name", None)
                item.setdefault("game", None)
                item.setdefault("reward_image", None)
                item.setdefault("reward_name", None)
                item.setdefault("drop_progress", None)
                item.setdefault("drop_verified", False)
            self.chromedriver_path = data.get("chromedriver_path")
            self.extension_path = data.get("extension_path")
            self.mute = data.get("mute", True)
            self.hide_player = data.get("hide_player", False)
            self.mini_player = data.get("mini_player", False)
            self.force_160p = data.get("force_160p", False)
            self.dark_mode = data.get("dark_mode", True)
            self.language = data.get("language", "tr")
            self.auto_start = data.get("auto_start", False)
            self.debug = data.get("debug", False)
            if migrated or len(self.items) != len(loaded_items):
                self.save()
        else:
            self.items = []

    def save(self):
        """Save configuration to file"""
        with self._lock:
            data = {
                "items": self.items,
                "chromedriver_path": self.chromedriver_path,
                "extension_path": self.extension_path,
                "mute": self.mute,
                "hide_player": self.hide_player,
                "mini_player": self.mini_player,
                "force_160p": self.force_160p,
                "dark_mode": self.dark_mode,
                "language": self.language,
                "auto_start": self.auto_start,
                "debug": self.debug,
            }
            temp_path = f"{self.config_file}.tmp.{os.getpid()}.{threading.get_ident()}"
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.config_file)
            finally:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass

    def add(
        self,
        url,
        minutes,
        campaign_id=None,
        campaign_channels=None,
        required_category_id=None,
        is_global_drop=False,
        campaign_name=None,
        game=None,
        reward_image=None,
        reward_name=None,
    ):
        """Add item with optional campaign grouping"""
        normalized_url = normalize_stream_url(url)
        if not normalized_url or self.contains(normalized_url, campaign_id=campaign_id):
            return False
        item = {
            "id": uuid.uuid4().hex[:12],
            "url": normalized_url,
            "minutes": minutes,
            "campaign_id": campaign_id,
            "campaign_channels": campaign_channels or [],
            "required_category_id": required_category_id,
            "is_global_drop": is_global_drop,
            "cumulative_time": 0,  # Track cumulative time across all streamers in campaign
            "tried_channels": [],
            "channel_statuses": {},
            "campaign_name": campaign_name,
            "game": game,
            "reward_image": reward_image,
            "reward_name": reward_name,
            "drop_progress": None,
            "drop_verified": False,
        }
        self.items.append(item)
        self.save()
        return True

    def contains(self, url, campaign_id=None):
        """Check duplicates by URL, scoped to a campaign when one is supplied."""
        normalized_url = normalize_stream_url(url)
        normalized_campaign_id = str(campaign_id) if campaign_id else None
        for item in self.items:
            if normalize_stream_url(item.get("url")) != normalized_url:
                continue
            item_campaign_id = item.get("campaign_id")
            item_campaign_id = str(item_campaign_id) if item_campaign_id else None
            if campaign_id is None or item_campaign_id == normalized_campaign_id:
                return True
        return False

    def campaign_item_count(self, campaign_id, urls=None):
        """Count queued URLs belonging to one campaign."""
        normalized_campaign_id = str(campaign_id) if campaign_id else None
        allowed_urls = None
        if urls is not None:
            allowed_urls = {normalize_stream_url(url) for url in urls if url}
        count = 0
        for item in self.items:
            item_campaign_id = item.get("campaign_id")
            item_campaign_id = str(item_campaign_id) if item_campaign_id else None
            if item_campaign_id != normalized_campaign_id:
                continue
            if allowed_urls is not None and normalize_stream_url(item.get("url")) not in allowed_urls:
                continue
            count += 1
        return count

    def remove(self, idx):
        """Remove item at index"""
        del self.items[idx]
        self.save()

    def clear(self):
        """Remove every queue item."""
        self.items.clear()
        self.save()
