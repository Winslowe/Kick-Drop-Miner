import tkinter as tk
import customtkinter as ctk
import urllib.request
from io import BytesIO
from PIL import Image
import threading
import queue

from core import (
    fetch_drops_campaigns_and_progress,
    fetch_live_streamers_by_category,
    is_campaign_expired
)
from utils.helpers import debug_print
from core.config import normalize_stream_url


def _progress_ratio(value):
    """Normalize Kick progress values returned as either 0..1 or 0..100."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if number > 1:
        number /= 100
    return max(0.0, min(number, 1.0))


def _progress_status(value):
    return str(value or "not_started").strip().lower().replace("_", " ")


def _campaign_status_display(campaign):
    progress_status = _progress_status(campaign.get("progress_status"))
    if progress_status == "in progress":
        return "DEVAM EDİYOR", ("#f59e0b", "#d97706")
    if progress_status in ("claimed", "completed"):
        return "TAMAMLANDI", ("#10b981", "#059669")
    api_status = str(campaign.get("status") or "").strip().lower()
    if api_status == "active":
        return "AKTİF", ("#3b82f6", "#2563eb")
    if api_status in ("expired", "ended", "inactive"):
        return "SÜRESİ DOLDU", ("#64748b", "#475569")
    return "BEKLİYOR", ("#64748b", "#475569")


class DropsTab(ctk.CTkFrame):
    def __init__(self, parent, app, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.app = app
        self._refresh_token = 0
        self._loading_after_id = None
        self._campaign_action_widgets = []
    
        # Consistent theme
        ctk.set_appearance_mode("Dark" if self.app.config_data.dark_mode else "Light")
    
        # Main frame with background color
        main_frame = ctk.CTkFrame(self, fg_color=("gray92", "gray14"))
        main_frame.pack(fill="both", expand=True, padx=0, pady=0)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(1, weight=1)
    
        # Header with refresh button
        header_frame = ctk.CTkFrame(
            main_frame,
            fg_color=("white", "#111827"),
            corner_radius=14,
            height=78,
            border_width=1,
            border_color=("gray82", "#273449"),
        )
        header_frame.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 0))
        header_frame.grid_columnconfigure(0, weight=1)
        header_frame.grid_propagate(False)
    
        status_label = ctk.CTkLabel(
            header_frame,
            text="Drop Envanteri",
            font=ctk.CTkFont(size=21, family="Segoe UI", weight="bold"),
            anchor="w",
        )
        status_label.grid(row=0, column=0, sticky="w", padx=20, pady=(12, 0))

        self.inventory_subtitle = ctk.CTkLabel(
            header_frame,
            text="Kampanyalar ve ödül ilerlemeleri",
            font=ctk.CTkFont(size=12),
            text_color=("gray42", "gray65"),
            anchor="w",
        )
        self.inventory_subtitle.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))
    
        scrollable_frame = ctk.CTkScrollableFrame(
            main_frame, 
            label_text="",
            fg_color=("gray92", "gray14")
        )
        scrollable_frame.grid(row=1, column=0, sticky="nsew", padx=15, pady=15)
        scrollable_frame.grid_columnconfigure(0, weight=1)
    
        refresh_btn = ctk.CTkButton(
            header_frame,
            text=self.app.t("btn_refresh_drops"),
            width=130,
            height=35,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=("#3b82f6", "#2563eb"),
            hover_color=("#2563eb", "#1d4ed8"),
            command=lambda: self._refresh_drops(scrollable_frame, status_label),
        )
        refresh_btn.grid(row=0, column=1, rowspan=2, padx=20, pady=15)
    
        # Refresh function for buttons
        def refresh_callback():
            self._refresh_drops(scrollable_frame, status_label)
        
        # Store reference for buttons
        self._current_drops_refresh = refresh_callback
        
        # Show loading feedback immediately after the first frame is drawn.
        self.after(100, refresh_callback)
    
    def _refresh_drops(self, scrollable_frame, status_label):
        """Refreshes the list of drop campaigns with integrated progress"""
        self._refresh_token += 1
        refresh_token = self._refresh_token
    
        # Clean the frame
        def clear_frame():
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            status_label.configure(text="Drop Envanteri")
            self.inventory_subtitle.configure(text="Kick bağlantısı hazırlanıyor")

            loading_card = ctk.CTkFrame(
                scrollable_frame,
                corner_radius=18,
                fg_color=("white", "#111827"),
                border_width=1,
                border_color=("gray82", "#273449"),
            )
            loading_card.grid(row=0, column=0, sticky="ew", padx=40, pady=60)
            loading_card.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                loading_card,
                text="Envanter hazırlanıyor",
                font=ctk.CTkFont(size=20, weight="bold"),
            ).grid(row=0, column=0, pady=(28, 6), padx=30)

            loading_text = ctk.CTkLabel(
                loading_card,
                text="Kick oturumu kontrol ediliyor...",
                font=ctk.CTkFont(size=12),
                text_color=("gray42", "gray65"),
            )
            loading_text.grid(row=1, column=0, pady=(0, 16), padx=30)

            loading_bar = ctk.CTkProgressBar(
                loading_card,
                height=12,
                corner_radius=8,
                progress_color="#22c55e",
            )
            loading_bar.grid(row=2, column=0, sticky="ew", padx=40, pady=(0, 8))
            loading_bar.set(0.05)

            loading_percent = ctk.CTkLabel(
                loading_card,
                text="%5",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#22c55e",
            )
            loading_percent.grid(row=3, column=0, pady=(0, 28))

            progress_state = {"value": 5}

            def animate_loading():
                if refresh_token != self._refresh_token or not loading_card.winfo_exists():
                    return
                value = progress_state["value"]
                if value < 88:
                    value += 3 if value < 55 else 1
                    progress_state["value"] = value
                    loading_bar.set(value / 100)
                    loading_percent.configure(text=f"%{value}")
                    if value >= 65:
                        loading_text.configure(text="Kampanyalar ve ilerlemeler eşleştiriliyor...")
                    elif value >= 30:
                        loading_text.configure(text="Aktif kampanyalar alınıyor...")
                self._loading_after_id = self.after(120, animate_loading)

            animate_loading()
            self._loading_widgets = (loading_bar, loading_percent, loading_text)
    
        self.after(0, clear_frame)
    
        def show_fetch_error(error_text):
            if refresh_token != self._refresh_token:
                return
            if self._loading_after_id:
                try:
                    self.after_cancel(self._loading_after_id)
                except Exception:
                    pass
                self._loading_after_id = None
            self.inventory_subtitle.configure(text=error_text)
            status_label.configure(text="Drop Envanteri")
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            error_card = ctk.CTkFrame(
                scrollable_frame,
                corner_radius=16,
                fg_color=("white", "#111827"),
                border_width=1,
                border_color=("#fecaca", "#7f1d1d"),
            )
            error_card.grid(row=0, column=0, sticky="ew", padx=50, pady=60)
            ctk.CTkLabel(
                error_card,
                text="Envanter yüklenemedi",
                font=ctk.CTkFont(size=18, weight="bold"),
            ).pack(pady=(24, 6))
            ctk.CTkLabel(
                error_card,
                text="Bağlantıyı ve Kick girişini kontrol edip Yenile düğmesine basın.",
                text_color=("gray42", "gray65"),
            ).pack(pady=(0, 24))

        def fetch_data_thread():
            try:
                result = fetch_drops_campaigns_and_progress()
                result_queue.put(("success", result))
            except Exception as e:
                result_queue.put(("error", f"Hata: {e}"))

        result_queue = queue.Queue()

        def poll_result():
            if refresh_token != self._refresh_token:
                return
            try:
                result_type, payload = result_queue.get_nowait()
            except queue.Empty:
                self.after(80, poll_result)
                return
            if result_type == "success":
                build_ui(payload)
            else:
                show_fetch_error(payload)

        def build_ui(result):
            if refresh_token != self._refresh_token:
                return
            if self._loading_after_id:
                try:
                    self.after_cancel(self._loading_after_id)
                except Exception:
                    pass
                self._loading_after_id = None
            for widget in getattr(self, "_loading_widgets", ()):
                try:
                    if isinstance(widget, ctk.CTkProgressBar):
                        widget.set(1)
                    elif widget.cget("text").startswith("%"):
                        widget.configure(text="%100")
                    else:
                        widget.configure(text="Envanter hazır")
                except Exception:
                    pass
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            self._campaign_action_widgets.clear()
            driver = result.get("driver")
            try:
                campaigns = result.get("campaigns", [])
                progress_data = result.get("progress", [])
                progress_data = [p for p in progress_data if isinstance(p, dict)]
                
                if not campaigns:
                    show_fetch_error("Aktif kampanya alınamadı")
                    return
    
                # Create a progress lookup by campaign ID
                progress_by_id = {}
                for prog in progress_data:
                    if not isinstance(prog, dict):
                        continue  # Skip unexpected progress entries
                    campaign_id = prog.get("id") or prog.get("campaign_id")
                    if campaign_id:
                        progress_by_id[campaign_id] = prog
                
                # Merge progress data into campaigns
                for campaign in campaigns:
                    campaign_id = campaign.get("id")
                    if campaign_id in progress_by_id:
                        # Campaign has progress - merge progress info
                        prog = progress_by_id[campaign_id]
                        campaign["progress_data"] = prog
                        campaign["progress_status"] = _progress_status(prog.get("status"))
                        campaign["progress_units"] = prog.get("progress_units", 0)
                        
                        # Merge category from progress data if not already in campaign
                        if "category" in prog and "category" not in campaign:
                            campaign["category"] = prog["category"]
                        elif "category" in prog:
                            # Update category if progress has more complete data
                            campaign["category"] = prog["category"]
                        
                        # Merge reward progress
                        reward_progress = {}
                        for reward in prog.get("rewards", []):
                            reward_id = reward.get("id")
                            if reward_id:
                                reward_progress[reward_id] = {
                                    "progress": _progress_ratio(reward.get("progress", 0.0)),
                                    "claimed": reward.get("claimed", False),
                                    "required_units": reward.get("required_units", 0),
                                }
                        
                        # Attach progress to each reward in campaign
                        for reward in campaign.get("rewards", []):
                            reward_id = reward.get("id")
                            if reward_id in reward_progress:
                                reward["progress"] = reward_progress[reward_id]["progress"]
                                reward["claimed"] = reward_progress[reward_id]["claimed"]
                                reward["progress_required_units"] = reward_progress[reward_id]["required_units"]
                    else:
                        # Campaign has no progress - not started
                        campaign["progress_data"] = None
                        campaign["progress_status"] = "not_started"
                        campaign["progress_units"] = 0
                        for reward in campaign.get("rewards", []):
                            reward["progress"] = 0.0
                            reward["claimed"] = False
    
                # Filter campaigns into active and expired
                active_campaigns = []
                expired_campaigns = []
                
                for campaign in campaigns:
                    if is_campaign_expired(campaign):
                        expired_campaigns.append(campaign)
                    else:
                        active_campaigns.append(campaign)
                
                # Group active campaigns by game and sort by progress status
                games = {}
                for campaign in active_campaigns:
                    # Double-check: skip if expired (safety check)
                    if is_campaign_expired(campaign):
                        continue
                    game_name = campaign["game"]
                    if game_name not in games:
                        games[game_name] = {
                            "image": campaign.get("game_image", ""),
                            "campaigns": [],
                        }
                    games[game_name]["campaigns"].append(campaign)
                
                # Sort campaigns within each game by progress status
                # Priority: in progress > not started > claimed/completed
                def sort_key(campaign):
                    status = _progress_status(campaign.get("progress_status"))
                    if status == "in progress":
                        return 0
                    elif status == "not started":
                        return 1
                    elif status == "claimed":
                        return 2
                    else:
                        return 3
                
                for game_name, game_data in games.items():
                    game_data["campaigns"].sort(key=sort_key)
                
                # Sort games by priority: games with in-progress campaigns first
                def game_priority(game_data):
                    campaigns = game_data["campaigns"]
                    # Check if any campaign is in progress
                    has_in_progress = any(
                        _progress_status(c.get("progress_status")) == "in progress"
                        for c in campaigns
                    )
                    if has_in_progress:
                        return 0
                    # Check if any campaign is not started
                    has_not_started = any(
                        _progress_status(c.get("progress_status")) == "not started"
                        for c in campaigns
                    )
                    if has_not_started:
                        return 1
                    return 2
                
                # Convert to list, sort, then back to dict (or use OrderedDict)
                games_list = sorted(games.items(), key=lambda x: game_priority(x[1]))
                games = dict(games_list)
    
                status_text = self.app.t("drops_loaded", count=len(active_campaigns))
                if expired_campaigns:
                    status_text += f" ({len(expired_campaigns)} süresi dolmuş)"
                status_label.configure(text="Drop Envanteri")
                self.inventory_subtitle.configure(text=status_text)
    
                # Add toggle for showing expired campaigns
                if not hasattr(scrollable_frame, "_show_expired_var"):
                    scrollable_frame._show_expired_var = tk.BooleanVar(value=False)
                
                show_expired = scrollable_frame._show_expired_var.get()
                
                # Display each game with its campaigns
                row_idx = 0
                for game_name, game_data in games.items():
                    # Separate campaigns into active and completed
                    game_active_campaigns = []
                    game_completed_campaigns = []
                    
                    for campaign in game_data["campaigns"]:
                        status = _progress_status(campaign.get("progress_status"))
                        if status in ("claimed", "completed"):
                            game_completed_campaigns.append(campaign)
                        else:
                            game_active_campaigns.append(campaign)
                    # Frame for game (collapsible) - improved style
                    game_frame = ctk.CTkFrame(
                        scrollable_frame, 
                        corner_radius=16,
                        border_width=2,
                        border_color=("#3b82f6", "#2563eb")
                    )
                    game_frame.grid(row=row_idx, column=0, sticky="ew", padx=0, pady=10)
                    game_frame.grid_columnconfigure(0, weight=1)
    
                    # Variable for toggle collapse - Start COLLAPSED for massive performance boost
                    is_expanded = tk.BooleanVar(value=False)
    
                    # Game header (clickable to collapse/expand) - larger and colored
                    game_header = ctk.CTkFrame(
                        game_frame, 
                        fg_color=("#e0f2fe", "#1e3a5f"),
                        cursor="hand2",
                        corner_radius=10
                    )
                    game_header.grid(row=0, column=0, sticky="ew", padx=3, pady=3)
                    # Don't expand any column - let content determine width
                    game_header.grid_columnconfigure(3, weight=1)  # Expand the empty space column
    
                    # Expand/collapse icon - more visible
                    collapse_icon = ctk.CTkLabel(
                        game_header, 
                        text="▶", 
                        font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold"),
                        text_color=("#3b82f6", "#60a5fa")
                    )
                    collapse_icon.grid(row=0, column=0, padx=(15, 10), pady=12)
    
                    # Game image (if available) - larger
                    col_offset = 1
                    if game_data["image"]:
                        img_label = ctk.CTkLabel(
                            game_header, text="img", font=ctk.CTkFont(size=10), width=48, height=48, cursor="hand2"
                        )
                        img_label.grid(row=0, column=1, padx=(0, 12))
                        col_offset = 2
                        
                        def load_game_image(url, label):
                            try:
                                import urllib.request
                                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                                with urllib.request.urlopen(req, timeout=5) as response:
                                    image_data = response.read()
                                game_img = Image.open(BytesIO(image_data))
                                game_img = game_img.resize((48, 48), Image.Resampling.LANCZOS)
                                
                                def update_ui():
                                    try:
                                        if label.winfo_exists():
                                            game_photo = ctk.CTkImage(light_image=game_img, dark_image=game_img, size=(48, 48))
                                            label.configure(image=game_photo, text="")
                                            label.image = game_photo
                                    except:
                                        pass
                                self.after(0, update_ui)
                            except Exception as e:
                                pass
                                
                        threading.Thread(target=load_game_image, args=(game_data["image"], img_label), daemon=True).start()
                    else:
                        # Fallback icon for Global Campaigns
                        img_label = ctk.CTkLabel(
                            game_header, text="🎁", font=ctk.CTkFont(size=32), width=48, height=48, cursor="hand2"
                        )
                        img_label.grid(row=0, column=1, padx=(0, 12))
                        col_offset = 2
    
                    # Game name - larger and colored
                    game_label = ctk.CTkLabel(
                        game_header,
                        text=game_name,
                        font=ctk.CTkFont(size=24, family="Segoe UI", weight="bold"),
                        text_color=("#1e40af", "#93c5fd")
                    )
                    game_label.grid(row=0, column=col_offset, sticky="w", padx=(0, 0))
                    
                    # Spacer column to push badge to the right
                    # (column 3 has weight=1)
    
                    # Number of campaigns - styled badge, aligned right
                    count_label = ctk.CTkLabel(
                        game_header,
                        text=f"{len(game_data['campaigns'])} kampanya",
                        font=ctk.CTkFont(size=11, weight="bold"),
                        fg_color=("#bfdbfe", "#1e40af"),
                        corner_radius=16,
                        padx=10,
                        pady=4
                    )
                    count_label.grid(row=0, column=4, sticky="e", padx=(15, 15))
    
                    # Campaigns frame (can be hidden)
                    campaigns_container = ctk.CTkFrame(
                        game_frame, fg_color="transparent"
                    )
                    campaigns_container.grid(row=1, column=0, sticky="ew")
                    campaigns_container.grid_columnconfigure(0, weight=1)
                    
                    # Initially hide container since default is collapsed
                    campaigns_container.grid_remove()
    
                    # Fonction toggle
                    def toggle_collapse(
                        event=None,
                        icon=collapse_icon,
                        container=campaigns_container,
                        var=is_expanded,
                    ):
                        if var.get():
                            container.grid_remove()
                            icon.configure(text="▶")
                            var.set(False)
                        else:
                            container.grid()
                            icon.configure(text="▼")
                            var.set(True)
    
                    # Make header clickable
                    game_header.bind("<Button-1>", toggle_collapse)
                    game_label.bind("<Button-1>", toggle_collapse)
                    collapse_icon.bind("<Button-1>", toggle_collapse)
                    count_label.bind("<Button-1>", toggle_collapse)
                    # Bind img_label if it exists
                    for widget in game_header.winfo_children():
                        if isinstance(widget, ctk.CTkLabel) and hasattr(
                            widget, "image"
                        ):
                            widget.bind("<Button-1>", toggle_collapse)
    
                    # Display active campaigns first
                    camp_idx = 0
                    for campaign in game_active_campaigns:
                        self._create_campaign_display(campaigns_container, campaign, camp_idx, scrollable_frame, game_data, status_label)
                        camp_idx += 1
                    
                    # Display completed campaigns in a collapsible section
                    if game_completed_campaigns:
                        # Add separator if there are active campaigns
                        if active_campaigns:
                            separator = ctk.CTkFrame(campaigns_container, fg_color="transparent", height=2)
                            separator.grid(row=camp_idx, column=0, sticky="ew", padx=8, pady=6)
                            camp_idx += 1
                        
                        # Collapsible header for completed campaigns
                        completed_header_frame = ctk.CTkFrame(
                            campaigns_container,
                            fg_color=("gray85", "#2d3748"),
                            corner_radius=8,
                            cursor="hand2"
                        )
                        completed_header_frame.grid(row=camp_idx, column=0, sticky="ew", padx=8, pady=6)
                        completed_header_frame.grid_columnconfigure(1, weight=1)
                        
                        completed_expanded = tk.BooleanVar(value=False)  # Collapsed by default
                        
                        completed_collapse_icon = ctk.CTkLabel(
                            completed_header_frame,
                            text="▶",
                            font=ctk.CTkFont(size=12, weight="bold"),
                            text_color=("gray60", "gray40")
                        )
                        completed_collapse_icon.grid(row=0, column=0, padx=(12, 8), pady=8)
                        
                        completed_header_label = ctk.CTkLabel(
                            completed_header_frame,
                            text=f"{self.app.t('drops_completed_campaigns')} ({len(game_completed_campaigns)})",
                            font=ctk.CTkFont(size=12, weight="bold"),
                            text_color=("gray60", "gray40")
                        )
                        completed_header_label.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=8)
                        
                        # Container for completed campaigns
                        completed_container = ctk.CTkFrame(
                            campaigns_container,
                            fg_color="transparent"
                        )
                        completed_container.grid(row=camp_idx + 1, column=0, sticky="ew")
                        completed_container.grid_columnconfigure(0, weight=1)
                        completed_container.grid_remove()  # Hidden by default
                        
                        def toggle_completed(event=None):
                            if completed_expanded.get():
                                completed_container.grid_remove()
                                completed_collapse_icon.configure(text="▶")
                                completed_expanded.set(False)
                            else:
                                completed_container.grid()
                                completed_collapse_icon.configure(text="▼")
                                completed_expanded.set(True)
                        
                        completed_header_frame.bind("<Button-1>", toggle_completed)
                        completed_collapse_icon.bind("<Button-1>", toggle_completed)
                        completed_header_label.bind("<Button-1>", toggle_completed)
                        
                        # Display completed campaigns
                        for comp_idx, campaign in enumerate(game_completed_campaigns):
                            self._create_campaign_display(completed_container, campaign, comp_idx, scrollable_frame, game_data, status_label)
                        
                        camp_idx += 2  # Skip header and container rows
                    
                    row_idx += 1
                
                # Display expired campaigns section if toggle is on
                if expired_campaigns and hasattr(scrollable_frame, "_show_expired_var") and scrollable_frame._show_expired_var.get():
                        expired_separator = ctk.CTkFrame(scrollable_frame, fg_color=("gray70", "gray30"), height=2)
                        expired_separator.grid(row=row_idx, column=0, sticky="ew", padx=0, pady=15)
                        row_idx += 1
                        
                        expired_label = ctk.CTkLabel(
                            scrollable_frame,
                            text=f"Süresi Dolan Kampanyalar ({len(expired_campaigns)})",
                            font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold"),
                            text_color=("#6b7280", "#9ca3af"),
                        )
                        expired_label.grid(row=row_idx, column=0, sticky="w", padx=15, pady=10)
                        row_idx += 1
                        
                        for exp_idx, campaign in enumerate(expired_campaigns):
                            self._create_campaign_display(scrollable_frame, campaign, exp_idx, scrollable_frame, {"image": ""}, status_label)
                            row_idx += 1
                
                # Force update
                scrollable_frame.update_idletasks()
            except Exception as e:
                status_label.configure(text=f"Hata: {str(e)}")
                import traceback
                traceback.print_exc()
            finally:
                # The API driver is shared and reused by later refreshes.
                pass
    
        # Fetch data on background thread, which then calls build_ui on main thread
        threading.Thread(target=fetch_data_thread, daemon=True).start()
        self.after(80, poll_result)
    
    def _auto_find_streamers_for_game(self, campaign, category_id, scrollable_frame, status_label):
        """Auto-find and add live streamers for a global drop campaign"""
        def find_and_add():
            game_name = campaign.get('game', 'game')
            debug_print(f"DEBUG: Starting search for live streamers")
            debug_print(f"DEBUG: Campaign: {campaign.get('name', 'unknown')}")
            debug_print(f"DEBUG: Game: {game_name}")
            debug_print(f"DEBUG: Category ID: {category_id}")
            
            self.after(0, lambda: status_label.configure(text=f"{game_name} için canlı yayıncılar aranıyor..."))
            
            debug_print(f"DEBUG: Calling fetch_live_streamers_by_category with category_id={category_id}")
            streamers = fetch_live_streamers_by_category(category_id, limit=24)
            debug_print(f"DEBUG: Found {len(streamers)} streamers")
            
            if not streamers:
                self.after(0, lambda: status_label.configure(text=f"{game_name} için canlı yayıncı bulunamadı"))
                debug_print(f"DEBUG: No streamers found, closing driver if needed")
                return
            
            debug_print(f"DEBUG: Processing {len(streamers)} streamers to add to queue")
            self.after(0, lambda: status_label.configure(text=f"{len(streamers)} yayıncı listeye ekleniyor..."))
            
            # Calculate maximum required time from rewards (cumulative drops)
            rewards = campaign.get("rewards", [])
            max_required_minutes = 0
            for reward in rewards:
                required_units = reward.get("required_units", 0)
                if required_units > max_required_minutes:
                    max_required_minutes = required_units
            
            # If no rewards found, default to 120
            if max_required_minutes == 0:
                max_required_minutes = 120
            
            debug_print(f"DEBUG: Campaign has {len(rewards)} rewards, max required: {max_required_minutes} minutes")
            
            # Add all found streamers to queue
            count = 0
            skipped = 0
            campaign_id = campaign.get("id")
            all_streamers = [{"url": s["url"], "username": s["username"]} for s in streamers]
            
            for streamer in streamers:
                try:
                    url = streamer["url"]
                    username = streamer.get("username", "unknown")
                    debug_print(f"DEBUG: Processing streamer: {username} ({url})")
                    
                    if self._is_channel_in_list(url, campaign_id):
                        debug_print(f"DEBUG: Streamer {username} already in list, skipping")
                        skipped += 1
                        continue
                    
                    # Store all streamers as alternatives for each other
                    # Use max_required_minutes for cumulative drops
                    debug_print(f"DEBUG: Adding {username} to queue with target: {max_required_minutes} minutes")
                    added = self.app.config_data.add(
                        url, 
                        max_required_minutes, 
                        campaign_id, 
                        all_streamers,
                        required_category_id=category_id,
                        is_global_drop=True
                    )
                    if added:
                        count += 1
                    else:
                        skipped += 1
                except Exception as e:
                    debug_print(f"DEBUG: Error adding streamer {streamer.get('username', 'unknown')}: {e}")
                    import traceback
                    traceback.print_exc()
            
            debug_print(f"DEBUG: Added {count} streamers, skipped {skipped} (already in list)")
            result_text = (
                f"{game_name} için {count} canlı yayıncı eklendi"
                + (f" ({skipped} tanesi zaten listedeydi)" if skipped > 0 else "")
            )
            self.after(0, self.app.refresh_list)
            self.after(0, lambda: status_label.configure(text=result_text))
            
            # Auto-start if enabled
            if self.app.config_data.auto_start and not self.app.queue_running:
                debug_print("DEBUG: Auto-start enabled, starting queue")
                self.after(500, self._auto_start_queue)
            else:
                debug_print("DEBUG: Auto-start disabled or queue already running")
            
            # Do not close the shared API driver.
        
        threading.Thread(target=find_and_add, daemon=True).start()
    
    def _create_campaign_display(self, parent, campaign, camp_idx, scrollable_frame, game_data, status_label=None):
        """Helper function to create a campaign display frame"""
        try:
            campaign_frame = ctk.CTkFrame(
                parent,
                corner_radius=14,
                fg_color=("white", "#111827"),
                border_width=1,
                border_color=("#dbe3ef", "#273449")
            )
            campaign_frame.grid(
                row=camp_idx, column=0, sticky="ew", padx=8, pady=6
            )
            campaign_frame.grid_columnconfigure(0, weight=1)
    
            # Campaign header - improved style
            header = ctk.CTkFrame(campaign_frame, fg_color="transparent")
            header.grid(row=0, column=0, sticky="ew", padx=15, pady=(12, 8))
            header.grid_columnconfigure(1, weight=1)
    
            campaign_name_label = ctk.CTkLabel(
                header,
                text=campaign["name"],
                font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold"),
                anchor="w"
            )
            campaign_name_label.grid(
                row=0, column=0, columnspan=2, sticky="w"
            )
    
            # Status badge - show progress status if available
            status_text, status_color = _campaign_status_display(campaign)
            
            status_badge = ctk.CTkLabel(
                header,
                text=status_text,
                font=ctk.CTkFont(size=10, weight="bold"),
                fg_color=status_color,
                text_color="white",
                corner_radius=6,
                padx=10,
                pady=4,
            )
            status_badge.grid(row=0, column=2, sticky="e")
    
            # Display rewards (drops) with images
            rewards = campaign.get("rewards", [])
            if rewards:
                rewards_frame = ctk.CTkFrame(
                    campaign_frame, 
                    fg_color=("gray90", "#111827"),
                    corner_radius=8
                )
                rewards_frame.grid(
                    row=1, column=0, sticky="ew", padx=15, pady=(0, 10)
                )
                rewards_frame.grid_columnconfigure(1, weight=1)
    
                rewards_label = ctk.CTkLabel(
                    rewards_frame,
                    text="Ödüller",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=("#7c3aed", "#a78bfa")
                )
                rewards_label.grid(row=0, column=0, sticky="w", padx=(12, 10), pady=10)
    
                # Horizontal frame for drop images
                images_frame = ctk.CTkFrame(
                    rewards_frame, fg_color="transparent"
                )
                images_frame.grid(row=0, column=1, sticky="w", pady=10, padx=(0, 12))
    
                for rew_idx, reward in enumerate(
                    rewards[:6]
                ):  # Max 6 rewards shown
                    try:
                        # Build complete image URL
                        reward_img_url = reward.get("image_url", "")
                        if reward_img_url and not reward_img_url.startswith(
                            "http"
                        ):
                            reward_img_url = (
                                f"https://ext.cdn.kick.com/{reward_img_url}"
                            )
    
                        reward_name = reward.get("name", "Bilinmeyen Ödül")
                        required_mins = reward.get("required_units", 0)
                        
                        # Get progress info if available
                        progress = _progress_ratio(reward.get("progress", 0.0))
                        claimed = reward.get("claimed", False)
                        progress_units = campaign.get("progress_units", 0)
                        
                        # Build tooltip with progress info
                        if progress > 0 or claimed:
                            progress_percent = int(progress * 100)
                            if claimed:
                                tooltip_text = f"{reward_name}\n{required_mins} dakika\nALINDI (%{progress_percent})"
                            else:
                                tooltip_text = f"{reward_name}\n{required_mins} dakika\n%{progress_percent} ({progress_units}/{required_mins})"
                        else:
                            tooltip_text = f"{reward_name}\n{required_mins} dakika\nBaşlatılmadı"

                        # Frame with border for each reward - change border color if claimed
                        border_color = ("#10b981", "#059669") if claimed else ("#f59e0b", "#d97706") if progress > 0 else ("#d1d5db", "#374151")
                        border_width = 3 if claimed or progress > 0 else 2
                        
                        rew_container = ctk.CTkFrame(
                            images_frame,
                            fg_color=("white", "#0f172a"),
                            border_width=border_width,
                            border_color=border_color,
                            corner_radius=8,
                            width=60,
                            height=60
                        )
                        rew_container.grid(row=0, column=rew_idx, padx=4)
                        rew_container.grid_propagate(False)
                        
                        # Create label without image initially
                        rew_label = ctk.CTkLabel(
                            rew_container,
                            text="img",
                            font=ctk.CTkFont(size=10)
                        )
                        rew_label.place(relx=0.5, rely=0.5, anchor="center")

                        if reward_img_url:
                            def load_reward_image(url, label):
                                try:
                                    import urllib.request
                                    from io import BytesIO
                                    from PIL import Image
                                    req = urllib.request.Request(
                                        url,
                                        headers={
                                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                            "Referer": "https://kick.com/"
                                        }
                                    )
                                    with urllib.request.urlopen(req, timeout=5) as response:
                                        img_data = response.read()
                                    rew_img = Image.open(BytesIO(img_data))
                                    rew_img = rew_img.resize((50, 50), Image.Resampling.LANCZOS)
                                    def update_ui():
                                        try:
                                            if label.winfo_exists():
                                                rew_photo = ctk.CTkImage(light_image=rew_img, dark_image=rew_img, size=(50, 50))
                                                label.configure(image=rew_photo, text="")
                                                label.image = rew_photo
                                        except: pass
                                    self.after(0, update_ui)
                                except Exception:
                                    pass
                            threading.Thread(target=load_reward_image, args=(reward_img_url, rew_label), daemon=True).start()
                            
                        # Add claimed checkmark overlay if claimed
                        if claimed:
                            claimed_overlay = ctk.CTkLabel(
                                rew_container,
                                text="✓",
                                font=ctk.CTkFont(size=18, family="Segoe UI", weight="bold"),
                                text_color="#10b981",
                                fg_color="transparent"
                            )
                            claimed_overlay.place(relx=0.85, rely=0.15, anchor="center")
    
                        # Add tooltip (drop name on hover) - on container for better functionality
                        self._create_tooltip(rew_container, tooltip_text)
                        self._create_tooltip(rew_label, tooltip_text)
                    except Exception:
                        pass
    
            # Participating channels - improved style
            channels_frame = ctk.CTkFrame(
                campaign_frame, fg_color="transparent"
            )
            channels_frame.grid(
                row=2, column=0, sticky="ew", padx=15, pady=(0, 12)
            )
            channels_frame.grid_columnconfigure(0, weight=1)
            
            # Store widget references (defined before if/else to avoid scope error)
            channel_buttons = []
    
            if not campaign["channels"]:
                # Global drop - show option to auto-find streamers
                global_drop_frame = ctk.CTkFrame(channels_frame, fg_color="transparent")
                global_drop_frame.grid(row=0, column=0, sticky="ew", pady=5)
                global_drop_frame.grid_columnconfigure(0, weight=1)
                
                no_channels_label = ctk.CTkLabel(
                    global_drop_frame,
                    text=self.app.t("drops_no_channels"),
                    text_color=("#6b7280", "#9ca3af"),
                    font=ctk.CTkFont(size=11, slant="italic"),
                )
                no_channels_label.grid(row=0, column=0, sticky="w")
                
                # Button to auto-find streamers for this game
                # Get category_id from campaign (from progress API or campaigns API)
                category = campaign.get("category", {})
                category_id = category.get("id") if isinstance(category, dict) else None
                
                # Also check in progress_data if category not found
                if not category_id:
                    progress_data = campaign.get("progress_data", {})
                    if isinstance(progress_data, dict):
                        progress_category = progress_data.get("category", {})
                        if isinstance(progress_category, dict):
                            category_id = progress_category.get("id")
                
                # Try alternative structure (if category is not nested)
                if not category_id:
                    category_id = campaign.get("category_id")
                
                # Always show button, but disable if no category_id
                def find_streamers(c=campaign, cid=category_id, sl=status_label):
                    if not cid:
                        if sl:
                            sl.configure(text="Hata: Bu kampanya için kategori kimliği bulunamadı")
                        debug_print(f"DEBUG: Campaign structure: {list(c.keys())}")
                        debug_print(f"DEBUG: Category: {c.get('category')}")
                        debug_print(f"DEBUG: Progress data: {c.get('progress_data', {}).get('category') if isinstance(c.get('progress_data'), dict) else 'N/A'}")
                        return
                    if sl:
                        self._auto_find_streamers_for_game(c, cid, scrollable_frame, sl)
                    else:
                        debug_print("DEBUG: No status_label available")
                
                find_btn = ctk.CTkButton(
                    global_drop_frame,
                    text="Canlı Yayıncı Bul",
                    width=180,
                    height=30,
                    font=ctk.CTkFont(size=11, weight="bold"),
                    fg_color=("#10b981", "#059669") if category_id else ("#6b7280", "#4b5563"),
                    hover_color=("#059669", "#047857") if category_id else ("#4b5563", "#374151"),
                    command=find_streamers,
                    state="normal" if category_id else "disabled",
                )
                find_btn.grid(row=0, column=1, padx=(10, 0), sticky="e")
                
                if not category_id:
                    debug_print(f"DEBUG: No category_id found for campaign {campaign.get('name', 'unknown')}")
                    debug_print(f"DEBUG: Campaign keys: {list(campaign.keys())}")
                    debug_print(f"DEBUG: Category value: {campaign.get('category')}")
            else:
                # Compact campaign action: no long channel selector.
                channel_row = ctk.CTkFrame(
                    channels_frame,
                    fg_color=("gray94", "#172033"),
                    corner_radius=10,
                )
                channel_row.grid(row=0, column=0, sticky="ew", pady=2)
                channel_row.grid_columnconfigure(0, weight=1)
                channels_list = campaign["channels"]
                channel_count = len(channels_list)
                campaign_id = campaign.get("id")
                channel_urls = [channel.get("url") for channel in channels_list]
                existing_count = self.app.config_data.campaign_item_count(
                    campaign_id,
                    channel_urls,
                )

                ctk.CTkLabel(
                    channel_row,
                    text=f"{channel_count} uygun kanal",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    anchor="w",
                ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 1))

                channel_hint = ctk.CTkLabel(
                    channel_row,
                    text=(
                        "Tüm kanallar izleme listesinde"
                        if existing_count == channel_count
                        else "Bot çevrimdışı kanallarda uygun alternatife otomatik geçer"
                    ),
                    font=ctk.CTkFont(size=10),
                    text_color=("gray42", "gray65"),
                    anchor="w",
                )
                channel_hint.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))

                add_all_btn = ctk.CTkButton(
                    channel_row,
                    text=(
                        self.app.t("btn_already_added")
                        if existing_count == channel_count
                        else "Kanalları Listeye Ekle"
                    ),
                    width=180,
                    height=34,
                    font=ctk.CTkFont(size=11, weight="bold"),
                    fg_color=("#64748b", "#475569") if existing_count == channel_count else ("#22c55e", "#16a34a"),
                    hover_color=("#475569", "#334155") if existing_count == channel_count else ("#16a34a", "#15803d"),
                    state="disabled" if existing_count == channel_count else "normal",
                )
                add_all_btn.grid(row=0, column=1, rowspan=2, sticky="e", padx=12, pady=10)
                self._campaign_action_widgets.append(
                    {
                        "campaign": campaign,
                        "button": add_all_btn,
                        "hint": channel_hint,
                    }
                )

                def add_campaign_channels(camp=campaign, sl=status_label, button=add_all_btn, hint=channel_hint):
                    added_count = self._add_all_campaign_channels(camp)
                    if sl:
                        self.inventory_subtitle.configure(
                            text=f"{added_count} kanal listeye eklendi"
                            if added_count
                            else "Bu kampanyadaki tüm kanallar zaten ekli"
                        )
                    self.refresh_queue_states()

                add_all_btn.configure(command=add_campaign_channels)
        except Exception as e:
            print(f"Error creating campaign display: {e}")
            import traceback
            traceback.print_exc()
    
    def refresh_queue_states(self):
        """Refresh campaign buttons after queue items are added or removed."""
        for entry in list(self._campaign_action_widgets):
            campaign = entry["campaign"]
            button = entry["button"]
            hint = entry["hint"]
            try:
                if not button.winfo_exists():
                    continue
                channels = campaign.get("channels", [])
                channel_urls = [
                    channel.get("url") if isinstance(channel, dict) else channel
                    for channel in channels
                ]
                existing_count = self.app.config_data.campaign_item_count(
                    campaign.get("id"),
                    channel_urls,
                )
                channel_count = len(channel_urls)
                all_added = channel_count > 0 and existing_count >= channel_count
                button.configure(
                    text=self.app.t("btn_already_added") if all_added else "Kanalları Listeye Ekle",
                    state="disabled" if all_added else "normal",
                    fg_color=("#64748b", "#475569") if all_added else ("#22c55e", "#16a34a"),
                    hover_color=("#475569", "#334155") if all_added else ("#16a34a", "#15803d"),
                )
                hint.configure(
                    text=(
                        "Tüm kanallar bu kampanya için listede"
                        if all_added
                        else (
                            f"{existing_count}/{channel_count} kanal bu kampanya için listede"
                            if existing_count
                            else "Bot çevrimdışı kanallarda uygun alternatife otomatik geçer"
                        )
                    )
                )
            except Exception:
                continue

    def _is_channel_in_list(self, url, campaign_id=None):
        """Check if a URL is already in the list"""
        return self.app.config_data.contains(url, campaign_id=campaign_id)
    
    def _add_all_campaign_channels(self, campaign):
        """Add all channels from a campaign with campaign grouping"""
        count = 0
        campaign_id = campaign.get("id")
        all_channels = campaign.get("channels", [])
        
        # Calculate max required time from rewards if campaign has rewards
        minutes = 120  # Default
        rewards = campaign.get("rewards", [])
        if rewards:
            max_required = 0
            for reward in rewards:
                required_units = reward.get("required_units", 0)
                if required_units > max_required:
                    max_required = required_units
            if max_required > 0:
                minutes = max_required
        
        # Get category_id from campaign
        required_category_id = None
        category = campaign.get("category", {})
        if isinstance(category, dict):
            required_category_id = category.get("id")
        else:
            # Try from progress_data
            progress_data = campaign.get("progress_data", {})
            if isinstance(progress_data, dict):
                progress_category = progress_data.get("category", {})
                if isinstance(progress_category, dict):
                    required_category_id = progress_category.get("id")
        
        existing_urls = {
            normalize_stream_url(item["url"])
            for item in self.app.config_data.items
            if str(item.get("campaign_id") or "") == str(campaign_id or "")
        }
        
        for channel in all_channels:
            try:
                url = channel.get("url") if isinstance(channel, dict) else channel
                normalized_url = normalize_stream_url(url)
                if normalized_url in existing_urls:
                    continue
                    
                # Store all channels as alternatives for each other
                campaign_channels = [
                    {"url": ch.get("url") if isinstance(ch, dict) else ch, 
                     "username": ch.get("username", "") if isinstance(ch, dict) else ""}
                    for ch in all_channels
                ]
                added = self.app.config_data.add(
                    url, 
                    minutes, 
                    campaign_id, 
                    campaign_channels,
                    required_category_id=required_category_id,
                    is_global_drop=False  # Regular drop, not global
                )
                if added:
                    count += 1
                    existing_urls.add(normalized_url)
            except Exception as e:
                channel_name = (
                    channel.get("username", "unknown")
                    if isinstance(channel, dict)
                    else str(channel)
                )
                print(f"Error adding channel {channel_name}: {e}")
    
        self.app.refresh_list()
        self.app.status_var.set(
            f"{campaign['name']} kampanyasından {count} kanal eklendi"
            if count
            else "Bu kampanyadaki tüm kanallar zaten ekli"
        )
        # Auto-start if enabled and queue not running
        if self.app.config_data.auto_start and not self.app.queue_running:
            self.after(500, self._auto_start_queue)
        return count
    
    def _create_tooltip(self, widget, text):
        """Create a tooltip that displays on widget hover"""
        tooltip = None
    
        def on_enter(event):
            nonlocal tooltip
            x = widget.winfo_rootx() + widget.winfo_width() // 2
            y = widget.winfo_rooty() - 10
    
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_attributes("-topmost", True)
            
            # Frame with shadow (modern effect)
            frame = tk.Frame(
                tooltip,
                background="#1f2937" if self.app.config_data.dark_mode else "#ffffff",
                relief="flat",
                borderwidth=0
            )
            frame.pack(padx=2, pady=2)
            
            label = tk.Label(
                frame,
                text=text,
                justify="center",
                background="#1f2937" if self.app.config_data.dark_mode else "#ffffff",
                foreground="#f9fafb" if self.app.config_data.dark_mode else "#111827",
                font=("Segoe UI", 12, "bold"),
                padx=12,
                pady=8,
            )
            label.pack()
            
            # Center tooltip above widget
            tooltip.update_idletasks()
            tooltip_width = tooltip.winfo_width()
            tooltip.wm_geometry(f"+{x - tooltip_width // 2}+{y - tooltip.winfo_height() - 10}")
    
        def on_leave(event):
            nonlocal tooltip
            if tooltip:
                tooltip.destroy()
                tooltip = None
    
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)
    
    # ----------- Toggles -----------
