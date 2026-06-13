import os
import customtkinter as ctk

class SettingsTab(ctk.CTkFrame):
    def __init__(self, parent, app, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.app = app
        
        # Center the window
        
        # Consistent theme
        ctk.set_appearance_mode("Dark" if self.app.config_data.dark_mode else "Light")
        
        self._build_ui()

    def _build_ui(self):
        # Main frame with padding
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Title
        title_label = ctk.CTkLabel(
            main_frame,
            text="Ayarlar",
            font=ctk.CTkFont(size=24, family="Segoe UI", weight="bold")
        )
        title_label.pack(pady=(0, 20))
        
        # Scrollable frame for settings
        scrollable_frame = ctk.CTkScrollableFrame(main_frame)
        scrollable_frame.pack(fill="both", expand=True)
        
        # Player Settings Section
        player_section = ctk.CTkFrame(scrollable_frame)
        player_section.pack(fill="x", pady=(0, 15))
        
        player_title = ctk.CTkLabel(
            player_section,
            text="Oynatıcı Ayarları",
            font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold")
        )
        player_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # Mute toggle
        sw_mute = ctk.CTkSwitch(
            player_section,
            text=self.app.t("switch_mute"),
            command=self.app.on_toggle_mute,
            variable=self.app.mute_var,
        )
        sw_mute.pack(anchor="w", padx=15, pady=5)
        
        # Hide player toggle
        sw_hide = ctk.CTkSwitch(
            player_section,
            text=self.app.t("switch_hide"),
            command=self.app.on_toggle_hide,
            variable=self.app.hide_player_var,
        )
        sw_hide.pack(anchor="w", padx=15, pady=5)
        
        # Mini player toggle
        sw_mini = ctk.CTkSwitch(
            player_section,
            text=self.app.t("switch_mini"),
            command=self.app.on_toggle_mini,
            variable=self.app.mini_player_var,
        )
        sw_mini.pack(anchor="w", padx=15, pady=5)
        
        # Force 160p toggle
        sw_force_160p = ctk.CTkSwitch(
            player_section,
            text=self.app.t("switch_force_160p"),
            command=self.app.on_toggle_force_160p,
            variable=self.app.force_160p_var,
        )
        sw_force_160p.pack(anchor="w", padx=15, pady=(5, 15))
        
        # Queue Settings Section
        queue_section = ctk.CTkFrame(scrollable_frame)
        queue_section.pack(fill="x", pady=(0, 15))
        
        queue_title = ctk.CTkLabel(
            queue_section,
            text="Yayın Sırası Ayarları",
            font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold")
        )
        queue_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # Auto-start toggle
        sw_auto_start = ctk.CTkSwitch(
            queue_section,
            text="Yayın sırasını otomatik başlat",
            command=self.app.on_toggle_auto_start,
            variable=self.app.auto_start_var,
        )
        sw_auto_start.pack(anchor="w", padx=15, pady=(5, 15))
        
        # Appearance Settings Section
        appearance_section = ctk.CTkFrame(scrollable_frame)
        appearance_section.pack(fill="x", pady=(0, 15))
        
        appearance_title = ctk.CTkLabel(
            appearance_section,
            text="Görünüm",
            font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold")
        )
        appearance_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # Theme dropdown
        theme_label = ctk.CTkLabel(appearance_section, text=self.app.t("label_theme"))
        theme_label.pack(anchor="w", padx=15, pady=(5, 5))
        theme_menu = ctk.CTkOptionMenu(
            appearance_section,
            values=[self.app.t("theme_dark"), self.app.t("theme_light")],
            command=self.app.change_theme,
            variable=self.app.theme_var,
            width=350,
        )
        theme_menu.pack(anchor="w", padx=15, pady=(0, 10))
        
        # Language dropdown
        language_choices = self.app._get_language_choices()
        lang_label = ctk.CTkLabel(appearance_section, text=self.app.t("label_language"))
        lang_label.pack(anchor="w", padx=15, pady=(5, 5))
        lang_menu = ctk.CTkOptionMenu(
            appearance_section,
            values=language_choices,
            command=self.app.change_language,
            variable=self.app.lang_var,
            width=350,
        )
        lang_menu.pack(anchor="w", padx=15, pady=(0, 15))
        
        # Browser Settings Section
        browser_section = ctk.CTkFrame(scrollable_frame)
        browser_section.pack(fill="x", pady=(0, 15))
        
        browser_title = ctk.CTkLabel(
            browser_section,
            text="Tarayıcı Ayarları",
            font=ctk.CTkFont(size=15, family="Segoe UI", weight="bold")
        )
        browser_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # ChromeDriver button
        def choose_chromedriver_wrapper():
            self.app.choose_chromedriver()
            chromedriver_label.configure(
                text=f"Mevcut: {os.path.basename(self.app.config_data.chromedriver_path) if self.app.config_data.chromedriver_path else 'Ayarlanmadı'}"
            )
        
        btn_chromedriver = ctk.CTkButton(
            browser_section,
            text=self.app.t("btn_chromedriver"),
            command=choose_chromedriver_wrapper,
            width=350,
        )
        btn_chromedriver.pack(anchor="w", padx=15, pady=5)
        
        # Show current chromedriver path if set
        chromedriver_label = ctk.CTkLabel(
            browser_section,
            text=f"Mevcut: {os.path.basename(self.app.config_data.chromedriver_path) if self.app.config_data.chromedriver_path else 'Ayarlanmadı'}",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray50")
        )
        chromedriver_label.pack(anchor="w", padx=15, pady=(0, 10))
        
        # Chrome Extension button
        def choose_extension_wrapper():
            self.app.choose_extension()
            extension_label.configure(
                text=f"Mevcut: {os.path.basename(self.app.config_data.extension_path) if self.app.config_data.extension_path else 'Ayarlanmadı'}"
            )
        
        btn_extension = ctk.CTkButton(
            browser_section,
            text=self.app.t("btn_extension"),
            command=choose_extension_wrapper,
            width=350,
        )
        btn_extension.pack(anchor="w", padx=15, pady=5)
        
        # Show current extension path if set
        extension_label = ctk.CTkLabel(
            browser_section,
            text=f"Mevcut: {os.path.basename(self.app.config_data.extension_path) if self.app.config_data.extension_path else 'Ayarlanmadı'}",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray50")
        )
        extension_label.pack(anchor="w", padx=15, pady=(0, 15))
        
