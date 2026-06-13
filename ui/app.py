"""Main application UI for KickDropsMiner"""
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import customtkinter as ctk
from PIL import Image
from ui.components.settings_window import SettingsTab
from ui.components.drops_window import DropsTab


from core.autopilot import AutoPilot
from core import (
    BROWSER_MANAGER,
    Config,
    StreamWorker,
    CookieManager,
    active_browser_count,
    close_chrome_driver,
    make_chrome_driver,
    kick_live_status_by_api,
)
from utils.helpers import (
    APP_DIR,
    domain_from_url,
    cookie_file_for_domain,
    debug_print,
    set_debug_config
)
from utils.translations import translate, TRANSLATIONS


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Kick Drop Miner")
        self.geometry("1120x800")
        self.minsize(980, 720)

        self.config_data = Config()
        # Set global debug config reference
        set_debug_config(self.config_data)
        self.workers = {}
        self.autopilot = AutoPilot(self)
        self._interactive_driver = None  # Chrome pour capture de cookies
        self.queue_running = False
        self.queue_current_idx = None
        self._shutdown_event = threading.Event()
        self.browser_resource_var = tk.StringVar(value="Aktif tarayıcı: 0")
        self.last_verification_var = tk.StringVar(value="Son doğrulama: Bekleniyor")

        # Helper traduction
        def _t(key: str, **kwargs):
            return translate(self.config_data.language, key).format(**kwargs)

        self.t = _t

        # Appearance / theme
        ctk.set_appearance_mode("Dark" if self.config_data.dark_mode else "Light")
        ctk.set_default_color_theme("green")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # CTkTabview
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        
        self.tab_miner = self.tabview.add("Madenci")
        self.tab_inventory = self.tabview.add("Envanter")
        self.tab_settings = self.tabview.add("Ayarlar")
        
        # Setup Miner Tab layout
        self.tab_miner.grid_columnconfigure(1, weight=1)
        self.tab_miner.grid_rowconfigure(0, weight=1)
        
        # Content frame inside Miner Tab
        self.sidebar = ctk.CTkFrame(self.tab_miner, corner_radius=0, width=200)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_rowconfigure(99, weight=1)
        self._build_sidebar()
        
        self.content = ctk.CTkFrame(self.tab_miner, corner_radius=16)
        self.content.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.content.grid_rowconfigure(2, weight=3)
        self.content.grid_rowconfigure(3, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self._build_content()
        
        # Add Console Textbox at the bottom of Miner Tab content
        self.console_textbox = ctk.CTkTextbox(self.content, height=100, corner_radius=8, font=ctk.CTkFont(family="Consolas", size=12))
        self.console_textbox.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.console_textbox.insert("0.0", "KickDropsMiner Konsolu Başlatıldı...\n")
        self.console_textbox.configure(state="disabled")

        # Create a helper function to print to UI
        def ui_print(message):
            self.console_textbox.configure(state="normal")
            self.console_textbox.insert("end", f"{message}\n")
            self.console_textbox.see("end")
            self.console_textbox.configure(state="disabled")
            
        self.ui_print = ui_print
        
        # Build Inventory and Settings inside their tabs
        self.inventory_ui = DropsTab(self.tab_inventory, self)
        self.inventory_ui.pack(fill="both", expand=True)
        
        self.settings_ui = SettingsTab(self.tab_settings, self)
        self.settings_ui.pack(fill="both", expand=True)


        # Status bar
        self.status_var = tk.StringVar(value=self.t("status_ready"))
        self.status = ctk.CTkLabel(
            self, textvariable=self.status_var, anchor="w", height=32
        )
        self.status.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.refresh_list()
        
        # Start offline retry monitor
        self._start_offline_retry_monitor()
        
        # Auto-start queue if enabled
        if self.config_data.auto_start and self.config_data.items:
            # Delay slightly to let UI finish loading
            self.after(1000, self._auto_start_queue)
        
        # Properly close all browsers when closing the app
        try:
            self.protocol("WM_DELETE_WINDOW", self.on_close)
        except Exception:
            pass
        self.after(500, self._refresh_resource_status)

    def _available_languages(self):
        codes = list(TRANSLATIONS.keys())
        ordered = []
        for preferred in ("fr", "en"):
            if preferred in codes:
                ordered.append(preferred)
        for code in sorted(c for c in codes if c not in ordered):
            ordered.append(code)
        return ordered

    def _language_label(self, lang_code):
        label_key = f"language_{lang_code}"
        label = translate(self.config_data.language, label_key)
        if label == label_key:
            label = translate(lang_code, label_key)
        if label == label_key:
            label = lang_code
        return label

    def _get_language_choices(self):
        codes = self._available_languages()
        if self.config_data.language not in codes and codes:
            self.config_data.language = codes[0]
            self.config_data.save()
        labels = {code: self._language_label(code) for code in codes}
        self.lang_display_to_code = {label: code for code, label in labels.items()}
        return [labels[code] for code in codes]

    def _refresh_resource_status(self):
        if self._shutdown_event.is_set():
            return
        try:
            self.browser_resource_var.set(
                f"Aktif tarayıcı: {active_browser_count()}"
            )
        except Exception:
            pass
        self.after(1000, self._refresh_resource_status)

    # ----------- UI construction -----------
    def _build_sidebar(self):
        self.sidebar.configure(fg_color=("#f5f7fb", "#0f172a"))
        header = ctk.CTkFrame(self.sidebar, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="w")
        header.grid_columnconfigure(1, weight=1)

        # Logo
        try:
            logo_path = os.path.join(APP_DIR, "assets", "logo.png")
            img = Image.open(logo_path)
            self._logo_img = ctk.CTkImage(light_image=img, dark_image=img, size=(24, 24))
            logo_lbl = ctk.CTkLabel(header, image=self._logo_img, text="")
            logo_lbl.grid(row=0, column=0, padx=(4, 6), pady=4, sticky="w")
        except Exception:
            pass

        title = ctk.CTkLabel(
            header, text="Kick Drop Miner", font=ctk.CTkFont(size=22, family="Segoe UI", weight="bold")
        )
        title.grid(row=0, column=1, padx=0, pady=4, sticky="w")

        ctk.CTkLabel(
            self.sidebar,
            text="OTOMASYON",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("gray45", "gray60"),
        ).grid(row=1, column=0, padx=16, pady=(22, 4), sticky="w")

        self.btn_autopilot = ctk.CTkButton(
            self.sidebar,
            text="OTO-PİLOTU BAŞLAT",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=46,
            width=180,
            corner_radius=12,
            fg_color="#22c55e",
            hover_color="#16a34a",
            command=self.toggle_autopilot
        )
        self.btn_autopilot.grid(row=2, column=0, padx=14, pady=(0, 12), sticky="w")

        btn_stop = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_stop_sel"),
            command=self.stop_selected,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            width=180,
            height=38,
            corner_radius=10,
        )
        btn_stop.grid(row=3, column=0, padx=14, pady=5, sticky="w")

        btn_login = ctk.CTkButton(
            self.sidebar,
            text="Kick'e Giriş Yap",
            command=self.manual_login,
            fg_color="#3498db",
            hover_color="#2980b9",
            width=180,
            height=38,
            corner_radius=10,
        )
        btn_login.grid(row=4, column=0, padx=14, pady=5, sticky="w")

        self.sidebar_status = ctk.CTkLabel(
            self.sidebar,
            text="Sistem hazır",
            font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray65"),
            wraplength=170,
            justify="left",
        )
        self.sidebar_status.grid(row=5, column=0, padx=16, pady=(16, 8), sticky="w")

        ctk.CTkLabel(
            self.sidebar,
            textvariable=self.browser_resource_var,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#2563eb", "#60a5fa"),
        ).grid(row=6, column=0, padx=16, pady=(4, 2), sticky="w")

        ctk.CTkLabel(
            self.sidebar,
            textvariable=self.last_verification_var,
            font=ctk.CTkFont(size=10),
            text_color=("gray45", "gray65"),
            wraplength=170,
            justify="left",
        ).grid(row=7, column=0, padx=16, pady=(2, 8), sticky="w")

        # Initialize toggle variables
        self.mute_var = tk.BooleanVar(value=bool(self.config_data.mute))
        self.hide_player_var = tk.BooleanVar(value=bool(self.config_data.hide_player))
        self.mini_player_var = tk.BooleanVar(value=bool(self.config_data.mini_player))
        self.force_160p_var = tk.BooleanVar(value=bool(self.config_data.force_160p))
        self.auto_start_var = tk.BooleanVar(value=bool(self.config_data.auto_start))
        self.theme_var = tk.StringVar(
            value=self.t("theme_dark") if self.config_data.dark_mode else self.t("theme_light")
        )
        language_choices = self._get_language_choices()
        current_label = self._language_label(self.config_data.language)
        if current_label not in language_choices and language_choices:
            current_label = language_choices[0]
        self.lang_var = tk.StringVar(value=current_label)

    def toggle_autopilot(self):
        if not self.autopilot.running:
            self.autopilot.start()
            self.btn_autopilot.configure(text="OTO-PİLOTU DURDUR", fg_color="#dc2626", hover_color="#b91c1c")
            self.sidebar_status.configure(text="Oto-Pilot kampanyaları tarıyor")
        else:
            self.autopilot.stop()
            self.queue_running = False
            self.queue_current_idx = None
            self._stop_all_workers(reason="user_stopped")
            self.btn_autopilot.configure(text="OTO-PİLOTU BAŞLAT", fg_color="#22c55e", hover_color="#16a34a")
            self.sidebar_status.configure(text="Sistem hazır")

    def manual_login(self):
        self.ui_print("Manuel Giriş: Tarayıcı açılıyor. Kick.com'a giriş yapıp tarayıcıyı kapatın.")
        def _login_thread():
            from core.browser import make_chrome_driver, CookieManager
            driver = None
            try:
                driver = make_chrome_driver(
                    headless=False,
                    profile_dir_name=f"manual_login_{os.getpid()}",
                    role="login",
                )
                self._interactive_driver = driver
                driver.get("https://kick.com/")
                while not self._shutdown_event.wait(2):
                    try:
                        driver.title
                        CookieManager.save_cookies(driver, "kick.com")
                    except Exception:
                        break
                self.after(
                    0,
                    lambda: self.ui_print(
                        "Manuel Giriş: Tarayıcı kapatıldı, giriş bilgisi kaydedildi."
                    ),
                )
            except Exception as error:
                self.after(
                    0,
                    lambda value=str(error): self.ui_print(
                        f"Manuel giriş tarayıcısı açılamadı: {value}"
                    ),
                )
            finally:
                if driver is not None:
                    close_chrome_driver(driver)
                self._interactive_driver = None
        import threading
        threading.Thread(target=_login_thread, daemon=True).start()

    def _build_content(self):
        header = ctk.CTkFrame(
            self.content,
            corner_radius=16,
            fg_color=("white", "#111827"),
            border_width=1,
            border_color=("gray82", "#273449"),
        )
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text=self.t("title_streams"),
            font=ctk.CTkFont(size=22, family="Segoe UI", weight="bold"),
        )
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))

        self.queue_summary_var = tk.StringVar(value="0 yayın • Kuyruk beklemede")
        summary = ctk.CTkLabel(
            header,
            textvariable=self.queue_summary_var,
            font=ctk.CTkFont(size=12),
            text_color=("gray42", "gray65"),
        )
        summary.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        clear_btn = ctk.CTkButton(
            header,
            text="Yayın Listesini Sıfırla",
            width=170,
            fg_color="#b91c1c",
            hover_color="#991b1b",
            command=self.clear_all_items,
        )
        clear_btn.grid(row=0, column=1, rowspan=2, sticky="e", padx=14, pady=12)

        actions = ctk.CTkFrame(self.content, corner_radius=14, fg_color=("gray94", "#172033"))
        actions.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        for column in range(4):
            actions.grid_columnconfigure(column, weight=1)

        action_specs = (
            ("Yayın Ekle", self.add_link, "#2563eb", "#1d4ed8"),
            ("Seçileni Başlat", self.start_selected, "#7c3aed", "#6d28d9"),
            ("Sırayı Başlat", self.start_all_in_order, "#16a34a", "#15803d"),
            ("Seçileni Kaldır", self.remove_selected, "#475569", "#334155"),
        )
        for column, (text, command, color, hover) in enumerate(action_specs):
            ctk.CTkButton(
                actions,
                text=text,
                command=command,
                height=38,
                corner_radius=10,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=color,
                hover_color=hover,
            ).grid(row=0, column=column, sticky="ew", padx=6, pady=8)

        # Tableau (ttk.Treeview) dans un CTkFrame
        table_frame = ctk.CTkFrame(self.content, corner_radius=16)
        table_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 10))
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        # Automatic light/dark theme
        if ctk.get_appearance_mode() == "Dark":
            style.theme_use("clam")
            style.configure(
                "Treeview",
                background="#1f2125",
                fieldbackground="#1f2125",
                foreground="#e6e6e6",
                rowheight=42,
                bordercolor="#2b2d31",
            )
            style.configure(
                "Treeview.Heading",
                background="#2b2d31",
                foreground="#e6e6e6",
                font=("Segoe UI", 12, "bold"),
            )
            sel_bg = "#3b82f6"
            style.map(
                "Treeview",
                background=[("selected", sel_bg)],
                foreground=[("selected", "white")],
            )
        else:
            style.theme_use("clam")
            style.configure(
                "Treeview",
                background="#ffffff",
                fieldbackground="#ffffff",
                foreground="#111111",
                rowheight=42,
                bordercolor="#e9ecef",
            )
            style.configure(
                "Treeview.Heading",
                background="#eef2f7",
                foreground="#111111",
                font=("Segoe UI", 12, "bold"),
            )
            sel_bg = "#2d8cff"
            style.map(
                "Treeview",
                background=[("selected", sel_bg)],
                foreground=[("selected", "white")],
            )

        self.tree = ttk.Treeview(
            table_frame,
            columns=("url", "minutes", "elapsed", "progress"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("url", text="URL")
        self.tree.heading("minutes", text=self.t("col_minutes"))
        self.tree.heading("elapsed", text=self.t("col_elapsed"))
        self.tree.heading("progress", text="İlerleme (%)")
        self.tree.column("url", width=550, anchor="w")
        self.tree.column("minutes", width=100, anchor="center")
        self.tree.column("elapsed", width=120, anchor="center")
        self.tree.column("progress", width=100, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky="ns")
        
        # Bind double-click to edit minutes
        self.tree.bind("<Double-Button-1>", self.on_tree_double_click)

        # Colored rows via tags
        try:
            self.tree.tag_configure(
                "odd",
                background="#0f0f11"
                if ctk.get_appearance_mode() == "Dark"
                else "#f7f7f7",
            )
            self.tree.tag_configure(
                "even",
                background="#1f2125"
                if ctk.get_appearance_mode() == "Dark"
                else "#ffffff",
            )
            self.tree.tag_configure(
                "redo",
                background="#3a3a00"
                if ctk.get_appearance_mode() == "Dark"
                else "#fff3cd",
            )
            self.tree.tag_configure(
                "paused",
                background="#3a2e2a"
                if ctk.get_appearance_mode() == "Dark"
                else "#fde2e2",
            )
            self.tree.tag_configure(
                "finished",
                background="#22352a"
                if ctk.get_appearance_mode() == "Dark"
                else "#e6f7e8",
            )
        except Exception:
            pass

    # ----------- Theme -----------
    def change_theme(self, choice):
        # Accepts FR/EN
        dark = choice in (self.t("theme_dark"), "Sombre", "Dark")
        self.config_data.dark_mode = dark
        self.config_data.save()
        ctk.set_appearance_mode("Dark" if dark else "Light")
        # Rebuild content (to recalculate Treeview styles)
        for w in self.content.winfo_children():
            w.destroy()
        self._build_content()
        self.refresh_list()

    # ----------- Language -----------
    def change_language(self, choice):
        mapping = getattr(self, "lang_display_to_code", {})
        new_lang = None

        if isinstance(choice, str):
            new_lang = mapping.get(choice)
            if not new_lang:
                # Fallback: case-insensitive match
                for label, code in mapping.items():
                    if label.lower() == choice.lower():
                        new_lang = code
                        break

        if not new_lang:
            return

        if new_lang == self.config_data.language:
            return  # No change needed

        self.config_data.language = new_lang
        self.config_data.save()

        # Rebuild sidebar & content to refresh text
        try:
            for w in self.sidebar.winfo_children():
                w.destroy()
            self._build_sidebar()
        except Exception:
            pass

        try:
            for w in self.content.winfo_children():
                w.destroy()
            self._build_content()
        except Exception:
            pass

        # Update status bar if it's at the initial text
        try:
            ready_variants = [translate(lang, "status_ready") for lang in TRANSLATIONS]
            if self.status_var.get() in ready_variants:
                self.status_var.set(self.t("status_ready"))
        except Exception:
            pass

    # ----------- Actions -----------
    def on_tree_double_click(self, event):
        """Handle double-click on tree to edit minutes"""
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        
        column = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        
        if not row_id:
            return
        
        # Check if clicked on minutes column (column #2)
        if column == "#2":
            idx = int(row_id)
            if idx >= len(self.config_data.items):
                return
            
            # Check if this stream is currently running
            if idx in self.workers:
                messagebox.showwarning(
                    self.t("warning"),
                    self.t("cannot_edit_active_stream")
                )
                return
                
            current_minutes = self.config_data.items[idx]["minutes"]
            
            new_minutes = simpledialog.askinteger(
                self.t("prompt_minutes_title"),
                self.t("prompt_minutes_msg"),
                initialvalue=current_minutes,
                minvalue=0
            )
            
            if new_minutes is not None:
                self.config_data.items[idx]["minutes"] = new_minutes
                self.config_data.save()
                self.refresh_list()
                self.status_var.set(f"Hedef {new_minutes} dakika olarak güncellendi")
    
    def refresh_list(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        for i, item in enumerate(self.config_data.items):
            elapsed = self.workers[i].elapsed_seconds if i in self.workers else 0
            progress = item.get("progress", 0)
            tags = ["odd" if i % 2 else "even"]
            if item.get("finished"):
                tags.append("finished")
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(item["url"], item["minutes"], f"{elapsed}s", f"%{progress}"),
                tags=tuple(tags),
            )
        self._update_queue_summary()
        inventory_ui = getattr(self, "inventory_ui", None)
        if inventory_ui and hasattr(inventory_ui, "refresh_queue_states"):
            inventory_ui.refresh_queue_states()

    def _update_queue_summary(self):
        total = len(self.config_data.items)
        finished = sum(1 for item in self.config_data.items if item.get("finished"))
        running = len(self.workers)
        if hasattr(self, "queue_summary_var"):
            if running:
                summary = f"{total} yayın • {running} yayın çalışıyor • {finished} tamamlandı"
            elif total:
                summary = f"{total} yayın • {finished} tamamlandı • {total - finished} bekliyor"
            else:
                summary = "Henüz yayın eklenmedi • Envanterden veya Yayın Ekle ile başlayın"
            self.queue_summary_var.set(summary)
        if hasattr(self, "sidebar_status"):
            self.sidebar_status.configure(
                text="Yayın izleniyor" if running else ("Kuyruk hazır" if total else "Sistem hazır")
            )

    def add_link(self):
        url = simpledialog.askstring(
            self.t("prompt_live_url_title"), self.t("prompt_live_url_msg")
        )
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        minutes = simpledialog.askinteger(
            self.t("prompt_minutes_title"), self.t("prompt_minutes_msg"), minvalue=0
        )
        if not self.config_data.add(url, minutes or 0):
            self.status_var.set(self.t("status_link_already_added"))
            messagebox.showinfo(self.t("warning"), self.t("status_link_already_added"))
            return
        self.refresh_list()
        self.status_var.set(self.t("status_link_added"))
        # Auto-start if enabled and queue not running
        if self.config_data.auto_start and not self.queue_running:
            self.after(500, self._auto_start_queue)

    def on_remove_button_click(self, event):
        """Handle remove button click - check for Ctrl key"""
        # Check if Ctrl key is pressed (state & 0x4 is Control modifier)
        ctrl_pressed = (event.state & 0x4) != 0
        
        if ctrl_pressed:
            # Ctrl is pressed - show clear all dialog
            self.after(0, self.clear_all_items)
        else:
            # Normal remove action
            self.after(0, self.remove_selected)

    def _stop_worker(self, idx, reason="user_stopped", timeout=0.5):
        worker = self.workers.get(idx)
        if worker is None:
            return True
        try:
            worker.stop(reason)
            worker.join(timeout=timeout)
            if worker.is_alive():
                worker.force_close_driver()
                worker.join(timeout=0.5)
        finally:
            self.workers.pop(idx, None)
        return not worker.is_alive()

    def _stop_all_workers(self, reason="user_stopped"):
        results = []
        for idx in list(self.workers):
            results.append(self._stop_worker(idx, reason=reason))
        self._update_queue_summary()
        return all(results) if results else True
    
    def clear_all_items(self):
        """Clear all items from the list after confirmation"""
        if not self.config_data.items:
            return  # Nothing to clear
        
        # Show confirmation dialog
        result = messagebox.askyesno(
            self.t("clear_list_title"),
            self.t("clear_list_confirm", count=len(self.config_data.items)),
            icon="warning"
        )
        
        if result:
            self.queue_running = False
            self.queue_current_idx = None
            self._stop_all_workers(reason="list_cleared")
            self.config_data.clear()
            
            # Refresh UI
            self.refresh_list()
            self.status_var.set(self.t("clear_list_done"))
            debug_print("DEBUG: Yayın listesi temizlendi")
    
    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        if self.workers:
            messagebox.showwarning(
                self.t("warning"),
                self.t("cannot_remove_while_running"),
            )
            return
        idx = int(sel[0])
        self.config_data.remove(idx)
        self.refresh_list()
        self.status_var.set(self.t("status_link_removed"))

    def start_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        
        self.queue_running = False
        self.queue_current_idx = None
        if self.workers:
            for running_idx in list(self.workers):
                if running_idx < len(self.config_data.items):
                    self.config_data.items[running_idx]["finished"] = False
            self._stop_all_workers(reason="replaced")

        # Start queue logic from this item
        self.queue_running = True
        self.queue_current_idx = None
        self._run_queue_from(idx)

    def _start_index(self, idx):
        """Start a stream, ensuring only one runs at a time (Kick limitation)"""
        if self.workers:
            self.queue_current_idx = None
            for running_idx in list(self.workers):
                if running_idx < len(self.config_data.items):
                    self.config_data.items[running_idx]["finished"] = False
            self._stop_all_workers(reason="replaced")
        
        item = self.config_data.items[idx]
        
        # A public API failure is "unknown", not proof that a stream is live.
        public_live_status = kick_live_status_by_api(item["url"])
        if public_live_status is False:
            campaign_channels = item.get("campaign_channels", [])
            if campaign_channels:
                tried_channels = item.get("tried_channels", [])
                current_url = item["url"]
                
                # Add current URL to tried list if not already there
                if current_url not in tried_channels:
                    tried_channels.append(current_url)
                
                # Get all channel URLs
                all_channel_urls = []
                for ch in campaign_channels:
                    ch_url = ch.get("url") if isinstance(ch, dict) else ch
                    if ch_url:
                        all_channel_urls.append(ch_url)
                if current_url not in all_channel_urls:
                    all_channel_urls.append(current_url)
                
                # Reset if all channels tried
                if len(tried_channels) >= len(all_channel_urls):
                    tried_channels.clear()
                    debug_print(f"DEBUG: Reset tried_channels in _start_index for campaign {item.get('campaign_id')}")
                
                # Try to find a live alternative channel that hasn't been tried
                switched_in_start = False
                for alt_channel in campaign_channels:
                    alt_url = alt_channel.get("url") if isinstance(alt_channel, dict) else alt_channel
                    if alt_url and alt_url != item["url"] and alt_url not in tried_channels:
                        if kick_live_status_by_api(alt_url) is not False:
                            # Switch to this alternative channel
                            self.config_data.items[idx]["url"] = alt_url
                            tried_channels.append(alt_url)
                            item["tried_channels"] = tried_channels
                            self.config_data.save()
                            self.refresh_list()
                            item = self.config_data.items[idx]  # Update item reference
                            debug_print(f"DEBUG: Switched to alternative in _start_index: {alt_url} (tried: {len(tried_channels)}/{len(all_channel_urls)})")
                            self.status_var.set(f"{alt_url.split('/')[-1]} kanalına geçildi, sayfa yükleniyor...")
                            switched_in_start = True
                            # Wait 8 seconds to allow browser to fully load before checking if stream is live
                            # Use after() to avoid blocking UI thread
                            self.after(8000, lambda i=idx: self._start_index_after_switch(i))
                            return
                
                # If we switched, we already scheduled a callback, so return early
                if switched_in_start:
                    return
        
        # Check again after potential channel switch
        if kick_live_status_by_api(item["url"]) is False:
            try:
                values = list(self.tree.item(str(idx), "values"))
                values[2] = self.t("retry")
                self.tree.item(str(idx), values=values, tags=("redo",))
            except Exception:
                pass
            self.status_var.set(self.t("offline_wait_retry", url=item["url"]))
            return

        domain = domain_from_url(item["url"])
        if not domain:
            messagebox.showerror(self.t("error"), self.t("invalid_url"))
            return

        cookie_path = cookie_file_for_domain(domain)
        if not os.path.exists(cookie_path):
            is_auto = self.config_data.auto_start or (getattr(self, "autopilot", None) and self.autopilot.running)
            # Auto-import cookies silently (no popup for automation)
            try:
                if not CookieManager.import_from_browser(domain):
                    if not is_auto:
                        if messagebox.askyesno(
                            self.t("cookies_missing_title"), self.t("cookies_missing_msg")
                        ):
                            self.obtain_cookies_interactively(item["url"], domain)
                    else:
                        self.status_var.set(f"{item['url']} atlandı: çerez bulunamadı")
                        self.ui_print(f"⚠️ {item['url']} izlenemiyor: Çerez bulunamadı! 'Manuel Giriş' yapın.")
                        return
            except Exception:
                if not is_auto:
                    if messagebox.askyesno(
                        self.t("cookies_missing_title"), self.t("cookies_missing_msg")
                    ):
                        self.obtain_cookies_interactively(item["url"], domain)
                else:
                    self.ui_print(f"⚠️ {item['url']} izlenemiyor: Çerez bulunamadı! 'Manuel Giriş' yapın.")
                    return

        stop_event = threading.Event()
        
        worker = StreamWorker(
            item["url"],
            item["minutes"],
            on_update=lambda payload: self.on_worker_update(idx, payload),
            on_finish=lambda elapsed, completed, reason: self.on_worker_finish(
                idx, elapsed, completed, reason
            ),
            stop_event=stop_event,
            driver_path=self.config_data.chromedriver_path,
            extension_path=self.config_data.extension_path,
            hide_player=bool(self.hide_player_var.get()),
            mute=bool(self.mute_var.get()),
            mini_player=bool(self.mini_player_var.get()),
            force_160p=bool(self.config_data.force_160p),
            required_category_id=item.get("required_category_id"),
            campaign_id=item.get("campaign_id"),
        )
        self.workers[idx] = worker
        worker.start()
        self._update_queue_summary()
        self.tree.selection_set(str(idx))
        self.status_var.set(self.t("status_playing", url=item["url"]))

    def _start_index_after_switch(self, idx):
        """Continue _start_index after a delay when switching channels"""
        if idx < 0 or idx >= len(self.config_data.items):
            return
        
        item = self.config_data.items[idx]
        
        # Check again after potential channel switch (after delay)
        if kick_live_status_by_api(item["url"]) is False:
            try:
                values = list(self.tree.item(str(idx), "values"))
                values[2] = self.t("retry")
                self.tree.item(str(idx), values=values, tags=("redo",))
            except Exception:
                pass
            self.status_var.set(self.t("offline_wait_retry", url=item["url"]))
            return

        domain = domain_from_url(item["url"])
        if not domain:
            messagebox.showerror(self.t("error"), self.t("invalid_url"))
            return

        cookie_path = cookie_file_for_domain(domain)
        if not os.path.exists(cookie_path):
            # Auto-import cookies silently (no popup for automation)
            try:
                if not CookieManager.import_from_browser(domain):
                    # Only show popup if auto-import fails and we're not in auto mode
                    if not self.config_data.auto_start:
                        if messagebox.askyesno(
                            self.t("cookies_missing_title"), self.t("cookies_missing_msg")
                        ):
                            self.obtain_cookies_interactively(item["url"], domain)
                    else:
                        # In auto mode, skip items without cookies
                        self.status_var.set(f"{item['url']} atlandı: çerez bulunamadı")
                        return
            except Exception:
                if not self.config_data.auto_start:
                    if messagebox.askyesno(
                        self.t("cookies_missing_title"), self.t("cookies_missing_msg")
                    ):
                        self.obtain_cookies_interactively(item["url"], domain)
                else:
                    return

        stop_event = threading.Event()
        
        worker = StreamWorker(
            item["url"],
            item["minutes"],
            on_update=lambda payload: self.on_worker_update(idx, payload),
            on_finish=lambda elapsed, completed, reason: self.on_worker_finish(
                idx, elapsed, completed, reason
            ),
            stop_event=stop_event,
            driver_path=self.config_data.chromedriver_path,
            extension_path=self.config_data.extension_path,
            hide_player=bool(self.hide_player_var.get()),
            mute=bool(self.mute_var.get()),
            mini_player=bool(self.mini_player_var.get()),
            force_160p=bool(self.config_data.force_160p),
            required_category_id=item.get("required_category_id"),
            campaign_id=item.get("campaign_id"),
        )
        self.workers[idx] = worker
        worker.start()
        self._update_queue_summary()
        self.tree.selection_set(str(idx))
        self.status_var.set(self.t("status_playing", url=item["url"]))

    def start_all_in_order(self):
        self.queue_running = True
        self.queue_current_idx = None
        self._run_queue_from(0)

    def _run_queue_from(self, start_idx: int):
        """Run queue ensuring only one stream at a time"""
        # Ensure no other streams are running
        if len(self.workers) > 0:
            # Wait for current stream to finish
            return
        
        for i in range(start_idx, len(self.config_data.items)):
            item = self.config_data.items[i]
            if item.get("finished"):
                continue
            self.tree.selection_set(str(i))
            self._start_index(i)
            after = set(self.workers.keys())
            if i in after:
                self.queue_current_idx = i
                self.status_var.set(self.t("queue_running_status", url=item["url"]))
                return  # Only one stream at a time
        self.queue_running = False
        self.queue_current_idx = None
        self.status_var.set(self.t("queue_finished_status"))

    def stop_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx in self.workers:
            self.queue_running = False
            self.queue_current_idx = None
            self._stop_worker(idx, reason="user_stopped")
            self.status_var.set(self.t("status_stopped"))
            # Update the display
            if str(idx) in self.tree.get_children():
                values = list(self.tree.item(str(idx), "values"))
                values[2] = f"{values[2]} ({self.t('tag_stop')})"
                self.tree.item(str(idx), values=values)

    def obtain_cookies_interactively(self, url, domain):
        try:
            drv = make_chrome_driver(
                headless=False,
                driver_path=self.config_data.chromedriver_path,
                extension_path=self.config_data.extension_path,
            )
            self._interactive_driver = drv
        except Exception as e:
            messagebox.showerror(self.t("error"), self.t("chrome_start_fail", e=e))
            return
        drv.get(url)
        messagebox.showinfo(self.t("action_required"), self.t("sign_in_and_click_ok"))
        try:
            CookieManager.save_cookies(drv, domain)
            messagebox.showinfo(
                self.t("ok"), self.t("cookies_saved_for", domain=domain)
            )
        except Exception as e:
            messagebox.showerror(self.t("error"), self.t("cannot_save_cookies", e=e))
        finally:
            close_chrome_driver(drv)
            self._interactive_driver = None

    def on_close(self):
        self._shutdown_event.set()
        self.queue_running = False
        self.queue_current_idx = None
        try:
            self.autopilot.stop()
        except Exception:
            pass
        self._stop_all_workers(reason="app_closing")
        if self._interactive_driver is not None:
            close_chrome_driver(self._interactive_driver)
            self._interactive_driver = None
        BROWSER_MANAGER.close_all()
        try:
            self.destroy()
        except Exception:
            os._exit(0)

    def choose_chromedriver(self):
        path = filedialog.askopenfilename(
            title=self.t("pick_chromedriver_title"),
            filetypes=[(self.t("executables_filter"), "*.exe;*")],
        )
        if not path:
            return
        self.config_data.chromedriver_path = path
        self.config_data.save()
        messagebox.showinfo(self.t("ok"), self.t("chromedriver_set", path=path))

    def choose_extension(self):
        path = filedialog.askopenfilename(
            title=self.t("pick_extension_title"),
            filetypes=[("CRX", "*.crx"), (self.t("all_files_filter"), "*.*")],
        )
        if not path:
            return
        self.config_data.extension_path = path
        self.config_data.save()
        messagebox.showinfo(self.t("ok"), self.t("extension_set", path=path))

    def on_toggle_mute(self):
        self.config_data.mute = bool(self.mute_var.get())
        self.config_data.save()
        for w in list(self.workers.values()):
            try:
                w.mute = self.config_data.mute
                w.ensure_player_state()
            except Exception:
                pass

    def on_toggle_hide(self):
        self.config_data.hide_player = bool(self.hide_player_var.get())
        self.config_data.save()
        for w in list(self.workers.values()):
            try:
                w.hide_player = self.config_data.hide_player
                w.ensure_player_state()
            except Exception:
                pass

    def on_toggle_mini(self):
        self.config_data.mini_player = bool(self.mini_player_var.get())
        self.config_data.save()
        for w in list(self.workers.values()):
            try:
                w.mini_player = self.config_data.mini_player
                w.ensure_player_state()
            except Exception:
                pass

    def on_toggle_force_160p(self):
        self.config_data.force_160p = bool(self.force_160p_var.get())
        self.config_data.save()
        # Note: force_160p only affects new streams (set during initialization)
        # Existing streams will need to be restarted to apply the change

    def on_toggle_auto_start(self):
        self.config_data.auto_start = bool(self.auto_start_var.get())
        self.config_data.save()
        if self.config_data.auto_start and not self.queue_running:
            # Auto-start if enabled and queue not running
            if self.config_data.items:
                self.start_all_in_order()
    

    def _auto_start_queue(self):
        """Auto-start queue on launch if enabled"""
        if not self.queue_running and self.config_data.items:
            # Check if there are any unfinished items
            unfinished = [i for i, item in enumerate(self.config_data.items) 
                         if not item.get("finished")]
            if unfinished:
                self.start_all_in_order()

    def _start_offline_retry_monitor(self):
        """Background thread that periodically checks offline streams and retries them"""
        def monitor():
            while not self._shutdown_event.wait(30):
                try:
                    if not self.queue_running:
                        continue
                    
                    # Only check if we're not currently running a stream
                    # (Kick only allows 1 stream at a time)
                    if len(self.workers) > 0:
                        continue
                    
                    # Find next unfinished item
                    for idx, item in enumerate(self.config_data.items):
                        if item.get("finished"):
                            continue
                        
                        if idx in self.workers:
                            continue  # Already running
                        
                        # Check if stream is now live
                        if kick_live_status_by_api(item["url"]) is not False:
                            # Stream is back online, retry it
                            self.after(0, lambda i=idx: self._start_index(i))
                            break  # Only start one at a time
                except Exception as e:
                    print(f"Monitor error: {e}")
                    if self._shutdown_event.wait(60):
                        break
        
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()

    # ----------- Callbacks Worker -----------
    def on_worker_update(self, idx, payload):
        def ui_update():
            if idx < 0 or idx >= len(self.config_data.items):
                return

            item = self.config_data.items[idx]
            seconds = int(payload.get("elapsed_seconds", 0) or 0)
            state = payload.get("state", "verifying")
            state_text = self.t(f"worker_state_{state}")
            if state_text == f"worker_state_{state}":
                state_text = state
            is_global_drop = item.get("is_global_drop", False)

            progress = payload.get("drop_progress")
            if progress is not None:
                item["progress"] = round(float(progress), 2)

            if str(idx) in self.tree.get_children():
                values = list(self.tree.item(str(idx), "values"))
                if is_global_drop:
                    cumulative_seconds = item.get("cumulative_time", 0) + seconds
                    cumulative_minutes = cumulative_seconds // 60
                    values[2] = f"{cumulative_minutes}m ({state_text})"
                else:
                    values[2] = f"{seconds}s ({state_text})"
                if progress is not None and len(values) >= 4:
                    values[3] = f"%{float(progress):.1f}"

                current_tags = set(self.tree.item(str(idx), "tags") or [])
                if state in ("watch_verified", "drop_verified"):
                    current_tags.discard("paused")
                else:
                    current_tags.add("paused")
                self.tree.item(str(idx), values=values, tags=tuple(current_tags))

            if is_global_drop:
                cumulative_seconds = item.get("cumulative_time", 0) + seconds
                cumulative_minutes = cumulative_seconds // 60
                secs = cumulative_seconds % 60
                time_str = f"{cumulative_minutes}m {secs}s" if cumulative_minutes > 0 else f"{secs}s"
            else:
                minutes = seconds // 60
                secs = seconds % 60
                time_str = f"{minutes}m {secs}s" if minutes > 0 else f"{secs}s"

            prefix = (
                self.t("queue_running_status", url=item["url"])
                if self.queue_running and self.queue_current_idx == idx
                else self.t("status_playing", url=item["url"])
            )
            self.status_var.set(f"{prefix} - {time_str} ({state_text})")
            self.last_verification_var.set(f"Son doğrulama: {state_text}")
            try:
                self.sidebar_status.configure(text=state_text)
            except Exception:
                pass

        self.after(0, ui_update)

    def on_worker_finish(self, idx, elapsed, completed, reason):
        def ui_finish():
            self.workers.pop(idx, None)
            if idx < 0 or idx >= len(self.config_data.items):
                self._update_queue_summary()
                return

            item = self.config_data.items[idx]
            campaign_id = item.get("campaign_id")
            is_global_drop = item.get("is_global_drop", False)

            if is_global_drop and campaign_id and elapsed:
                campaign_items = [
                    other
                    for other in self.config_data.items
                    if other.get("campaign_id") == campaign_id
                ]
                previous_total = max(
                    (other.get("cumulative_time", 0) for other in campaign_items),
                    default=0,
                )
                new_total = previous_total + elapsed
                for other in campaign_items:
                    other["cumulative_time"] = new_total

            if completed:
                targets = (
                    [
                        other
                        for other in self.config_data.items
                        if other.get("campaign_id") == campaign_id
                    ]
                    if campaign_id
                    else [item]
                )
                for target in targets:
                    target["finished"] = True
                    target["tried_channels"] = []
                    target["progress"] = 100
                self.config_data.save()
                self.last_verification_var.set("Son doğrulama: Drop Doğrulandı")
                if str(idx) in self.tree.get_children():
                    values = list(self.tree.item(str(idx), "values"))
                    values[2] = f"{elapsed}s ({self.t('tag_finished')})"
                    if len(values) >= 4:
                        values[3] = "%100"
                    tags = set(self.tree.item(str(idx), "tags") or [])
                    tags.update(("finished",))
                    tags.discard("paused")
                    tags.discard("redo")
                    self.tree.item(str(idx), values=values, tags=tuple(tags))
                self.status_var.set("Drop Kick tarafından doğrulandı ve tamamlandı.")
                if self.queue_running and self.queue_current_idx == idx:
                    self.queue_current_idx = None
                    self._run_queue_from(idx + 1)
                self._update_queue_summary()
                return

            switch_reasons = {
                "offline",
                "wrong_category",
                "no_progress",
                "verification_failed",
            }
            if reason in switch_reasons:
                campaign_channels = item.get("campaign_channels") or []
                current_url = item["url"]
                tried = list(item.get("tried_channels") or [])
                if current_url not in tried:
                    tried.append(current_url)

                alternatives = []
                for channel in campaign_channels:
                    url = channel.get("url") if isinstance(channel, dict) else channel
                    if url and url != current_url and url not in tried:
                        alternatives.append(url)

                next_url = None
                for alternative in alternatives:
                    if kick_live_status_by_api(alternative) is not False:
                        next_url = alternative
                        break

                if next_url:
                    item["url"] = next_url
                    tried.append(next_url)
                    item["tried_channels"] = tried
                    self.config_data.save()
                    self.refresh_list()
                    reason_text = {
                        "offline": "Yayın çevrimdışı",
                        "wrong_category": "Kategori değişti",
                        "no_progress": "Drop ilerlemesi 8 dakika doğrulanmadı",
                        "verification_failed": "Yayın doğrulanamadı",
                    }[reason]
                    self.status_var.set(
                        f"{reason_text}; {next_url.split('/')[-1]} kanalına geçiliyor."
                    )
                    self.last_verification_var.set(
                        f"Son doğrulama: {reason_text}"
                    )
                    if self.queue_running:
                        self.queue_current_idx = None
                        self.after(1000, lambda current=idx: self._start_index(current))
                    self._update_queue_summary()
                    return

                item["tried_channels"] = tried
                self.config_data.save()
                if str(idx) in self.tree.get_children():
                    values = list(self.tree.item(str(idx), "values"))
                    values[2] = f"{elapsed}s ({self.t('retry')})"
                    tags = set(self.tree.item(str(idx), "tags") or [])
                    tags.add("redo")
                    tags.discard("finished")
                    self.tree.item(str(idx), values=values, tags=tuple(tags))

            if reason in {"progress_error", "browser_error"}:
                self.queue_running = False
                self.queue_current_idx = None
                message = (
                    "Kick drop ilerlemesi okunamadı. Giriş ve bağlantı kontrol edilmeli."
                    if reason == "progress_error"
                    else "Tarayıcı hatası nedeniyle madencilik durduruldu."
                )
                self.status_var.set(message)
                self.last_verification_var.set(f"Son doğrulama: Hata")
                self.ui_print(message)
            elif reason not in {"user_stopped", "replaced", "app_closing", "list_cleared"}:
                if self.queue_running and self.queue_current_idx == idx:
                    self.queue_current_idx = None
                    self._run_queue_from(idx + 1)

            self._update_queue_summary()

        self.after(0, ui_finish)
