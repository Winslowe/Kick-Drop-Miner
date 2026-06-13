"""Thread-safe service layer used by the web interface."""

from collections import deque
from datetime import datetime, timezone
import json
import os
import threading
import time
import re

from .api import (
    campaign_progress,
    fetch_drops_campaigns_and_progress,
    fetch_live_streamers_by_category,
)
from .browser import active_browser_count
from .config import Config, normalize_stream_url
from .worker import StreamWorker
from utils.helpers import cookie_file_for_domain


STATE_LABELS = {
    "waiting": "Bekliyor",
    "starting": "Başlatılıyor",
    "verifying": "Doğrulanıyor",
    "watch_verified": "İzleme Doğrulandı",
    "drop_waiting": "Drop İlerlemesi Bekleniyor",
    "drop_verified": "Drop Doğrulandı",
    "offline": "Çevrimdışı",
    "wrong_category": "Yanlış Kategori",
    "no_progress": "İlerleme Yok",
    "progress_error": "Kick Bağlantı Hatası",
    "verification_failed": "Doğrulama Başarısız",
    "browser_error": "Tarayıcı Hatası",
    "user_stopped": "Durduruldu",
    "app_closing": "Kapatılıyor",
    "completed": "Tamamlandı",
    "error": "Hata",
}

TRANSITION_MESSAGES = {
    "offline": "Kanal iki kez kontrol edildi ve çevrimdışı olduğu için sıradaki göreve geçildi.",
    "wrong_category": "Kanal gerekli oyunu yayınlamadığı için sıradaki uygun kanala geçildi.",
    "no_progress": "Video oynasa da Kick ilerlemesi değişmediği için alternatif kanal denendi.",
    "verification_failed": "Canlı yayın ve video akışı doğrulanamadığı için görev geçici olarak atlandı.",
    "browser_error": "Tarayıcı iki yeniden denemeden sonra açılamadığı için sıradaki göreve geçildi.",
}


