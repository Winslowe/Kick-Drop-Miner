import threading
from core.api import (
    campaign_progress,
    fetch_drops_campaigns_and_progress,
    fetch_live_streamers_by_category,
)
from core.browser import CookieManager
from utils.helpers import debug_print


def _campaign_progress(progress):
    return campaign_progress(progress)


class AutoPilot:
    def __init__(self, app):
        self.app = app
        self.running = False
        self.thread = None
        self.stop_event = threading.Event()

    def _set_status(self, text):
        self.app.after(0, lambda value=text: self.app.status_var.set(value))

    def _log(self, text):
        self.app.after(0, lambda value=text: self.app.ui_print(value))

    def _wait(self, seconds):
        return self.stop_event.wait(seconds)

    def start(self):
        if self.running:
            return
        
        self.running = True
        self.stop_event.clear()
        import os
        from utils.helpers import cookie_file_for_domain
        if not os.path.exists(cookie_file_for_domain("kick.com")):
            CookieManager.import_from_browser("kick.com")
        self._set_status("Oto-Pilot başlatıldı. Kampanyalar taranıyor...")
        
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        self._set_status("Oto-Pilot durduruldu.")

    def _run_loop(self):
        while self.running and not self.stop_event.is_set():
            if len(self.app.workers) > 0:
                if self._wait(10):
                    break
                continue

            self._set_status("Oto-Pilot: Uygun bir kampanya aranıyor...")
            try:
                import os
                from utils.helpers import cookie_file_for_domain
                if not os.path.exists(cookie_file_for_domain("kick.com")):
                    if not CookieManager.import_from_browser("kick.com"):
                        self._log("Uyarı: Çerezler bulunamadı. Lütfen 'Manuel Giriş' yapın.")
                
                self._log("Oto-Pilot: Aktif Kick kampanyaları sorgulanıyor...")
                res = fetch_drops_campaigns_and_progress()
                campaigns = res.get("campaigns", [])
                progress_list = res.get("progress", [])
            except Exception as e:
                debug_print(f"AutoPilot fetch error: {e}")
                if self._wait(60):
                    break
                continue
            
            # TDM Style Priority System:
            # 1. Sort by highest progress % first
            # 2. If no progress, sort by ends_at date (soonest first)
            
            campaigns_with_stats = []
            for c in campaigns:
                claimed = False
                progress_percent = 0.0
                
                if isinstance(progress_list, dict):
                    p = progress_list.get(str(c.get("id")), {})
                    progress_percent, claimed = _campaign_progress(p)
                else:
                    for p in progress_list:
                        if isinstance(p, dict):
                            if (p.get("id") or p.get("campaign_id")) == c.get("id"):
                                progress_percent, claimed = _campaign_progress(p)
                                break
                                
                if not claimed:
                    # Calculate end time priority if ends_at exists
                    ends_at = c.get("ends_at") or "9999-12-31"
                    campaigns_with_stats.append({
                        "campaign": c,
                        "progress": progress_percent,
                        "ends_at": ends_at
                    })
                    
            # Sort by progress (descending) and then ends_at (ascending)
            campaigns_with_stats.sort(key=lambda x: (-x["progress"], x["ends_at"]))
            
            target_campaign = None
            if campaigns_with_stats:
                target_campaign = campaigns_with_stats[0]["campaign"]
            
            if not target_campaign:
                self._set_status("Oto-Pilot: Bekleyen drop bulunamadı, 5 dakika bekleniyor...")
                self._log("Oto-Pilot: Tamamlanmamış aktif kampanya yok.")
                if self._wait(300):
                    break
                continue

            self._log(f"Oto-Pilot: Uygun kampanya bulundu - {target_campaign.get('name', 'Bilinmeyen Kampanya')}")

            channels_list = target_campaign.get("channels", [])
            target_channel = None
            available_channels = channels_list
            
            if channels_list:
                target_channel = channels_list[0]
            else:
                cat_id = target_campaign.get("category_id")
                if not cat_id:
                    self._set_status("Oto-Pilot: Kategori kimliği bulunamadı, bekleniyor...")
                    if self._wait(60):
                        break
                    continue

                self._set_status(f"Oto-Pilot: {target_campaign.get('name', 'Kampanya')} için yayıncı aranıyor...")
                channels = fetch_live_streamers_by_category(cat_id)
                
                if not channels:
                    self._set_status("Oto-Pilot: Bu kampanya için canlı yayıncı bulunamadı, bekleniyor...")
                    if self._wait(60):
                        break
                    continue

                target_channel = channels[0]
                available_channels = channels

            url = target_channel.get("url")
            if not url:
                url = f"https://kick.com/{target_channel.get('slug')}"
                
            self._log(f"Oto-Pilot: Yayıncı bulundu - {url}. İzleme başlatılıyor...")
            
            # Clear old items to keep UI clean
            self.app.config_data.items = []
            
            # Add to list and start
            campaign_channels = [
                {
                    "url": channel.get("url")
                    or f"https://kick.com/{channel.get('slug')}",
                    "username": channel.get("username") or channel.get("slug") or "",
                }
                for channel in available_channels
                if isinstance(channel, dict)
                and (channel.get("url") or channel.get("slug"))
            ]
            self.app.config_data.add(
                url,
                0,
                campaign_id=target_campaign.get("id"),
                campaign_channels=campaign_channels,
                required_category_id=target_campaign.get("category_id"),
                is_global_drop=not bool(channels_list),
            )
            self.app.after(0, self.app.refresh_list)
            self.app.after(0, self.app.start_all_in_order)
            
            # Wait a bit before next loop iteration to let worker start
            if self._wait(15):
                break
