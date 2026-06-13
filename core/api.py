"""Kick API helpers with short-lived, managed browser sessions."""

import json
import os
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlparse

from utils.helpers import cookie_file_for_domain, debug_print
from .browser import close_chrome_driver, make_chrome_driver


class BrowserRequestError(RuntimeError):
    pass


_api_driver_lock = threading.RLock()


def _http_headers(authenticated=False, cookie_path=None):
    headers = {
        "Accept": "application/json",
        "User-Agent": os.environ.get("KDM_USER_AGENT")
        or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
    }
    cookies = []
    if authenticated or cookie_path:
        cookie_path = cookie_path or cookie_file_for_domain("kick.com")
        try:
            with open(cookie_path, "r", encoding="utf-8") as cookie_file:
                cookies = [
                    item
                    for item in json.load(cookie_file)
                    if isinstance(item, dict)
                    and item.get("name")
                    and item.get("value") is not None
                ]
        except Exception:
            cookies = []
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{item['name']}={item['value']}" for item in cookies
        )
    if authenticated:
        token = next(
            (
                item.get("value")
                for item in cookies
                if item.get("name") == "session_token"
            ),
            None,
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_fetch_text(url, authenticated=False, cookie_path=None):
    request = urllib.request.Request(
        url,
        headers=_http_headers(
            authenticated=authenticated,
            cookie_path=cookie_path,
        ),
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read(300).decode("utf-8", "replace")
        raise BrowserRequestError(
            f"HTTP {error.code}: {detail or error.reason}"
        ) from error
    except Exception as error:
        raise BrowserRequestError(str(error)) from error


def fetch_viewer_token(cookie_path=None):
    """Fetch a short-lived Kick viewer token without exposing it to callers' logs."""
    headers = _http_headers(authenticated=True, cookie_path=cookie_path)
    headers.update(
        {
            "Origin": "https://kick.com",
            "Referer": "https://kick.com/",
            "X-CLIENT-TOKEN": os.environ.get(
                "KDM_VIEWER_CLIENT_TOKEN",
                "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823",
            ),
        }
    )
    request = urllib.request.Request(
        "https://websockets.kick.com/viewer/v1/token",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
            token = (
                body.get("token")
                if isinstance(body, dict)
                else None
            )
            if (
                token is None
                and isinstance(body, dict)
                and isinstance(body.get("data"), dict)
            ):
                token = body["data"].get("token")
            return {
                "ok": bool(token),
                "status": int(response.status),
                "token": token,
                "error": None if token else "Kick izleyici tokenı bulunamadı.",
            }
    except urllib.error.HTTPError as error:
        return {
            "ok": False,
            "status": int(error.code),
            "token": None,
            "error": f"Kick izleyici isteği HTTP {error.code} döndürdü.",
        }
    except Exception as error:
        return {
            "ok": False,
            "status": 0,
            "token": None,
            "error": str(error),
        }


def kick_channel_data_by_api(url):
    """Return public channel data when Kick answers, otherwise None."""
    try:
        parsed = urlparse(url)
        if "kick.com" not in parsed.netloc:
            return None
        username = parsed.path.strip("/").split("/")[0]
        if not username:
            return None
        data = json.loads(
            _http_fetch_text(f"https://kick.com/api/v2/channels/{username}")
        )
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def kick_live_status_by_api(url):
    """Return True/False when the public API answers, otherwise None."""
    data = kick_channel_data_by_api(url)
    if data is None:
        return None
    livestream = data.get("livestream")
    return bool(livestream and livestream.get("is_live"))


def is_campaign_expired(campaign):
    try:
        ends_at = campaign.get("ends_at")
        if not ends_at:
            return False
        now = datetime.now(timezone.utc)
        if isinstance(ends_at, str):
            value = ends_at.strip()
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            try:
                end_date = datetime.fromisoformat(value)
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                return now >= end_date.astimezone(timezone.utc)
            except ValueError:
                ends_at = float(value)
        return now >= datetime.fromtimestamp(float(ends_at), tz=timezone.utc)
    except Exception:
        return False


def _campaign_key(campaign):
    campaign_id = campaign.get("id") or campaign.get("campaign_id")
    if campaign_id:
        return f"id:{campaign_id}"
    return "meta:{name}|{start}|{end}".format(
        name=str(campaign.get("name") or "").strip().casefold(),
        start=campaign.get("starts_at") or "",
        end=campaign.get("ends_at") or "",
    )


def _deduplicate_progress(progress_data):
    merged = {}
    for progress in progress_data if isinstance(progress_data, list) else []:
        if not isinstance(progress, dict):
            continue
        key = _campaign_key(progress)
        if key not in merged:
            merged[key] = dict(progress)
            merged[key]["rewards"] = list(progress.get("rewards") or [])
            continue
        current = merged[key]
        old_rewards = list(current.get("rewards") or [])
        current.update(
            {key: value for key, value in progress.items() if value not in (None, "", [], {})}
        )
        rewards = {}
        for reward in old_rewards + list(progress.get("rewards") or []):
            if not isinstance(reward, dict):
                continue
            reward_key = reward.get("id") or reward.get("reward_id") or reward.get("name")
            if reward_key not in rewards:
                rewards[reward_key] = dict(reward)
            else:
                rewards[reward_key].update(
                    {
                        key: value
                        for key, value in reward.items()
                        if value not in (None, "", [], {})
                    }
                )
        current["rewards"] = list(rewards.values())
    return list(merged.values())


def campaign_progress(progress):
    """Return normalized percentage and claimed state for a progress record."""
    if not isinstance(progress, dict):
        return 0.0, False
    rewards = [item for item in (progress.get("rewards") or []) if isinstance(item, dict)]
    if rewards:
        ratios = []
        for reward in rewards:
            try:
                value = float(reward.get("progress", 0) or 0)
            except (TypeError, ValueError):
                value = 0
            ratios.append(value / 100 if value > 1 else value)
        percent = (sum(ratios) / len(ratios)) * 100 if ratios else 0
        claimed = all(bool(reward.get("claimed")) for reward in rewards)
        return min(100.0, max(0.0, percent)), claimed
    try:
        percent = float(progress.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        percent = 0
    if 0 < percent <= 1:
        percent *= 100
    claimed = bool(progress.get("is_claimed") or progress.get("is_fully_watched"))
    return min(100.0, max(0.0, percent)), claimed


def _load_cookies_to_driver(driver, cookie_path=None):
    cookie_path = cookie_path or cookie_file_for_domain("kick.com")
    if not os.path.exists(cookie_path):
        return False
    try:
        with open(cookie_path, "r", encoding="utf-8") as cookie_file:
            cookies = json.load(cookie_file)
    except Exception:
        return False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        cookie = dict(cookie)
        if cookie.get("expiry") is None:
            cookie.pop("expiry", None)
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass
    try:
        driver.refresh()
        time.sleep(1)
    except Exception:
        pass
    return True


def _new_api_driver(prefix="api", profile_prefix=""):
    return make_chrome_driver(
        headless=True,
        visible_width=400,
        visible_height=300,
        profile_dir_name=(
            f"{profile_prefix}{prefix}_{os.getpid()}_"
            f"{threading.get_ident()}_{time.time_ns()}"
        ),
        role="api",
    )


def _prepare_api_driver(driver, cookie_path=None):
    driver.get("https://web.kick.com/api/v1/drops/campaigns")
    time.sleep(1)
    _load_cookies_to_driver(driver, cookie_path=cookie_path)


def _session_token(driver):
    try:
        for cookie in driver.get_cookies():
            if cookie.get("name") == "session_token":
                return cookie.get("value")
    except Exception:
        pass
    return None


def _browser_navigate_text(driver, url, session_token=None, new_tab=False):
    original_handle = None
    if new_tab:
        original_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
    try:
        try:
            driver.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders",
                {
                    "headers": (
                        {"Authorization": f"Bearer {session_token}"}
                        if session_token
                        else {}
                    )
                },
            )
        except Exception:
            pass
        driver.get(url)
        text = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        )
        text = text or ""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("error"):
                raise BrowserRequestError(str(parsed.get("error")))
        except json.JSONDecodeError:
            pass
        return text
    finally:
        try:
            driver.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders", {"headers": {}}
            )
        except Exception:
            pass
        if new_tab:
            try:
                driver.close()
            finally:
                driver.switch_to.window(original_handle)


def _browser_fetch_text(driver, url, session_token=None):
    role = getattr(driver, "_kdm_role", None)
    if role == "api":
        return _browser_navigate_text(driver, url, session_token)

    script = """
    const done = arguments[arguments.length - 1];
    const url = arguments[0];
    const token = arguments[1];
    const headers = {'Accept': 'application/json'};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    fetch(url, {
      method: 'GET',
      headers: headers,
      credentials: 'include',
      cache: 'no-store'
    }).then(async response => {
      done(JSON.stringify({
        ok: response.ok,
        status: response.status,
        text: await response.text()
      }));
    }).catch(error => {
      done(JSON.stringify({ok: false, status: 0, error: String(error)}));
    });
    """
    try:
        driver.set_script_timeout(20)
    except Exception:
        pass
    raw = driver.execute_async_script(script, url, session_token)
    envelope = json.loads(raw or "{}")
    if not envelope.get("ok"):
        if role == "worker":
            return _browser_navigate_text(
                driver,
                url,
                session_token,
                new_tab=True,
            )
        detail = envelope.get("error") or f"HTTP {envelope.get('status', 0)}"
        raise BrowserRequestError(detail)
    return envelope.get("text") or ""


def _response_data(text):
    response = json.loads(text)
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        return response.get("data", [])
    return []


_RUST_KICKOFF_REWARD_IMAGES = {
    "large box": "large-wood-box.jpg",
    "garage door": "garage-door.jpg",
    "locker": "locker.jpg",
    "wallpaper": "wallpapers.jpg",
    "crossbow": "crossbow.jpg",
    "sks": "sks.jpg",
    "backpack": "large-backpack.jpg",
    "gloves": "tactical-gloves.jpg",
    "pickaxe": "pickaxe.jpg",
    "picco axe": "salvaged-axe.jpg",
    "double door": "sheet-metal-double-door.jpg",
    "krolay + raven door": "sheet-metal-double-door.jpg",
    "hjune + frost ar": "ak47.jpg",
    "m249": "m249.jpg",
    "panpots + v2 door": "sheet-metal-door.jpg",
}


def _official_reward_image(reward_name):
    """Return official Facepunch art for known Rust Kick Off 2 rewards."""
    folded = str(reward_name or "").casefold()
    for marker, filename in _RUST_KICKOFF_REWARD_IMAGES.items():
        if marker in folded:
            return f"https://files.facepunch.com/lewis/1b0111b1/{filename}"
    return ""


def _parse_campaigns(data):
    campaigns = []
    for campaign in data if isinstance(data, list) else []:
        if not isinstance(campaign, dict):
            continue
        if str(campaign.get("status") or "").lower() in (
            "expired",
            "ended",
            "cancelled",
        ):
            continue
        category = campaign.get("category") or {}
        game_name = category.get("name")
        campaign_name = str(campaign.get("name") or "")
        if not game_name:
            reward_names = " ".join(
                str(reward.get("name") or "")
                for reward in campaign.get("rewards", [])
                if isinstance(reward, dict)
            ).lower()
            if (
                "rust" in campaign_name.lower()
                or "rust" in reward_names
                or "garage door" in reward_names
                or "locker" in reward_names
            ):
                game_name = "Rust"
            elif (
                "escape from tarkov" in campaign_name.lower()
                or "eft" in campaign_name.lower()
                or "tarkov" in reward_names
            ):
                game_name = "Escape from Tarkov"
            else:
                game_name = "Genel Kampanyalar (Global)"

        rewards = []
        for reward in campaign.get("rewards", []):
            if not isinstance(reward, dict):
                continue
            parsed_reward = dict(reward)
            official_image = _official_reward_image(parsed_reward.get("name"))
            if official_image:
                parsed_reward["kick_image_url"] = parsed_reward.get("image_url")
                parsed_reward["image_url"] = official_image
                parsed_reward["image_source"] = "facepunch"
            rewards.append(parsed_reward)

        parsed = {
            "id": campaign.get("id"),
            "name": campaign.get("name", "Bilinmeyen Kampanya"),
            "category_id": category.get("id"),
            "category": category,
            "game": game_name,
            "game_slug": category.get("slug", ""),
            "game_image": category.get("image_url", ""),
            "status": campaign.get("status", "unknown"),
            "starts_at": campaign.get("starts_at"),
            "ends_at": campaign.get("ends_at"),
            "rewards": rewards,
            "channels": [],
        }
        for channel in campaign.get("channels", []):
            if not isinstance(channel, dict):
                continue
            slug = channel.get("slug")
            user = channel.get("user") or {}
            livestream = channel.get("livestream") or {}
            profile_picture = (
                user.get("profile_picture")
                or channel.get("profile_picture")
                or channel.get("avatar")
                or ""
            )
            if isinstance(profile_picture, dict):
                profile_picture = (
                    profile_picture.get("url")
                    or profile_picture.get("src")
                    or ""
                )
            if slug:
                parsed["channels"].append(
                    {
                        "slug": slug,
                        "username": user.get("username") or slug,
                        "url": f"https://kick.com/{slug}",
                        "profile_picture": profile_picture,
                        "is_live": (
                            bool(livestream.get("is_live"))
                            if livestream
                            else channel.get("is_live")
                        ),
                    }
                )
        if not parsed["channels"] and campaign.get("status") != "active":
            continue

        existing = next(
            (item for item in campaigns if _campaign_key(item) == _campaign_key(parsed)),
            None,
        )
        if existing is None:
            campaigns.append(parsed)
            continue
        existing_slugs = {channel.get("slug") for channel in existing["channels"]}
        existing["channels"].extend(
            channel
            for channel in parsed["channels"]
            if channel.get("slug") not in existing_slugs
        )
        if (
            existing["game"] == "Genel Kampanyalar (Global)"
            and parsed["game"] != "Genel Kampanyalar (Global)"
        ):
            existing.update(
                {
                    "category_id": parsed["category_id"],
                    "category": parsed["category"],
                    "game": parsed["game"],
                    "game_slug": parsed["game_slug"],
                    "game_image": parsed["game_image"],
                }
            )
    return sorted(
        campaigns,
        key=lambda item: (
            str(item.get("status") or "").lower() != "active",
            str(item.get("ends_at") or ""),
            str(item.get("name") or "").casefold(),
        ),
    )


def fetch_live_streamers_by_category(
    category_id,
    limit=24,
    driver=None,
    cookie_path=None,
    profile_prefix="",
):
    """Fetch live streamers, closing a locally-created browser immediately."""
    if not category_id:
        return []
    url = (
        "https://web.kick.com/api/v1/livestreams"
        f"?limit={int(limit)}&sort=viewer_count_desc&category_id={category_id}"
    )
    try:
        text = _http_fetch_text(url)
        return _parse_streamers_response(text, limit)
    except Exception as error:
        debug_print(f"Direct streamer API error, using browser: {error}")

    owned_driver = driver is None
    if owned_driver:
        _api_driver_lock.acquire()
    try:
        if owned_driver:
            driver = _new_api_driver(
                "streamers",
                profile_prefix=profile_prefix,
            )
            _prepare_api_driver(driver, cookie_path=cookie_path)
        text = _browser_fetch_text(
            driver,
            url,
        )
        return _parse_streamers_response(text, limit)
    except Exception as error:
        debug_print(f"Streamer API error: {error}")
        return []
    finally:
        if owned_driver and driver is not None:
            close_chrome_driver(driver)
        if owned_driver:
            _api_driver_lock.release()


def _parse_streamers_response(text, limit):
    response = json.loads(text)
    data = response.get("data", {}) if isinstance(response, dict) else {}
    streams = data.get("livestreams", []) if isinstance(data, dict) else data
    result = []
    for stream in streams[:limit] if isinstance(streams, list) else []:
        channel = stream.get("channel") or {}
        user = channel.get("user") or {}
        slug = channel.get("slug") or user.get("username") or user.get("slug")
        if not slug:
            continue
        result.append(
            {
                "url": f"https://kick.com/{slug}",
                "username": slug,
                "title": stream.get("session_title", ""),
                "viewer_count": stream.get("viewer_count", 0),
                "profile_picture": (
                    user.get("profile_picture")
                    or channel.get("profile_picture")
                    or ""
                ),
                "is_live": True,
            }
        )
    return result


def fetch_drops_progress(driver=None, cookie_path=None, profile_prefix=""):
    """Fetch progress; an externally supplied worker driver remains open."""
    try:
        text = _http_fetch_text(
            "https://web.kick.com/api/v1/drops/progress",
            authenticated=True,
            cookie_path=cookie_path,
        )
        return {
            "progress": _deduplicate_progress(_response_data(text)),
            "driver": None,
            "ok": True,
            "error": None,
        }
    except Exception as error:
        debug_print(f"Direct progress API error, using browser: {error}")

    owned_driver = driver is None
    if owned_driver:
        _api_driver_lock.acquire()
    try:
        if owned_driver:
            driver = _new_api_driver("progress", profile_prefix=profile_prefix)
            _prepare_api_driver(driver, cookie_path=cookie_path)
        text = _browser_fetch_text(
            driver,
            "https://web.kick.com/api/v1/drops/progress",
            _session_token(driver),
        )
        return {
            "progress": _deduplicate_progress(_response_data(text)),
            "driver": None,
            "ok": True,
            "error": None,
        }
    except Exception as error:
        debug_print(f"Progress API error: {error}")
        return {
            "progress": [],
            "driver": None,
            "ok": False,
            "error": str(error),
        }
    finally:
        if owned_driver and driver is not None:
            close_chrome_driver(driver)
        if owned_driver:
            _api_driver_lock.release()


def fetch_drops_campaigns_and_progress(cookie_path=None, profile_prefix=""):
    """Fetch campaigns and progress with one short-lived browser."""
    try:
        campaigns_text = _http_fetch_text(
            "https://web.kick.com/api/v1/drops/campaigns"
        )
        campaigns = _parse_campaigns(_response_data(campaigns_text))
        progress = []
        progress_ok = True
        progress_error = None
        try:
            progress_text = _http_fetch_text(
                "https://web.kick.com/api/v1/drops/progress",
                authenticated=True,
                cookie_path=cookie_path,
            )
            progress = _deduplicate_progress(_response_data(progress_text))
        except Exception as error:
            progress_ok = False
            progress_error = str(error)
        return {
            "campaigns": campaigns,
            "progress": progress,
            "driver": None,
            "ok": True,
            "campaigns_ok": True,
            "progress_ok": progress_ok,
            "error": progress_error,
        }
    except Exception as error:
        debug_print(f"Direct drops API error, using browser: {error}")

    driver = None
    with _api_driver_lock:
        try:
            driver = _new_api_driver("api", profile_prefix=profile_prefix)
            _prepare_api_driver(driver, cookie_path=cookie_path)
            campaigns_text = _browser_fetch_text(
                driver,
                "https://web.kick.com/api/v1/drops/campaigns",
            )
            campaigns = _parse_campaigns(_response_data(campaigns_text))
            progress = []
            progress_ok = True
            progress_error = None
            try:
                progress_text = _browser_fetch_text(
                    driver,
                    "https://web.kick.com/api/v1/drops/progress",
                    _session_token(driver),
                )
                progress = _deduplicate_progress(_response_data(progress_text))
            except Exception as error:
                progress_ok = False
                progress_error = str(error)
            return {
                "campaigns": campaigns,
                "progress": progress,
                "driver": None,
                "ok": True,
                "campaigns_ok": True,
                "progress_ok": progress_ok,
                "error": progress_error,
            }
        except Exception as error:
            debug_print(f"Drops API error: {error}")
            return {
                "campaigns": [],
                "progress": [],
                "driver": None,
                "ok": False,
                "campaigns_ok": False,
                "progress_ok": False,
                "error": str(error),
            }
        finally:
            if driver is not None:
                close_chrome_driver(driver)