class MinerService:
    """Owns queue execution and exposes serializable state to web clients."""

    def __init__(self, data_dir=None, user_id=None, username=None):
        self.data_dir = os.path.abspath(data_dir) if data_dir else None
        self.user_id = user_id
        self.username = username or "kullanıcı"
        safe_user = re.sub(r"[^a-zA-Z0-9_-]", "_", str(user_id or "legacy"))
        self.profile_prefix = f"user_{safe_user}_"
        self.cookie_path = cookie_file_for_domain(
            "kick.com",
            data_dir=self.data_dir,
        )
        self.config = Config(data_dir=self.data_dir)
        self._lock = threading.RLock()
        self._worker = None
        self._active_item_id = None
        self._queue_running = False
        self._states = {}
        self._last_saved_elapsed = {}
        self._last_logged_viewer = {}
        self._last_logged_progress = {}
        self._logs = deque(maxlen=120)
        self._inventory = []
        self._inventory_progress = []
        self._inventory_loading = False
        self._inventory_error = None
        self._inventory_updated_at = None
        self._started_at = time.time()
        self._load_inventory_cache()
        self._log("Web hizmeti hazır.")

    @property
    def _inventory_cache_path(self):
        root = self.data_dir or os.getcwd()
        return os.path.join(root, "inventory.json")

    def _load_inventory_cache(self):
        try:
            with open(self._inventory_cache_path, "r", encoding="utf-8") as cache_file:
                cached = json.load(cache_file)
            campaigns = cached.get("campaigns")
            progress = cached.get("progress")
            if isinstance(campaigns, list):
                self._inventory = campaigns
            if isinstance(progress, list):
                self._inventory_progress = progress
            self._inventory_updated_at = cached.get("updated_at")
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return

    def _save_inventory_cache(self):
        path = self._inventory_cache_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as cache_file:
            json.dump(
                {
                    "campaigns": self._inventory,
                    "progress": self._inventory_progress,
                    "updated_at": self._inventory_updated_at,
                },
                cache_file,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temporary, path)

    def _log(self, message, level="info"):
        with self._lock:
            self._logs.appendleft(
                {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "level": level,
                    "message": str(message),
                }
            )

    def _item_by_id(self, item_id):
        return next(
            (item for item in self.config.items if item.get("id") == item_id),
            None,
        )

    def _reward_for_campaign(self, campaign):
        rewards = [
            reward
            for reward in campaign.get("rewards", [])
            if isinstance(reward, dict)
        ]
        if not rewards:
            return {}
        progress = next(
            (
                item
                for item in self._inventory_progress
                if str(item.get("id") or item.get("campaign_id"))
                == str(campaign.get("id"))
            ),
            {},
        )
        progress_rewards = [
            reward
            for reward in progress.get("rewards", [])
            if isinstance(reward, dict)
        ]
        by_id = {
            str(item.get("id") or item.get("reward_id")): item
            for item in progress_rewards
            if item.get("id") or item.get("reward_id")
        }
        for index, reward in enumerate(rewards):
            reward_id = reward.get("id") or reward.get("reward_id")
            reward_progress = by_id.get(str(reward_id)) if reward_id else None
            if reward_progress is None and index < len(progress_rewards):
                reward_progress = progress_rewards[index]
            if not (reward_progress or {}).get("claimed"):
                return reward
        return rewards[-1]

    def _cookie_summary(self):
        path = self.cookie_path
        if not os.path.exists(path):
            return {"available": False, "count": 0, "expired": False}
        try:
            with open(path, "r", encoding="utf-8") as cookie_file:
                cookies = json.load(cookie_file)
            now = time.time()
            session_cookies = [
                item
                for item in cookies
                if isinstance(item, dict)
                and item.get("name") == "session_token"
                and item.get("value")
            ]
            expiries = [
                float(item["expiry"])
                for item in session_cookies
                if item.get("expiry")
            ]
            expired = bool(expiries) and max(expiries) < now
            return {
                "available": bool(session_cookies) and not expired,
                "count": len(cookies),
                "expired": expired,
            }
        except Exception:
            return {"available": False, "count": 0, "expired": True}

    def snapshot(self):
        with self._lock:
            items = []
            for item in self.config.items:
                item_id = item.get("id")
                state = dict(self._states.get(item_id, {}))
                status = state.get("state") or (
                    "completed" if item.get("finished") else "waiting"
                )
                elapsed = int(
                    state.get(
                        "elapsed_seconds",
                        item.get("elapsed_seconds", item.get("cumulative_time", 0)),
                    )
                    or 0
                )
                minutes = int(item.get("minutes") or 0)
                local_percent = (
                    min(100.0, elapsed / (minutes * 60) * 100)
                    if minutes
                    else 0.0
                )
                drop_progress = state.get("drop_progress")
                if drop_progress is None:
                    drop_progress = item.get("drop_progress")
                percent = (
                    float(drop_progress)
                    if drop_progress is not None
                    else local_percent
                )
                items.append(
                    {
                        **item,
                        "status": status,
                        "status_label": STATE_LABELS.get(status, status),
                        "elapsed_seconds": elapsed,
                        "progress_percent": round(percent, 2),
                        "live": state.get("live"),
                        "video_ok": bool(state.get("video_ok")),
                        "video_advanced": bool(state.get("video_advanced")),
                        "drop_progress": drop_progress,
                        "drop_verified": bool(
                            state.get(
                                "drop_verified",
                                item.get("drop_verified", False),
                            )
                        ),
                        "viewer_session": state.get("viewer_session"),
                        "error": state.get("error"),
                        "transition": item.get("last_transition"),
                        "active": item_id == self._active_item_id,
                    }
                )
            completed = sum(bool(item.get("finished")) for item in self.config.items)
            return {
                "items": items,
                "queue_running": self._queue_running,
                "active_item_id": self._active_item_id,
                "browser_count": active_browser_count(self.profile_prefix),
                "cookie": self._cookie_summary(),
                "stats": {
                    "total": len(items),
                    "completed": completed,
                    "pending": max(0, len(items) - completed),
                    "uptime_seconds": int(time.time() - self._started_at),
                },
                "inventory": {
                    "campaigns": list(self._inventory),
                    "progress": list(self._inventory_progress),
                    "loading": self._inventory_loading,
                    "error": self._inventory_error,
                    "updated_at": self._inventory_updated_at,
                },
                "logs": list(self._logs)[:80],
            }

    def is_running(self):
        with self._lock:
            return bool(self._worker and self._worker.is_alive())

    def add_stream(
        self,
        url,
        minutes=120,
        campaign_id=None,
        campaign_channels=None,
        required_category_id=None,
        is_global_drop=False,
        campaign_name=None,
        game=None,
        reward_image=None,
        reward_name=None,
    ):
        normalized = normalize_stream_url(url)
        if not normalized or "kick.com/" not in normalized:
            raise ValueError("Geçerli bir Kick kanal bağlantısı girin.")
        with self._lock:
            added = self.config.add(
                normalized,
                max(0, int(minutes or 0)),
                campaign_id=campaign_id,
                campaign_channels=campaign_channels,
                required_category_id=required_category_id,
                is_global_drop=is_global_drop,
                campaign_name=campaign_name,
                game=game,
                reward_image=reward_image,
                reward_name=reward_name,
            )
            if not added:
                raise ValueError("Bu yayın zaten listede.")
            item = self.config.items[-1]
            self._states[item["id"]] = {"state": "waiting", "elapsed_seconds": 0}
            self._log(f"Yayın listeye eklendi: {normalized}")
            return dict(item)

    def remove_stream(self, item_id):
        with self._lock:
            if item_id == self._active_item_id:
                raise ValueError("Çalışan yayını kaldırmadan önce madenciyi durdurun.")
            index = next(
                (
                    index
                    for index, item in enumerate(self.config.items)
                    if item.get("id") == item_id
                ),
                None,
            )
            if index is None:
                raise ValueError("Yayın bulunamadı.")
            removed = self.config.items[index].get("url")
            self.config.remove(index)
            self._states.pop(item_id, None)
            self._last_logged_viewer.pop(item_id, None)
            self._last_logged_progress.pop(item_id, None)
            self._log(f"Yayın kaldırıldı: {removed}")

    def clear_queue(self):
        self.stop("user_stopped")
        with self._lock:
            self.config.clear()
            self._states.clear()
            self._last_saved_elapsed.clear()
            self._last_logged_viewer.clear()
            self._last_logged_progress.clear()
            self._log("Yayın listesi sıfırlandı.")

    def start(self, item_id=None):
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Madenci zaten çalışıyor.")
            if not self.config.items:
                raise ValueError("Yayın listesi boş.")
            self._queue_running = True
            start_index = 0
            if item_id:
                start_index = next(
                    (
                        index
                        for index, item in enumerate(self.config.items)
                        if item.get("id") == item_id
                    ),
                    None,
                )
                if start_index is None:
                    raise ValueError("Başlatılacak yayın bulunamadı.")
            for item in self.config.items[start_index:]:
                if item.get("finished") or not item.get("campaign_id"):
                    continue
                item["tried_channels"] = []
                item["channel_statuses"] = {}
                item["browser_retry_counts"] = {}
                channels = item.get("campaign_channels") or []
                if channels:
                    item["url"] = normalize_stream_url(channels[0].get("url"))
            self.config.save()
        self._start_next(start_index)

    def _start_next(self, start_index):
        with self._lock:
            if not self._queue_running:
                return
            selected = None
            selected_index = None
            for index in range(start_index, len(self.config.items)):
                candidate = self.config.items[index]
                if not candidate.get("finished"):
                    selected = candidate
                    selected_index = index
                    break
            if selected is None:
                self._queue_running = False
                self._active_item_id = None
                self._worker = None
                self._log("Yayın sırası tamamlandı.")
                return

            item_id = selected["id"]
            selected.pop("last_transition", None)
            previous_state = dict(self._states.get(item_id, {}))
            self._active_item_id = item_id
            self._states[item_id] = {
                "state": "starting",
                "elapsed_seconds": int(selected.get("elapsed_seconds", 0) or 0),
                "drop_progress": previous_state.get(
                    "drop_progress",
                    selected.get("drop_progress"),
                ),
                "drop_verified": bool(
                    previous_state.get(
                        "drop_verified",
                        selected.get("drop_verified", False),
                    )
                ),
            }
            worker = StreamWorker(
                selected["url"],
                selected.get("minutes", 0),
                on_update=lambda payload: self._on_worker_update(item_id, payload),
                on_finish=lambda elapsed, completed, reason: self._on_worker_finish(
                    worker, item_id, selected_index, elapsed, completed, reason
                ),
                hide_player=os.environ.get("KDM_FORCE_HEADFUL") != "1",
                mute=True,
                mini_player=False,
                force_160p=True,
                required_category_id=selected.get("required_category_id"),
                campaign_id=selected.get("campaign_id"),
                cookie_path=self.cookie_path,
                profile_prefix=self.profile_prefix,
                initial_elapsed_seconds=int(
                    selected.get(
                        "elapsed_seconds",
                        selected.get("cumulative_time", 0),
                    )
                    or 0
                ),
                initial_drop_progress=self._states[item_id].get("drop_progress"),
                initial_drop_verified=self._states[item_id].get("drop_verified"),
            )
            self._worker = worker
            self._log(f"Doğrulama başlatıldı: {selected['url']}")
            worker.start()

    def _on_worker_update(self, item_id, payload):
        with self._lock:
            previous_state = dict(self._states.get(item_id, {}))
            self._states[item_id] = dict(payload)
            item = self._item_by_id(item_id)
            if item is None:
                return
            old_status = previous_state.get("state")
            new_status = payload.get("state")
            if new_status and new_status != old_status and new_status != "starting":
                level = (
                    "success"
                    if new_status in ("watch_verified", "drop_verified", "completed")
                    else "warning"
                    if new_status in (
                        "offline",
                        "wrong_category",
                        "no_progress",
                        "verification_failed",
                        "progress_error",
                    )
                    else "info"
                )
                self._log(
                    f"{item.get('url')}: {STATE_LABELS.get(new_status, new_status)}",
                    level,
                )

            viewer = payload.get("viewer_session") or {}
            viewer_key = (
                viewer.get("checked"),
                viewer.get("status"),
                viewer.get("authenticated"),
                viewer.get("tracking_started"),
                viewer.get("error"),
            )
            if viewer.get("checked") and self._last_logged_viewer.get(item_id) != viewer_key:
                self._last_logged_viewer[item_id] = viewer_key
                if viewer.get("ok") and viewer.get("tracking_started"):
                    self._log(
                        "Kick izleyici oturumu doğrulandı ve takip bağlantısı açıldı.",
                        "success",
                    )
                else:
                    self._log(
                        f"Kick izleyici doğrulaması başarısız: {viewer.get('error') or 'kimlik doğrulanamadı'}",
                        "warning",
                    )

            progress = payload.get("drop_progress")
            if progress is not None:
                item["drop_progress"] = progress
            item["drop_verified"] = bool(payload.get("drop_verified"))
            if progress is not None and self._last_logged_progress.get(item_id) != progress:
                previous_progress = self._last_logged_progress.get(item_id)
                self._last_logged_progress[item_id] = progress
                self._log(
                    f"Kick drop ilerlemesi %{float(progress):.2f} olarak okundu.",
                    "success" if previous_progress is not None else "info",
                )
            elapsed = int(payload.get("elapsed_seconds") or 0)
            item["elapsed_seconds"] = elapsed
            previous = self._last_saved_elapsed.get(item_id, -30)
            if elapsed - previous >= 30:
                self._last_saved_elapsed[item_id] = elapsed
                self.config.save()

    def _on_worker_finish(
        self,
        worker,
        item_id,
        item_index,
        elapsed,
        completed,
        reason,
    ):
        with self._lock:
            state = dict(self._states.get(item_id, {}))
            state.update(
                {
                    "state": "completed" if completed else (reason or "error"),
                    "elapsed_seconds": int(elapsed or 0),
                }
            )
            self._states[item_id] = state
            item = self._item_by_id(item_id)
            restart_index = None
            if item:
                item["elapsed_seconds"] = int(elapsed or 0)
                if completed:
                    item["finished"] = True
                elif (
                    item.get("campaign_id")
                    and reason == "browser_error"
                    and int(
                        item.setdefault("browser_retry_counts", {}).get(
                            normalize_stream_url(item.get("url")),
                            0,
                        )
                        or 0
                    )
                    < 2
                ):
                    current_url = normalize_stream_url(item.get("url"))
                    retry_counts = item.setdefault("browser_retry_counts", {})
                    retry_count = int(retry_counts.get(current_url, 0) or 0)
                    if retry_count < 2:
                        retry_counts[current_url] = retry_count + 1
                        restart_index = item_index
                        state.update(
                            {
                                "state": "starting",
                                "error": None,
                                "live": None,
                                "video_ok": False,
                                "video_advanced": False,
                            }
                        )
                        self._states[item_id] = state
                        self._log(
                            "Tarayıcı sekmesi kapandı; kanal temiz profille "
                            f"yeniden başlatılıyor ({retry_count + 1}/2).",
                            "warning",
                        )
                elif item.get("campaign_id") and reason in {
                    "offline",
                    "wrong_category",
                    "no_progress",
                    "verification_failed",
                    "browser_error",
                }:
                    current_url = normalize_stream_url(item.get("url"))
                    tried = {
                        normalize_stream_url(url)
                        for url in item.setdefault("tried_channels", [])
                    }
                    tried.add(current_url)
                    item["tried_channels"] = sorted(url for url in tried if url)
                    statuses = item.setdefault("channel_statuses", {})
                    statuses[current_url] = {
                        "state": reason,
                        "label": STATE_LABELS.get(reason, reason),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    item["last_transition"] = {
                        "reason": reason,
                        "label": STATE_LABELS.get(reason, reason),
                        "message": TRANSITION_MESSAGES.get(
                            reason,
                            "Görev tamamlanamadığı için sıradaki yayına geçildi.",
                        ),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    alternate = next(
                        (
                            channel
                            for channel in item.get("campaign_channels", [])
                            if normalize_stream_url(channel.get("url")) not in tried
                        ),
                        None,
                    )
                    if alternate:
                        item["url"] = normalize_stream_url(alternate.get("url"))
                        restart_index = item_index
                        state.update(
                            {
                                "state": "starting",
                                "error": None,
                                "live": None,
                                "video_ok": False,
                                "video_advanced": False,
                            }
                        )
                        self._states[item_id] = state
                        self._log(
                            "Yayın sona erdi; aynı drop için alternatif kanal "
                            f"seçildi: {alternate.get('username') or item['url']}",
                            "warning",
                        )
            self.config.save()
            self._log(
                f"Yayın sona erdi: {STATE_LABELS.get(reason, reason or 'Bilinmeyen')}",
                "success" if completed else "warning",
            )

        threading.Thread(
            target=self._continue_after_worker,
            args=(
                worker,
                item_id,
                restart_index if restart_index is not None else item_index + 1,
            ),
            daemon=True,
        ).start()

    def _continue_after_worker(self, worker, item_id, next_index):
        if threading.current_thread() is not worker:
            worker.join(timeout=15)
        if worker.is_alive():
            worker.force_close_driver()
            worker.join(timeout=5)
        with self._lock:
            if self._worker is worker:
                self._worker = None
            if self._active_item_id == item_id:
                self._active_item_id = None
            should_continue = self._queue_running
        if should_continue:
            self._start_next(next_index)

    def stop(self, reason="user_stopped"):
        with self._lock:
            self._queue_running = False
            worker = self._worker
        if worker and worker.is_alive():
            worker.stop(reason)
            worker.join(timeout=12)
            if worker.is_alive():
                worker.force_close_driver()
                worker.join(timeout=5)
        with self._lock:
            if self._worker is worker:
                self._worker = None
            self._active_item_id = None
            if reason != "app_closing":
                self._log("Madenci durduruldu.")

    def refresh_inventory(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise ValueError(
                    "Kaynak kullanımını düşük tutmak için envanteri madenci durduğunda yenileyin."
                )
            if self._inventory_loading:
                raise ValueError("Envanter zaten yenileniyor.")
            self._inventory_loading = True
            self._inventory_error = None
        try:
            result = fetch_drops_campaigns_and_progress(
                cookie_path=self.cookie_path,
                profile_prefix=self.profile_prefix,
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Kick envanteri alınamadı.")
            with self._lock:
                self._inventory = result.get("campaigns", [])
                self._inventory_progress = result.get("progress", [])
                campaigns_by_id = {
                    str(item.get("id")): item for item in self._inventory
                }
                for queue_item in self.config.items:
                    campaign = campaigns_by_id.get(
                        str(queue_item.get("campaign_id"))
                    )
                    if not campaign:
                        continue
                    reward = self._reward_for_campaign(campaign)
                    queue_item["reward_image"] = (
                        reward.get("image_url")
                        or reward.get("image")
                        or reward.get("icon_url")
                        or campaign.get("game_image")
                    )
                    queue_item["reward_name"] = (
                        reward.get("name") or campaign.get("name")
                    )
                    progress_record = next(
                        (
                            item
                            for item in self._inventory_progress
                            if str(item.get("id") or item.get("campaign_id"))
                            == str(campaign.get("id"))
                        ),
                        None,
                    )
                    percent, claimed = campaign_progress(progress_record)
                    queue_item["drop_progress"] = percent
                    queue_item["drop_verified"] = bool(
                        queue_item.get("drop_verified") or claimed
                    )
                self.config.save()
                self._inventory_updated_at = datetime.now(timezone.utc).isoformat()
                self._save_inventory_cache()
                if not result.get("progress_ok", True):
                    self._inventory_error = (
                        "Kampanyalar alındı fakat hesap ilerlemesi okunamadı."
                    )
                self._log(
                    f"{len(self._inventory)} kampanya envantere yüklendi.",
                    "success",
                )
            return self.snapshot()["inventory"]
        except Exception as error:
            with self._lock:
                self._inventory_error = str(error)
                self._log(f"Envanter hatası: {error}", "error")
            raise
        finally:
            with self._lock:
                self._inventory_loading = False

    def add_campaign(self, campaign_id):
        with self._lock:
            campaign = next(
                (
                    item
                    for item in self._inventory
                    if str(item.get("id")) == str(campaign_id)
                ),
                None,
            )
        if campaign is None:
            raise ValueError("Kampanya bulunamadı. Önce envanteri yenileyin.")

        channels = list(campaign.get("channels") or [])
        if not channels and campaign.get("category_id"):
            channels = fetch_live_streamers_by_category(
                campaign["category_id"],
                cookie_path=self.cookie_path,
                profile_prefix=self.profile_prefix,
            )
        if not channels:
            raise ValueError("Bu kampanya için uygun canlı kanal bulunamadı.")

        normalized_channels = [
            {
                "url": channel.get("url")
                or f"https://kick.com/{channel.get('slug')}",
                "username": channel.get("username")
                or channel.get("slug")
                or "Kick Kanalı",
                "profile_picture": channel.get("profile_picture") or "",
                "is_live": channel.get("is_live"),
            }
            for channel in channels
            if channel.get("url") or channel.get("slug")
        ]
        if any(
            str(item.get("campaign_id")) == str(campaign.get("id"))
            for item in self.config.items
        ):
            return {"added": 0, "skipped": len(normalized_channels)}

        reward = self._reward_for_campaign(campaign)
        reward_image = (
            reward.get("image_url")
            or reward.get("image")
            or reward.get("icon_url")
            or campaign.get("game_image")
        )
        self.add_stream(
            normalized_channels[0]["url"],
            0,
            campaign_id=campaign.get("id"),
            campaign_channels=normalized_channels,
            required_category_id=campaign.get("category_id"),
            is_global_drop=not bool(campaign.get("channels")),
            campaign_name=campaign.get("name"),
            game=campaign.get("game"),
            reward_image=reward_image,
            reward_name=reward.get("name") or campaign.get("name"),
        )
        return {"added": 1, "skipped": 0}

    def replace_cookies(self, cookies):
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("Geçerli bir çerez JSON listesi yükleyin.")
        cleaned = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if not name:
                continue
            cleaned_cookie = dict(cookie)
            cleaned_cookie["name"] = name
            cleaned_cookie["value"] = value
            cleaned_cookie.setdefault("domain", ".kick.com")
            cleaned_cookie.setdefault("path", "/")
            cleaned.append(cleaned_cookie)
        if not cleaned:
            raise ValueError("Dosyada kullanılabilir Kick çerezi bulunamadı.")
        path = self.cookie_path
        temp_path = f"{path}.tmp.{os.getpid()}"
        with open(temp_path, "w", encoding="utf-8") as cookie_file:
            json.dump(cleaned, cookie_file, ensure_ascii=False, indent=2)
            cookie_file.flush()
            os.fsync(cookie_file.fileno())
        os.replace(temp_path, path)
        self._log(f"{len(cleaned)} Kick çerezi güncellendi.", "success")
        return self._cookie_summary()

    def clear_logs(self):
        with self._lock:
            self._logs.clear()
            self._log("Konsol günlüğü temizlendi.")

    def shutdown(self):
        self.stop("app_closing")
