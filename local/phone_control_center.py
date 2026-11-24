#!/usr/bin/env python3
"""20-Phone Control Center — Android live previews, ADB control, and scrcpy."""
import base64
import io
import shutil
import subprocess
import threading
import time
import struct
import math
import queue
import tkinter as tk
from tkinter import ttk, messagebox
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

APP_TITLE = "20-Phone Control Center"
ANDROID_APPS = {
    "Facebook": "com.facebook.katana",
    "Instagram": "com.instagram.android",
    "X": "com.twitter.android",
    "YouTube": "com.google.android.youtube",
    "Snapchat": "com.snapchat.android",
    "Google Drive": "com.google.android.apps.docs",
    "Play Store": "com.android.vending",
}
PREVIEW_FALLBACK_INTERVAL_SECONDS = 0.9
LIVE_PREVIEW_FPS = 10
PREVIEW_MAX_WIDTH = 260
PREVIEW_MAX_HEIGHT = 390
SCREEN_GRID_HEIGHT = 420
SCREEN_TILE_MAX_HEIGHT = 560
SCREEN_TILE_MIN_HEIGHT = 110
SCREEN_TILE_MIN_WIDTH = 88
SCREEN_TILE_GAP = 8
APP_BG = "#070a10"
PANEL_BG = "#10151e"
TEXT_FG = "#edf3fb"
MUTED_FG = "#8997a9"
ACCENT = "#55c9ff"
BORDER = "#293646"


def responsive_card_preview_limit(device_count):
    if device_count <= 0:
        return PREVIEW_MAX_WIDTH, PREVIEW_MAX_HEIGHT
    scale = 1 / math.sqrt(device_count)
    return (
        max(80, int(PREVIEW_MAX_WIDTH * scale)),
        max(120, int(PREVIEW_MAX_HEIGHT * scale)),
    )


def responsive_screen_layout(serials, available_width, source_sizes):
    """Return a responsive column count and per-screen image bounds."""
    count = len(serials)
    if not count:
        return 1, {}, 260

    aspects = []
    for serial in serials:
        width, height = source_sizes.get(serial, (9, 20))
        aspects.append(max(0.1, width / max(1, height)))
    average_aspect = sum(aspects) / len(aspects)
    target_height = max(
        SCREEN_TILE_MIN_HEIGHT,
        int(SCREEN_TILE_MAX_HEIGHT / math.sqrt(count)),
    )
    target_width = min(SCREEN_TILE_MAX_HEIGHT, int(target_height * average_aspect))
    column_width_hint = max(SCREEN_TILE_MIN_WIDTH, target_width + 16)
    max_columns = max(1, (available_width - SCREEN_TILE_GAP) // column_width_hint)
    columns = max(1, min(count, max_columns))
    cell_width = max(
        1,
        (available_width - (columns + 1) * SCREEN_TILE_GAP) // columns,
    )

    limits = {}
    for serial, aspect in zip(serials, aspects):
        image_width = min(
            cell_width - 12,
            int(target_height * aspect),
            SCREEN_TILE_MAX_HEIGHT,
        )
        image_width = max(1, image_width)
        image_height = min(target_height, int(image_width / aspect))
        limits[serial] = (image_width, max(1, image_height))
    return columns, limits, target_height


class Phone:
    def __init__(self, key, label, detail="", state="Connected"):
        self.key = key
        self.label = label
        self.detail = detail
        self.state = state
        self.selected = False


class ControlCenter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        initial_width = min(1800, max(900, self.winfo_screenwidth() - 60))
        initial_height = min(1100, max(620, self.winfo_screenheight() - 100))
        self.initial_width = initial_width
        self.geometry(f"{initial_width}x{initial_height}")
        self.minsize(900, 620)
        self.phones = {}
        self.vars = {}
        self.preview_images = {}
        self.preview_labels = {}
        self.preview_sizes = {}
        self.preview_payloads = {}
        self.latest_stream_frames = {}
        self.latest_stream_frames_lock = threading.Lock()
        self.preview_workers = {}
        self.preview_workers_lock = threading.Lock()
        self.active_preview_serials = set()
        self.preview_states = {}
        self.preview_states_lock = threading.Lock()
        self.preview_state_snapshot = {}
        self._preview_drain_started = False
        self.preview_limit = (PREVIEW_MAX_WIDTH, PREVIEW_MAX_HEIGHT)
        self.screen_grid_images = {}
        self.screen_grid_labels = {}
        self.screen_grid_captions = {}
        self.screen_tiles = {}
        self.screen_tile_limits = {}
        self.screen_grid_columns = 0
        self.screen_canvas_height = SCREEN_GRID_HEIGHT
        self.primary_photo = None
        self.primary_image_item = None
        self.primary_empty_item = None
        self.primary_current_serial = None
        self.primary_scale = (1.0, 1.0)
        self.primary_offset = (0, 0)
        self.viewer_image = None
        self.viewer_display_size = (1, 1)
        self.viewer_original_size = (1, 1)
        self.viewer_key = None
        self.busy = False
        self.closing = False
        self.preview_thread_started = False
        self.stop_event = threading.Event()
        self.status = tk.StringVar(
            value="Connect Android phones by USB, unlock them, enable USB debugging, and approve the Mac."
        )
        self.mirror_controls_var = tk.BooleanVar(value=True)
        self.session_apps = [
            (name, ANDROID_APPS[name]) for name in ("Facebook", "Instagram", "YouTube")
        ]
        self.session_phones = []
        self.session_active = False
        self.session_paused = False
        self.session_launching = False
        self.session_index = -1
        self.session_seconds_remaining = 180
        self.session_tick_id = None
        self.session_info_var = tk.StringVar(
            value="Facebook → Instagram → YouTube · 3 minutes each · scrolling stays manual"
        )
        self.session_timer_var = tk.StringVar(value="3:00 each")
        self.hd_window = None
        self.hd_canvas = None
        self.hd_photo = None
        self.hd_image_item = None
        self.hd_view_mode = "fit"
        self.hd_scale = 1.0
        self.hd_offset = (0, 0)
        self.hd_serial = None
        self.pointer_gesture = None
        self.input_queues = {}
        self.input_workers = set()
        self.input_queue_lock = threading.Lock()
        self._configure_theme()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(500, self.refresh_devices)

    def _configure_theme(self):
        self.configure(background=APP_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=APP_BG, foreground=TEXT_FG, font=("Helvetica Neue", 10))
        style.configure("TFrame", background=APP_BG)
        style.configure("TLabel", background=APP_BG, foreground=TEXT_FG)
        style.configure("Muted.TLabel", background=APP_BG, foreground=MUTED_FG)
        style.configure("Metric.TLabel", background=APP_BG, foreground=ACCENT, font=("Helvetica Neue", 10, "bold"))
        style.configure(
            "TLabelframe", background=PANEL_BG, foreground=TEXT_FG,
            bordercolor=BORDER, lightcolor="#1c2836", darkcolor="#080b10", relief="solid"
        )
        style.configure(
            "TLabelframe.Label", background=PANEL_BG, foreground=ACCENT,
            font=("Helvetica Neue", 10, "bold")
        )
        style.configure(
            "TButton", background="#1a2634", foreground=TEXT_FG,
            bordercolor=BORDER, lightcolor="#263648", darkcolor="#0c1118", padding=(11, 6)
        )
        style.map(
            "TButton", background=[("pressed", "#102131"), ("active", "#26394d")],
            foreground=[("disabled", "#657184")]
        )
        style.configure(
            "Accent.TButton", background="#12466a", foreground="#f6fbff",
            bordercolor="#2b86b5", lightcolor="#27769a", darkcolor="#0b2639",
            padding=(12, 7), font=("Helvetica Neue", 10, "bold")
        )
        style.map("Accent.TButton", background=[("pressed", "#0b3049"), ("active", "#1b628b")])
        style.configure("TCheckbutton", background=PANEL_BG, foreground=TEXT_FG, indicatorcolor="#0a1119")
        style.map(
            "TCheckbutton", background=[("active", PANEL_BG)],
            foreground=[("disabled", "#657184")], indicatorcolor=[("selected", ACCENT)]
        )
        style.configure("TEntry", fieldbackground="#090d13", foreground=TEXT_FG, bordercolor=BORDER)
        style.configure("TCombobox", fieldbackground="#0b1119", background="#182433", foreground=TEXT_FG, arrowcolor=ACCENT)
        style.map(
            "TCombobox", fieldbackground=[("readonly", "#0b1119")],
            foreground=[("readonly", TEXT_FG)], selectbackground=[("readonly", "#1b4e70")],
            selectforeground=[("readonly", TEXT_FG)]
        )
        style.configure("TScrollbar", background="#1b2633", troughcolor="#090d13", bordercolor=BORDER, arrowcolor=MUTED_FG)

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Button(header, text="Refresh devices", style="Accent.TButton", command=self.refresh_devices).pack(side="right")
        ttk.Label(
            root,
            text="Connected Android previews · manual app sessions · mirrored phone control",
            style="Muted.TLabel"
        ).pack(anchor="w", pady=(0, 10))

        actions = ttk.LabelFrame(root, text="Actions for selected Android devices", padding=8)
        actions.pack(fill="x", pady=(0, 8))
        action_layout = ttk.Frame(actions)
        action_layout.pack(fill="x")
        action_layout.columnconfigure(0, weight=1)
        action_layout.columnconfigure(2, weight=0)

        action_controls = ttk.Frame(action_layout)
        action_controls.grid(row=0, column=0, sticky="nsew")
        action_separator = ttk.Separator(action_layout, orient="vertical")
        action_separator.grid(
            row=0, column=1, sticky="ns", padx=12
        )
        session_panel = ttk.Frame(action_layout, padding=(2, 0, 0, 0))
        session_panel.grid(row=0, column=2, sticky="nsew")
        action_layout_mode = {"compact": None}

        def adapt_action_layout(event):
            compact = event.width < 1250
            if compact == action_layout_mode["compact"]:
                return
            action_layout_mode["compact"] = compact
            if compact:
                action_separator.configure(orient="horizontal")
                action_separator.grid(
                    row=1, column=0, columnspan=3, sticky="ew", padx=0, pady=10
                )
                session_panel.grid(row=2, column=0, columnspan=3, sticky="ew")
                action_layout.columnconfigure(2, weight=1)
            else:
                action_separator.configure(orient="vertical")
                action_separator.grid(row=0, column=1, sticky="ns", padx=12, pady=0)
                session_panel.grid(row=0, column=2, sticky="nsew")
                action_layout.columnconfigure(2, weight=0)

        action_layout.bind("<Configure>", adapt_action_layout)

        row = ttk.Frame(action_controls)
        row.pack(fill="x")
        ttk.Button(row, text="Select all", command=self.select_all).pack(side="left", padx=(0, 5))
        ttk.Button(row, text="Select none", command=self.select_none).pack(side="left", padx=(0, 12))
        ttk.Button(row, text="Wake selected", command=self.wake_selected).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            row, text="Mirror input to selected", variable=self.mirror_controls_var
        ).pack(side="left", padx=(0, 8))

        app_row = ttk.Frame(action_controls)
        app_row.pack(fill="x", pady=(8, 0))
        for app_name, package in ANDROID_APPS.items():
            ttk.Button(
                app_row,
                text=app_name,
                command=lambda app=app_name, app_package=package: self.launch_selected_app(app, app_package),
            ).pack(side="left", padx=(0, 5))

        ttk.Label(session_panel, text="Power Boost · manual session:").pack(anchor="w")
        ttk.Label(
            session_panel, textvariable=self.session_info_var, wraplength=430, justify="left"
        ).pack(anchor="w", pady=(3, 0))
        session_row = ttk.Frame(session_panel)
        session_row.pack(fill="x", pady=(8, 0))
        ttk.Label(session_row, textvariable=self.session_timer_var, style="Metric.TLabel").pack(side="left", padx=(0, 8))
        self.session_start_button = ttk.Button(
            session_row, text="Start session", command=self.start_manual_session
        )
        self.session_start_button.pack(side="left", padx=(0, 5))
        self.session_next_button = ttk.Button(
            session_row, text="Next app", command=self.next_manual_session_app, state="disabled"
        )
        self.session_next_button.pack(side="left", padx=(0, 5))
        self.session_pause_button = ttk.Button(
            session_row, text="Pause", command=self.toggle_manual_session_pause, state="disabled"
        )
        self.session_pause_button.pack(side="left", padx=(0, 5))
        self.session_stop_button = ttk.Button(
            session_row, text="Stop", command=self.stop_manual_session, state="disabled"
        )
        self.session_stop_button.pack(side="left")
        ttk.Label(
            session_panel,
            text="Select Android phones in the device cards below. App buttons open the chosen app on selected phones. Session timers wait for you to advance; scrolling and other input stay manual.",
            style="Muted.TLabel", wraplength=430, justify="left"
        ).pack(anchor="w", pady=(7, 0))

        stats = ttk.Frame(root)
        stats.pack(fill="x", pady=(0, 8))
        self.total_stat = ttk.Label(stats, text="Connected: 0 / 20", style="Metric.TLabel")
        self.total_stat.pack(side="left", padx=(0, 18))
        self.android_stat = ttk.Label(stats, text="Android: 0", style="Metric.TLabel")
        self.android_stat.pack(side="left", padx=(0, 18))
        self.selected_stat = ttk.Label(stats, text="Selected: 0")
        self.selected_stat.pack(side="left")
        # All authorized Android previews share one grid.
        viewer = ttk.LabelFrame(root, text="Phone screens · embedded preview", padding=8)
        viewer.pack(fill="x", pady=(0, 8))
        viewer_controls = ttk.Frame(viewer)
        viewer_controls.pack(fill="x", pady=(0, 5))
        ttk.Label(viewer_controls, text="Keyboard target:").pack(side="left")
        self.viewer_device_var = tk.StringVar(value="Select an Android phone")
        self.viewer_combo = ttk.Combobox(
            viewer_controls, textvariable=self.viewer_device_var, state="readonly", width=38
        )
        self.viewer_combo.pack(side="left", padx=(6, 10))
        self.viewer_combo.bind("<<ComboboxSelected>>", self._viewer_device_changed)
        self.hd_button = ttk.Button(
            viewer_controls, text="Full-resolution view", command=self.open_hd_viewer
        )
        self.hd_button.pack(side="left", padx=(0, 8))
        ttk.Label(
            viewer_controls,
            text="Keyboard target for Android preview and mirrored input.",
            style="Muted.TLabel"
        ).pack(side="left")

        self.screen_area = ttk.Frame(viewer)
        self.screen_area.pack(fill="both", expand=True)
        self.primary_pane = ttk.LabelFrame(self.screen_area, text="Target phone · fit to panel", padding=6)
        primary_width = 300
        self.primary_pane.configure(width=primary_width)
        self.primary_pane.pack(side="left", fill="y")
        self.primary_pane.pack_propagate(False)
        primary_area = ttk.Frame(self.primary_pane)
        primary_area.pack(fill="both", expand=True)
        self.primary_canvas = tk.Canvas(
            primary_area, width=primary_width - 40, height=SCREEN_GRID_HEIGHT, background="#020407",
            highlightthickness=1, highlightbackground="#33516a"
        )
        self.primary_canvas.grid(row=0, column=0, sticky="nsew")
        primary_area.grid_rowconfigure(0, weight=1)
        primary_area.grid_columnconfigure(0, weight=1)
        self.primary_canvas.bind("<Configure>", self._primary_canvas_resized)
        self.primary_canvas.bind("<ButtonPress-1>", self._primary_pointer_press)
        self.primary_canvas.bind("<ButtonRelease-1>", self._primary_pointer_release)
        self.primary_canvas.bind("<KeyPress>", self._viewer_keypress)
        self.screen_area.bind("<Configure>", self._primary_panel_resized)
        self.primary_pane.pack_forget()

        followers = ttk.LabelFrame(
            self.screen_area,
            text="Connected Android screens",
            padding=6,
        )
        followers.pack(side="left", fill="both", expand=True, padx=(8, 0))
        follower_area = ttk.Frame(followers)
        follower_area.pack(fill="both", expand=True)
        self.screen_canvas = tk.Canvas(
            follower_area, height=SCREEN_GRID_HEIGHT, background=APP_BG,
            highlightthickness=1, highlightbackground=BORDER
        )
        self.screen_scrollbar = ttk.Scrollbar(
            follower_area, orient="vertical", command=self.screen_canvas.yview
        )
        follower_xbar = ttk.Scrollbar(follower_area, orient="horizontal", command=self.screen_canvas.xview)
        self.screen_tiles_frame = ttk.Frame(self.screen_canvas)
        self.screen_tiles_window = self.screen_canvas.create_window(
            (0, 0), window=self.screen_tiles_frame, anchor="nw"
        )
        self.screen_canvas.configure(yscrollcommand=self.screen_scrollbar.set, xscrollcommand=follower_xbar.set)
        self.screen_canvas.grid(row=0, column=0, sticky="nsew")
        self.screen_scrollbar.grid(row=0, column=1, sticky="ns")
        follower_xbar.grid(row=1, column=0, sticky="ew")
        follower_area.grid_rowconfigure(0, weight=1)
        follower_area.grid_columnconfigure(0, weight=1)
        self.screen_tiles_frame.bind("<Configure>", self._screen_tiles_configured)
        self.screen_canvas.bind("<Configure>", self._screen_canvas_configured)

        self.canvas = tk.Canvas(root, highlightthickness=0, background=APP_BG)
        self.scrollbar = ttk.Scrollbar(root, orient="vertical", command=self.canvas.yview)
        self.cards_frame = ttk.Frame(self.canvas)
        self.cards_frame.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.canvas_window, width=e.width))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, textvariable=self.status, wraplength=1080).pack(anchor="w")

    def run_cmd(self, args, timeout=8, binary=False):
        try:
            p = subprocess.run(
                args, capture_output=True, text=not binary, timeout=timeout
            )
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, b"" if binary else "", f"Command not found: {args[0]}"
        except subprocess.TimeoutExpired:
            return 124, b"" if binary else "", "Command timed out"

    def discover(self):
        found = {}
        adb = shutil.which("adb")
        if adb:
            rc, out, _err = self.run_cmd([adb, "devices", "-l"], timeout=12)
            if rc == 0:
                for line in out.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] in ("device", "unauthorized", "offline"):
                        serial, state = parts[0], parts[1]
                        model = ""
                        for part in parts[2:]:
                            if part.startswith("model:"):
                                model = part.split(":", 1)[1].replace("_", " ")
                                break
                        detail = f"ADB serial: {serial}"
                        found["android:" + serial] = Phone(
                            serial, model or serial, detail, state.title()
                        )

        for key, phone in found.items():
            if key in self.phones:
                phone.selected = self.phones[key].selected
        return found

    def refresh_devices(self):
        if self.busy or self.closing:
            return
        self.busy = True
        self.status.set("Scanning USB devices…")
        def worker():
            found = self.discover()
            if not self.closing:
                self.after(0, lambda: self._finish_refresh(found))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_refresh(self, found):
        self.phones = found
        self.busy = False
        self.preview_state_snapshot = {}
        self.render_devices()
        android = len(self.phones)
        self.total_stat.config(text=f"Connected: {android} / 20")
        self.android_stat.config(text=f"Android: {android}")
        self.status.set(
            f"Scan complete at {time.strftime('%H:%M:%S')}. Android: {android}. "
            "If Android is 0, check the data cable, USB debugging, and authorization prompt."
        )
        self.update_selected_count()
        if not self.preview_thread_started:
            self.preview_thread_started = True
            threading.Thread(target=self.preview_loop, daemon=True).start()
        if not self._preview_drain_started:
            self._preview_drain_started = True
            self.after(40, self._drain_live_frames)

    def render_devices(self):
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.vars = {}
        self.preview_labels = {}
        android_ready = [p for p in self.phones.values() if p.state.lower() == "device"]
        self.preview_limit = responsive_card_preview_limit(len(android_ready))
        self._viewer_options = {f"{p.label} ({p.key})": p.key for p in android_ready}
        self.viewer_combo.configure(values=list(self._viewer_options.keys()))
        self.hd_button.configure(state="normal" if android_ready else "disabled")
        if self.viewer_key not in [p.key for p in android_ready]:
            self.viewer_key = android_ready[0].key if android_ready else None
        if self.viewer_key:
            selected_label = next(
                (label for label, key in self._viewer_options.items() if key == self.viewer_key), ""
            )
            self.viewer_device_var.set(selected_label)
        else:
            self.viewer_device_var.set("Select an Android phone")
            self.viewer_image = None
        self._build_screen_tiles(self._screen_devices())

        items = list(self.phones.items())
        if not items:
            ttk.Label(
                self.cards_frame,
                text="No Android devices detected. Connect a phone, unlock it, approve USB debugging, then refresh.",
                padding=20, wraplength=760
            ).grid(row=0, column=0, sticky="w")
            return

        for idx, (key, phone) in enumerate(items):
            row, col = divmod(idx, 4)
            card = ttk.LabelFrame(
                self.cards_frame, text=f"Android · {phone.state}", padding=8
            )
            card.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            var = tk.BooleanVar(value=phone.selected)
            self.vars[key] = var
            var.trace_add("write", lambda *_args, k=key, v=var: self._selection_changed(k, v))
            ttk.Checkbutton(card, text="Select", variable=var).pack(anchor="w")
            ttk.Label(
                card, text=phone.label, font=("Helvetica Neue", 10, "bold"),
                wraplength=PREVIEW_MAX_WIDTH
            ).pack(anchor="w", pady=(2, 1))
            ttk.Label(card, text=phone.detail, style="Muted.TLabel", wraplength=PREVIEW_MAX_WIDTH).pack(anchor="w")

            if phone.state.lower() == "device":
                preview = ttk.Label(
                    card, text="Waiting for screen preview…", anchor="center",
                    relief="solid", padding=5
                )
                preview.pack(fill="both", expand=True, pady=(6, 4))
                preview.bind("<Button-1>", lambda _e, serial=phone.key: self._select_viewer_by_key(serial))
                preview.configure(cursor="hand2")
                ttk.Button(
                    card, text="Set as Android keyboard target",
                    command=lambda serial=phone.key: self._select_viewer_by_key(serial)
                ).pack(fill="x")
                preview_key = phone.key
                self.preview_labels[preview_key] = preview
                payload = self.preview_payloads.get(preview_key)
                if payload:
                    image = self._make_preview_image(payload[0], self.preview_limit)
                    self.preview_images[preview_key] = image
                    preview.configure(image=image, text="")
                elif phone.key in self.preview_images:
                    preview.configure(image=self.preview_images[phone.key], text="")
            else:
                ttk.Label(
                    card, text="Unlock phone and approve USB debugging to enable preview.",
                    wraplength=PREVIEW_MAX_WIDTH
                ).pack(fill="both", expand=True, pady=12)

        for col in range(4):
            self.cards_frame.grid_columnconfigure(col, weight=1, uniform="col")

    def _screen_devices(self):
        """Return authorized Android phones shown in the live preview grid."""
        return [phone for phone in self.phones.values() if phone.state.lower() == "device"]



    def _build_screen_tiles(self, devices):
        for child in self.screen_tiles_frame.winfo_children():
            child.destroy()
        self.screen_grid_labels = {}
        self.screen_grid_captions = {}
        active = {phone.key for phone in devices}
        self.screen_tiles = {}
        for stale in set(self.screen_grid_images) - active:
            self.screen_grid_images.pop(stale, None)
        for stale in set(self.preview_payloads) - active:
            self.preview_payloads.pop(stale, None)
            self.preview_sizes.pop(stale, None)
        self.screen_tile_limits = {}

        self._render_primary_screen()

        if not devices:
            ttk.Label(
                self.screen_tiles_frame,
                text="Connected Android screens will appear here after USB debugging is authorized.",
                anchor="center", padding=20, style="Muted.TLabel", wraplength=420
            ).grid(row=0, column=0, sticky="nsew")
            self.screen_canvas_height = 260
            self.screen_canvas.configure(height=260)
            self._screen_tiles_configured()
            return

        for phone in devices:
            preview_key = phone.key
            tile = ttk.Frame(self.screen_tiles_frame, relief="groove", borderwidth=1, padding=4)
            caption = ttk.Label(
                tile, text=f"Android · {phone.label}", anchor="center", wraplength=140
            )
            caption.pack(fill="x", pady=(0, 3))
            screen = ttk.Label(
                tile, text="Waiting for screen…", anchor="center", relief="solid", padding=3,
                cursor="crosshair"
            )
            screen.pack(padx=2, pady=(0, 2))
            screen.bind(
                "<ButtonPress-1>",
                lambda event, serial=phone.key: self._screen_pointer_press(event, serial)
            )
            screen.bind(
                "<ButtonRelease-1>",
                lambda event, serial=phone.key: self._screen_pointer_release(event, serial)
            )
            screen.bind("<KeyPress>", self._viewer_keypress)
            self.screen_tiles[preview_key] = tile
            self.screen_grid_labels[preview_key] = screen
            self.screen_grid_captions[preview_key] = caption
            image = self.screen_grid_images.get(preview_key)
            if image:
                screen.configure(image=image, text="")

        self._update_active_screen_tile()
        self.after_idle(self._layout_screen_tiles)

    def _screen_canvas_configured(self, event):
        self.screen_canvas.itemconfigure(self.screen_tiles_window, width=event.width)
        self._layout_screen_tiles()

    def _screen_tiles_configured(self, _event=None):
        if self.screen_canvas.winfo_exists():
            self.screen_canvas.configure(scrollregion=self.screen_canvas.bbox("all"))

    def _layout_screen_tiles(self):
        if not self.screen_tiles:
            return
        serials = list(self.screen_tiles)
        available_width = max(240, self.screen_canvas.winfo_width())
        columns, limits, target_height = responsive_screen_layout(
            serials, available_width, self.preview_sizes
        )
        rows = math.ceil(len(serials) / columns)
        canvas_height = min(
            560,
            max(260, rows * (target_height + 40) + SCREEN_TILE_GAP),
        )
        if canvas_height != self.screen_canvas_height:
            self.screen_canvas_height = canvas_height
            self.screen_canvas.configure(height=canvas_height)

        for column in range(max(self.screen_grid_columns, columns)):
            self.screen_tiles_frame.grid_columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="screen" if column < columns else ""
            )
        self.screen_grid_columns = columns
        cell_width = max(
            60,
            (available_width - (columns + 1) * SCREEN_TILE_GAP) // columns,
        )
        for caption in self.screen_grid_captions.values():
            caption.configure(wraplength=max(60, cell_width - 12))
        for index, serial in enumerate(serials):
            self.screen_tiles[serial].grid(
                row=index // columns, column=index % columns,
                sticky="n", padx=SCREEN_TILE_GAP // 2, pady=SCREEN_TILE_GAP // 2
            )

        previous_limits = self.screen_tile_limits
        self.screen_tile_limits = limits
        for serial, limit in limits.items():
            if previous_limits.get(serial) != limit and serial in self.preview_payloads:
                self._refresh_screen_tile(serial)
        self._screen_tiles_configured()

    def _refresh_screen_tile(self, serial):
        label = self.screen_grid_labels.get(serial)
        payload = self.preview_payloads.get(serial)
        if not label or not payload:
            return
        limit = self.screen_tile_limits.get(serial, (180, 360))
        try:
            image = self._make_preview_image(payload[0], limit)
            self.screen_grid_images[serial] = image
            label.configure(image=image, text="")
            if serial == self.viewer_key:
                self.viewer_image = image
                self.viewer_display_size = (image.width(), image.height())
                self.viewer_original_size = self.preview_sizes.get(serial, (1, 1))
        except Exception as exc:
            label.configure(image="", text=f"Preview unavailable: {str(exc)[:90]}")

    def _render_primary_screen(self):
        if not hasattr(self, "primary_canvas"):
            return
        self._update_primary_panel_size()
        preview_key = self._primary_preview_key()
        phone = self.phones.get("android:" + self.viewer_key) if self.viewer_key else None
        payload = self.preview_payloads.get(preview_key) if preview_key else None
        if not phone or not payload:
            self.primary_canvas.delete("all")
            self.primary_photo = None
            self.primary_image_item = None
            self.primary_current_serial = None
            self.primary_scale = (1.0, 1.0)
            self.primary_offset = (0, 0)
            text = "Waiting for the target phone's screen…" if phone else "Connect and authorize an Android phone."
            title = f"{phone.label} · fit to panel" if phone else "Target Android phone · fit to panel"
            self.primary_canvas.create_text(
                max(1, self.primary_canvas.winfo_width() // 2),
                max(1, self.primary_canvas.winfo_height() // 2),
                text=text,
                fill=MUTED_FG, anchor="center", width=330
            )
            self.primary_pane.configure(text=title)
            return

        encoded, width, height = payload
        try:
            canvas_width = max(1, self.primary_canvas.winfo_width() - 4)
            canvas_height = max(1, self.primary_canvas.winfo_height() - 4)
            if canvas_width <= 1 or canvas_height <= 1:
                return
            photo = self._make_preview_image(encoded, (canvas_width, canvas_height))
            display_width, display_height = photo.width(), photo.height()
            offset_x = max(0, (canvas_width - display_width) // 2)
            offset_y = max(0, (canvas_height - display_height) // 2)
            if self.primary_image_item is None or self.primary_current_serial != preview_key:
                self.primary_canvas.delete("all")
                self.primary_image_item = self.primary_canvas.create_image(
                    offset_x, offset_y, image=photo, anchor="nw"
                )
            else:
                self.primary_canvas.itemconfigure(self.primary_image_item, image=photo)
                self.primary_canvas.coords(self.primary_image_item, offset_x, offset_y)
            self.primary_photo = photo
            self.primary_current_serial = preview_key
            self.primary_offset = (offset_x, offset_y)
            self.primary_scale = (
                display_width / max(1, width),
                display_height / max(1, height),
            )
            self.viewer_image = photo
            self.viewer_display_size = (display_width, display_height)
            self.viewer_original_size = (width, height)
            fit_percent = round(min(self.primary_scale) * 100)
            self.primary_pane.configure(text=f"{phone.label} · {fit_percent}% fit")
        except Exception as exc:
            self.primary_canvas.delete("all")
            self.primary_image_item = None
            self.primary_current_serial = None
            self.primary_photo = None
            self.primary_scale = (1.0, 1.0)
            self.primary_offset = (0, 0)
            self.primary_canvas.create_text(
                12, 12, text=f"Could not display native screenshot: {str(exc)[:120]}",
                fill="#ffb3b3", anchor="nw", width=350
            )

    def _primary_canvas_resized(self, _event=None):
        preview_key = self._primary_preview_key()
        if preview_key and self.preview_payloads.get(preview_key):
            self.after_idle(self._render_primary_screen)

    def _primary_panel_resized(self, _event=None):
        self._update_primary_panel_size()

    def _update_primary_panel_size(self):
        if not hasattr(self, "primary_canvas"):
            return
        available_width = self.screen_area.winfo_width()
        if available_width < 100:
            available_width = max(600, self.initial_width - 60)

        preview_key = self._primary_preview_key()
        source_width, source_height = self.preview_sizes.get(preview_key, (0, 0))
        if source_width > 0 and source_height > 0:
            max_canvas_width = max(120, available_width - 448)
            canvas_width = min(
                max_canvas_width,
                max(120, round(SCREEN_GRID_HEIGHT * source_width / source_height)),
            )
        else:
            canvas_width = 260

        if int(self.primary_canvas.cget("width")) != canvas_width:
            self.primary_canvas.configure(width=canvas_width)
        if int(self.primary_canvas.cget("height")) != SCREEN_GRID_HEIGHT:
            self.primary_canvas.configure(height=SCREEN_GRID_HEIGHT)
        panel_width = canvas_width + 20
        if int(self.primary_pane.cget("width")) != panel_width:
            self.primary_pane.configure(width=panel_width)

    def _primary_preview_key(self):
        return self.viewer_key

    def _primary_point_from_event(self, event, clamp=False):
        if not self.viewer_key:
            return None
        width, height = self.preview_sizes.get(self.viewer_key, (0, 0))
        if not width or not height:
            return None
        scale_x, scale_y = self.primary_scale
        offset_x, offset_y = self.primary_offset
        x = int((self.primary_canvas.canvasx(event.x) - offset_x) / max(scale_x, 1e-9))
        y = int((self.primary_canvas.canvasy(event.y) - offset_y) / max(scale_y, 1e-9))
        if clamp:
            x = max(0, min(width - 1, x))
            y = max(0, min(height - 1, y))
        elif not (0 <= x < width and 0 <= y < height):
            return None
        return x, y

    def _primary_pointer_press(self, event):
        self.primary_canvas.focus_set()
        point = self._primary_point_from_event(event)
        self.pointer_gesture = None
        if point:
            self.pointer_gesture = {
                "serial": self.viewer_key,
                "start": point,
                "root": (event.x_root, event.y_root),
                "pressed_at": time.monotonic(),
            }
        return "break"

    def _primary_pointer_release(self, event):
        gesture = self.pointer_gesture
        self.pointer_gesture = None
        if not gesture or gesture["serial"] != self.viewer_key:
            return "break"
        end = self._primary_point_from_event(event, clamp=True)
        start = gesture["start"]
        moved = (
            abs(event.x_root - gesture["root"][0]) >= 5
            or abs(event.y_root - gesture["root"][1]) >= 5
        )
        held_ms = max(1, int((time.monotonic() - gesture["pressed_at"]) * 1000))
        if moved and end:
            self._dispatch_pointer_action(self.viewer_key, start, end, max(100, min(1500, held_ms)))
        elif held_ms >= 500:
            self._dispatch_pointer_action(self.viewer_key, start, start, max(500, min(5000, held_ms)))
        else:
            self._dispatch_pointer_action(self.viewer_key, start)
        return "break"

    def _screen_point_from_label(self, label, serial, event, clamp=False):
        image = self.screen_grid_images.get(serial)
        source_size = self.preview_sizes.get(serial)
        if not label or not image or not source_size:
            return None
        displayed_w, displayed_h = image.width(), image.height()
        label_w, label_h = label.winfo_width(), label.winfo_height()
        x = event.x - (label_w - displayed_w) // 2
        y = event.y - (label_h - displayed_h) // 2
        if clamp:
            x = max(0, min(displayed_w - 1, x))
            y = max(0, min(displayed_h - 1, y))
        elif not (0 <= x < displayed_w and 0 <= y < displayed_h):
            return None
        original_w, original_h = source_size
        return (
            max(0, min(original_w - 1, int(x * original_w / displayed_w))),
            max(0, min(original_h - 1, int(y * original_h / displayed_h))),
        )

    def _screen_pointer_press(self, event, serial):
        # Keep the clicked thumbnail alive until release so its gesture can finish.
        self._select_viewer_by_key(serial, rebuild=False)
        label = self.screen_grid_labels.get(serial)
        point = self._screen_point_from_label(label, serial, event)
        self.pointer_gesture = None
        if point:
            self.pointer_gesture = {
                "serial": serial,
                "start": point,
                "root": (event.x_root, event.y_root),
                "pressed_at": time.monotonic(),
            }
        return "break"

    def _screen_pointer_release(self, event, serial):
        gesture = self.pointer_gesture
        self.pointer_gesture = None
        if not gesture or gesture["serial"] != serial:
            if self.viewer_key == serial:
                self._rebuild_follower_tiles()
            return "break"
        label = self.screen_grid_labels.get(serial)
        end = self._screen_point_from_label(label, serial, event, clamp=True)
        start = gesture["start"]
        moved = (
            abs(event.x_root - gesture["root"][0]) >= 5
            or abs(event.y_root - gesture["root"][1]) >= 5
        )
        held_ms = max(1, int((time.monotonic() - gesture["pressed_at"]) * 1000))
        if moved and end:
            self._dispatch_pointer_action(serial, start, end, max(100, min(1500, held_ms)))
        elif held_ms >= 500:
            self._dispatch_pointer_action(serial, start, start, max(500, min(5000, held_ms)))
        else:
            self._dispatch_pointer_action(serial, start)
        self._rebuild_follower_tiles()
        return "break"

    def _rebuild_follower_tiles(self):
        self._build_screen_tiles(self._screen_devices())

    def _update_active_screen_tile(self):
        for serial, tile in self.screen_tiles.items():
            tile.configure(relief="sunken" if serial == self.viewer_key else "groove")

    def preview_loop(self):
        """Start live preview workers for authorized Android devices."""
        while not self.stop_event.is_set():
            adb = shutil.which("adb")
            ffmpeg = shutil.which("ffmpeg")
            devices = [phone for phone in list(self.phones.values()) if phone.state.lower() == "device"]
            active = {phone.key for phone in devices}
            workers_to_start = []
            with self.preview_workers_lock:
                self.active_preview_serials = active
                for serial, worker in list(self.preview_workers.items()):
                    if not worker.is_alive():
                        self.preview_workers.pop(serial, None)
                if adb:
                    for phone in devices:
                        worker = self.preview_workers.get(phone.key)
                        if worker is None:
                            worker = threading.Thread(
                                target=self._preview_device_worker,
                                args=(phone.key, adb, ffmpeg),
                                name=f"screen-{phone.key}",
                                daemon=True,
                            )
                            self.preview_workers[phone.key] = worker
                            workers_to_start.append(worker)
            with self.preview_states_lock:
                for stale in set(self.preview_states) - active:
                    self.preview_states.pop(stale, None)
            with self.latest_stream_frames_lock:
                for stale in set(self.latest_stream_frames) - active:
                    self.latest_stream_frames.pop(stale, None)
            for worker in workers_to_start:
                worker.start()
            self.stop_event.wait(0.5)

    def _preview_is_active(self, serial):
        if self.stop_event.is_set():
            return False
        with self.preview_workers_lock:
            return serial in self.active_preview_serials





    def _set_preview_state(self, serial, state):
        with self.preview_states_lock:
            self.preview_states[serial] = state

    def _preview_device_worker(self, serial, adb, ffmpeg):
        retry_stream_at = 0.0
        fallback_reported = False
        try:
            while self._preview_is_active(serial):
                now = time.monotonic()
                if ffmpeg and now >= retry_stream_at:
                    had_frames = self._run_live_stream(serial, adb, ffmpeg)
                    if not self._preview_is_active(serial):
                        break
                    if had_frames:
                        retry_stream_at = 0.0
                        fallback_reported = False
                        continue
                    retry_stream_at = time.monotonic() + 10.0
                    self._set_preview_state(serial, "ADB screenshot fallback")
                    fallback_reported = True
                elif not ffmpeg and not fallback_reported:
                    self._set_preview_state(serial, "ADB screenshot fallback (FFmpeg unavailable)")
                    fallback_reported = True

                self._capture_preview_snapshot(serial, adb)
                self.stop_event.wait(PREVIEW_FALLBACK_INTERVAL_SECONDS)
        finally:
            with self.preview_workers_lock:
                self.preview_workers.pop(serial, None)

    def _capture_preview_snapshot(self, serial, adb):
        try:
            rc, data, _err = self.run_cmd(
                [adb, "-s", serial, "exec-out", "screencap", "-p"],
                timeout=5, binary=True
            )
            if rc == 0 and isinstance(data, bytes) and data.startswith(b"\x89PNG") and len(data) >= 24:
                width, height = struct.unpack(">II", data[16:24])
                with self.latest_stream_frames_lock:
                    self.latest_stream_frames[serial] = (data, width, height)
            else:
                self._set_preview_state(serial, "No screen data from ADB")
        except Exception:
            self._set_preview_state(serial, "ADB screenshot fallback failed")

    @staticmethod
    def _extract_png_frames(buffer):
        """Remove and return complete PNGs from a concatenated FFmpeg output stream."""
        signature = b"\x89PNG\r\n\x1a\n"
        frames = []
        while True:
            start = buffer.find(signature)
            if start < 0:
                # Preserve a possible partial signature spanning the next read.
                del buffer[:-len(signature)]
                break
            if start:
                del buffer[:start]
            offset = len(signature)
            complete = False
            while len(buffer) >= offset + 8:
                chunk_length = struct.unpack(">I", buffer[offset:offset + 4])[0]
                chunk_type = bytes(buffer[offset + 4:offset + 8])
                chunk_end = offset + 12 + chunk_length
                if chunk_end > len(buffer):
                    break
                offset = chunk_end
                if chunk_type == b"IEND":
                    frames.append(bytes(buffer[:offset]))
                    del buffer[:offset]
                    complete = True
                    break
            if not complete:
                break
        return frames

    def _run_live_stream(self, serial, adb, ffmpeg):
        """Pipe Android's H.264 screen stream through FFmpeg into latest PNG frames."""
        adb_process = ffmpeg_process = None
        reader = feeder = None
        state_lock = threading.Lock()
        state = {"frames": 0, "last_frame": 0.0}
        had_frames = False
        feeder_done = threading.Event()
        started_at = time.monotonic()

        def read_frames():
            png_buffer = bytearray()
            try:
                while not self.stop_event.is_set():
                    chunk = ffmpeg_process.stdout.read(65536)
                    if not chunk:
                        break
                    png_buffer.extend(chunk)
                    for png in self._extract_png_frames(png_buffer):
                        if len(png) < 24:
                            continue
                        width, height = struct.unpack(">II", png[16:24])
                        if not width or not height:
                            continue
                        with self.latest_stream_frames_lock:
                            self.latest_stream_frames[serial] = (png, width, height)
                        with state_lock:
                            state["frames"] += 1
                            state["last_frame"] = time.monotonic()
                        self._set_preview_state(serial, "Live H.264")
            except (OSError, ValueError):
                pass

        def feed_decoder():
            try:
                while not self.stop_event.is_set():
                    chunk = adb_process.stdout.read(65536)
                    if not chunk:
                        break
                    ffmpeg_process.stdin.write(chunk)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                feeder_done.set()
                try:
                    ffmpeg_process.stdin.close()
                except (AttributeError, OSError, ValueError):
                    pass

        try:
            adb_process = subprocess.Popen(
                [
                    adb, "-s", serial, "exec-out", "screenrecord",
                    "--output-format=h264", "--bit-rate", "8000000",
                    # Android screenrecord caps a session at 180 seconds; the worker reconnects.
                    "--time-limit", "180", "-",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            ffmpeg_process = subprocess.Popen(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-threads", "1", "-f", "h264", "-i", "pipe:0",
                    "-vf", f"fps={LIVE_PREVIEW_FPS}", "-an", "-c:v", "png",
                    "-compression_level", "1", "-threads", "1",
                    "-f", "image2pipe", "pipe:1",
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            reader = threading.Thread(target=read_frames, daemon=True)
            feeder = threading.Thread(target=feed_decoder, daemon=True)
            reader.start()
            feeder.start()

            while self._preview_is_active(serial):
                if adb_process.poll() is not None or ffmpeg_process.poll() is not None or feeder_done.is_set():
                    break
                now = time.monotonic()
                with state_lock:
                    frame_count = state["frames"]
                    last_frame = state["last_frame"]
                if frame_count == 0 and now - started_at > 10:
                    break
                if frame_count and now - last_frame > 8:
                    break
                time.sleep(0.1)

        except (OSError, ValueError):
            pass
        finally:
            # On EOF, let FFmpeg drain the H.264 it already received before
            # terminating it. Otherwise the reader can see the first PNG only
            # after we have already decided that this stream had no frames.
            if (
                adb_process and adb_process.poll() is not None
                and feeder and feeder.is_alive()
            ):
                feeder.join(timeout=0.5)
            if (
                feeder_done.is_set() and not self.stop_event.is_set()
                and ffmpeg_process and ffmpeg_process.poll() is None
            ):
                try:
                    ffmpeg_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            for process in (adb_process, ffmpeg_process):
                if process and process.poll() is None:
                    process.terminate()
            for process in (adb_process, ffmpeg_process):
                if process:
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    except (OSError, ValueError):
                        pass
            for process in (adb_process, ffmpeg_process):
                if process:
                    for pipe in (process.stdin, process.stdout):
                        if pipe:
                            try:
                                pipe.close()
                            except OSError:
                                pass
            for thread in (reader, feeder):
                if thread and thread.is_alive():
                    thread.join(timeout=1)
            with state_lock:
                had_frames = state["frames"] > 0
        return had_frames

    def _drain_live_frames(self):
        if self.closing:
            return
        with self.latest_stream_frames_lock:
            frames = self.latest_stream_frames
            self.latest_stream_frames = {}
        if frames:
            results = {
                serial: (base64.b64encode(png).decode("ascii"), width, height)
                for serial, (png, width, height) in frames.items()
            }
            self.apply_previews(results)
        self._refresh_live_status()
        self.after(40, self._drain_live_frames)

    def _refresh_live_status(self):
        with self.preview_states_lock:
            states = dict(self.preview_states)
        if states == self.preview_state_snapshot:
            return
        self.preview_state_snapshot = states
        with self.preview_workers_lock:
            total = len(self.active_preview_serials)
        live = sum(state == "Live H.264" for state in states.values())
        fallback = [serial for serial, state in states.items() if state != "Live H.264"]
        if fallback:
            devices = ", ".join(fallback[:4])
            suffix = f" (+{len(fallback) - 4} more)" if len(fallback) > 4 else ""
            self.status.set(
                f"Live video: {live}/{total} phones. ADB screenshot fallback for: {devices}{suffix}."
            )
        elif total:
            self.status.set(f"Live H.264 video connected: {live}/{total} Android phones.")

    def apply_previews(self, results):
        if self.closing:
            return
        for serial, payload in results.items():
            encoded, original_width, original_height = payload
            self.preview_sizes[serial] = (original_width, original_height)
            self.preview_payloads[serial] = payload

            card_label = self.preview_labels.get(serial)
            phone = self.phones.get(serial) or self.phones.get("android:" + serial)
            if card_label and phone:
                try:
                    image = self._make_preview_image(encoded, self.preview_limit)
                    self.preview_images[serial] = image
                    card_label.configure(image=image, text="")
                except Exception as exc:
                    card_label.configure(text=f"Preview unavailable: {str(exc)[:90]}")

            if serial in self.screen_grid_labels:
                self._refresh_screen_tile(serial)

        primary_key = self._primary_preview_key()
        if primary_key in results:
            self._render_primary_screen()

        self._layout_screen_tiles()
        if self.hd_serial in results:
            self._render_hd_viewer()

    def _make_preview_image(self, encoded, max_size):
        max_width, max_height = max_size
        if Image is not None and ImageTk is not None:
            import io
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as source:
                image_pil = source.convert("RGB")
                resampling = getattr(Image, "Resampling", Image).LANCZOS
                image_pil.thumbnail((max_width, max_height), resampling)
                return ImageTk.PhotoImage(image_pil)

        image = tk.PhotoImage(data=encoded, format="png")
        factor = max(
            1,
            (image.width() + max_width - 1) // max_width,
            (image.height() + max_height - 1) // max_height,
        )
        return image.subsample(factor, factor) if factor > 1 else image

    def _viewer_device_changed(self, _event=None):
        label = self.viewer_device_var.get()
        self.viewer_key = getattr(self, "_viewer_options", {}).get(label)
        self._select_viewer_by_key(self.viewer_key) if self.viewer_key else None

    def _input_target_serials(self, source_serial):
        if not source_serial:
            return []
        targets = [source_serial]
        if self.mirror_controls_var.get():
            for phone in self.phones.values():
                if (
                    phone.state.lower() == "device"
                    and phone.selected
                    and phone.key not in targets
                ):
                    targets.append(phone.key)
        return targets

    def _dispatch_adb_commands(self, commands):
        if not shutil.which("adb") or not commands:
            return
        with self.input_queue_lock:
            for command in commands:
                serial = command[2]
                pending = self.input_queues.setdefault(serial, queue.Queue())
                pending.put(command)
                if serial not in self.input_workers:
                    self.input_workers.add(serial)
                    threading.Thread(
                        target=self._input_command_worker,
                        args=(serial, pending),
                        daemon=True,
                    ).start()

    def _input_command_worker(self, serial, pending):
        while not self.closing:
            try:
                command = pending.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                rc, out, err = self.run_cmd(command, timeout=5)
                if rc != 0 and not self.closing:
                    msg = f"Input failed on {serial}: {err or out or 'ADB input failed'}"
                    self.after(0, lambda text=msg: self.status.set(text))
            finally:
                pending.task_done()

    def _dispatch_pointer_action(self, source_serial, start, end=None, duration_ms=350):
        source_size = self.preview_sizes.get(source_serial)
        if not source_size:
            return
        source_w, source_h = source_size
        commands = []
        for serial in self._input_target_serials(source_serial):
            target_w, target_h = self.preview_sizes.get(serial, source_size)

            def map_point(point):
                x, y = point
                return (
                    max(0, min(target_w - 1, round(x * (target_w - 1) / max(1, source_w - 1)))),
                    max(0, min(target_h - 1, round(y * (target_h - 1) / max(1, source_h - 1)))),
                )

            x1, y1 = map_point(start)
            if end is None:
                args = ["tap", str(x1), str(y1)]
            else:
                x2, y2 = map_point(end)
                args = ["swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)]
            commands.append([shutil.which("adb") or "adb", "-s", serial, "shell", "input", *args])
        self._dispatch_adb_commands(commands)

    def _dispatch_keyevent(self, source_serial, code):
        commands = [
            [shutil.which("adb") or "adb", "-s", serial, "shell", "input", "keyevent", code]
            for serial in self._input_target_serials(source_serial)
        ]
        self._dispatch_adb_commands(commands)

    def _dispatch_text(self, source_serial, character):
        safe_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@+/:?=,"
        if character == " ":
            encoded = "%s"
        elif len(character) == 1 and character in safe_chars:
            encoded = character
        else:
            return
        commands = [
            [shutil.which("adb") or "adb", "-s", serial, "shell", "input", "text", encoded]
            for serial in self._input_target_serials(source_serial)
        ]
        self._dispatch_adb_commands(commands)

    def _viewer_keypress(self, event):
        if not self.viewer_key:
            return None
        keycodes = {
            "Return": "66", "BackSpace": "67", "Escape": "4", "space": "62",
            "Home": "3", "Left": "21", "Up": "19", "Right": "22", "Down": "20",
        }
        code = keycodes.get(event.keysym)
        if code:
            self._dispatch_keyevent(self.viewer_key, code)
            return "break"
        if event.char and event.char.isprintable() and not (event.state & 0xFC):
            self._dispatch_text(self.viewer_key, event.char)
            return "break"
        return None

    def _select_viewer_by_key(self, serial, rebuild=True):
        self.viewer_key = serial
        label = next(
            (name for name, key in getattr(self, "_viewer_options", {}).items() if key == serial),
            "",
        )
        self.viewer_device_var.set(label or (serial or "Select an Android phone"))
        self.viewer_image = None
        self.viewer_original_size = self.preview_sizes.get(serial, (1, 1))
        self.viewer_display_size = self.viewer_original_size
        self._render_primary_screen()
        if rebuild:
            self._rebuild_follower_tiles()
        screen = self.screen_grid_labels.get(serial)
        if screen and not rebuild:
            screen.focus_set()
        elif hasattr(self, "primary_canvas"):
            self.primary_canvas.focus_set()
        self._update_active_screen_tile()
        if self.hd_window and self.hd_window.winfo_exists():
            self.hd_serial = serial
            self._render_hd_viewer()

    def open_hd_viewer(self):
        if not self.viewer_key:
            messagebox.showinfo("No Android phone", "Connect and authorize an Android phone first.")
            return
        self.hd_serial = self.viewer_key
        if self.hd_window and self.hd_window.winfo_exists():
            self.hd_window.title(f"Full-resolution Android preview — {self.hd_serial}")
            self.hd_window.deiconify()
            self.hd_window.lift()
            self._render_hd_viewer()
            if self.hd_canvas:
                self.hd_canvas.focus_set()
            return

        self.hd_window = tk.Toplevel(self, background=APP_BG)
        self.hd_window.title(f"Full-resolution Android preview — {self.hd_serial}")
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = max(600, min(1500, screen_w - 80))
        height = max(500, min(1100, screen_h - 100))
        self.hd_window.geometry(
            f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 2)}"
        )

        toolbar = ttk.Frame(self.hd_window, padding=8)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Fit to window", command=lambda: self._set_hd_view_mode("fit")).pack(side="left")
        ttk.Button(toolbar, text="100% pixels", command=lambda: self._set_hd_view_mode("actual")).pack(side="left", padx=6)
        self.hd_target_label = ttk.Label(toolbar, text="Live native-resolution video; 100% enables pixel-size viewing.")
        self.hd_target_label.pack(side="left", padx=(10, 0))

        image_area = ttk.Frame(self.hd_window)
        image_area.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.hd_canvas = tk.Canvas(image_area, background=APP_BG, highlightthickness=0)
        xbar = ttk.Scrollbar(image_area, orient="horizontal", command=self.hd_canvas.xview)
        ybar = ttk.Scrollbar(image_area, orient="vertical", command=self.hd_canvas.yview)
        self.hd_canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.hd_canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        image_area.grid_rowconfigure(0, weight=1)
        image_area.grid_columnconfigure(0, weight=1)
        self.hd_canvas.bind("<Configure>", self._hd_canvas_resized)
        self.hd_canvas.bind("<ButtonPress-1>", self._hd_pointer_press)
        self.hd_canvas.bind("<ButtonRelease-1>", self._hd_pointer_release)
        self.hd_canvas.bind("<KeyPress>", self._viewer_keypress)
        self.hd_window.protocol("WM_DELETE_WINDOW", self._close_hd_viewer)
        self.hd_window.update_idletasks()
        self._render_hd_viewer()
        self.hd_canvas.focus_set()

    def _close_hd_viewer(self):
        if self.hd_window and self.hd_window.winfo_exists():
            self.hd_window.destroy()
        self.hd_window = None
        self.hd_canvas = None
        self.hd_photo = None
        self.hd_image_item = None
        self.hd_serial = None
        self.pointer_gesture = None

    def _set_hd_view_mode(self, mode):
        self.hd_view_mode = mode
        self._render_hd_viewer()

    def _hd_canvas_resized(self, _event=None):
        if self.hd_view_mode == "fit":
            self.after_idle(self._render_hd_viewer)

    def _render_hd_viewer(self):
        if not self.hd_window or not self.hd_window.winfo_exists() or not self.hd_canvas:
            return
        serial = self.hd_serial
        payload = self.preview_payloads.get(serial)
        previous_x = self.hd_canvas.xview()[0] if self.hd_view_mode == "actual" else 0.0
        previous_y = self.hd_canvas.yview()[0] if self.hd_view_mode == "actual" else 0.0
        self.hd_canvas.delete("all")
        if not payload:
            self.hd_canvas.create_text(
                max(1, self.hd_canvas.winfo_width() // 2),
                max(1, self.hd_canvas.winfo_height() // 2),
                text="Waiting for the phone's native-resolution screenshot…",
                fill="white", anchor="center"
            )
            self.hd_canvas.configure(scrollregion=self.hd_canvas.bbox("all"))
            return

        encoded, source_w, source_h = payload
        canvas_w = max(1, self.hd_canvas.winfo_width() - 24)
        canvas_h = max(1, self.hd_canvas.winfo_height() - 24)
        self.hd_scale = min(canvas_w / source_w, canvas_h / source_h, 1.0) if self.hd_view_mode == "fit" else 1.0

        try:
            raw = base64.b64decode(encoded)
            if Image is not None and ImageTk is not None:
                import io
                with Image.open(io.BytesIO(raw)) as source:
                    image = source.convert("RGB")
                if self.hd_scale < 1.0:
                    display_size = (
                        max(1, round(source_w * self.hd_scale)),
                        max(1, round(source_h * self.hd_scale)),
                    )
                    resampling = getattr(Image, "Resampling", Image).LANCZOS
                    image = image.resize(display_size, resampling)
                self.hd_photo = ImageTk.PhotoImage(image)
                display_w, display_h = image.size
            else:
                image = tk.PhotoImage(data=encoded, format="png")
                if self.hd_scale < 1.0:
                    factor = max(
                        1,
                        math.ceil(source_w / canvas_w),
                        math.ceil(source_h / canvas_h),
                    )
                    image = image.subsample(factor, factor)
                    self.hd_scale = 1 / factor
                self.hd_photo = image
                display_w, display_h = image.width(), image.height()
        except Exception as exc:
            self.hd_canvas.create_text(
                12, 12, text=f"Could not decode screenshot: {exc}", fill="white", anchor="nw"
            )
            return

        canvas_w = max(1, self.hd_canvas.winfo_width())
        canvas_h = max(1, self.hd_canvas.winfo_height())
        offset_x = max(0, (canvas_w - display_w) // 2) if self.hd_view_mode == "fit" else 0
        offset_y = max(0, (canvas_h - display_h) // 2) if self.hd_view_mode == "fit" else 0
        self.hd_offset = (offset_x, offset_y)
        self.hd_image_item = self.hd_canvas.create_image(
            offset_x, offset_y, image=self.hd_photo, anchor="nw"
        )
        self.hd_canvas.configure(
            scrollregion=(
                0, 0, max(canvas_w, offset_x + display_w), max(canvas_h, offset_y + display_h)
            )
        )
        if self.hd_view_mode == "actual":
            self.hd_canvas.xview_moveto(previous_x)
            self.hd_canvas.yview_moveto(previous_y)
        phone = self.phones.get("android:" + serial) if serial else None
        title = f"{phone.label} ({serial})" if phone else (serial or "Android")
        self.hd_window.title(f"Full-resolution Android preview — {title}")
        percent = round(self.hd_scale * 100)
        self.hd_target_label.configure(
            text=f"{source_w} × {source_h} native pixels · showing at {percent}% · taps/swipes/keys use selected-phone mirroring"
        )

    def _hd_point_from_event(self, event, clamp=False):
        if not self.hd_canvas or not self.hd_serial:
            return None
        source_w, source_h = self.preview_sizes.get(self.hd_serial, (0, 0))
        if not source_w or not source_h or not self.hd_scale:
            return None
        x = (self.hd_canvas.canvasx(event.x) - self.hd_offset[0]) / self.hd_scale
        y = (self.hd_canvas.canvasy(event.y) - self.hd_offset[1]) / self.hd_scale
        if clamp:
            x = max(0, min(source_w - 1, int(x)))
            y = max(0, min(source_h - 1, int(y)))
        elif not (0 <= x < source_w and 0 <= y < source_h):
            return None
        return int(x), int(y)

    def _hd_pointer_press(self, event):
        if self.viewer_key != self.hd_serial:
            self._select_viewer_by_key(self.hd_serial)
        self.hd_canvas.focus_set()
        point = self._hd_point_from_event(event)
        self.pointer_gesture = None
        if point:
            self.pointer_gesture = {
                "serial": self.hd_serial,
                "start": point,
                "root": (event.x_root, event.y_root),
                "pressed_at": time.monotonic(),
            }
        return "break"

    def _hd_pointer_release(self, event):
        gesture = self.pointer_gesture
        self.pointer_gesture = None
        if not gesture or gesture["serial"] != self.hd_serial:
            return "break"
        end = self._hd_point_from_event(event, clamp=True)
        start = gesture["start"]
        moved = (
            abs(event.x_root - gesture["root"][0]) >= 5
            or abs(event.y_root - gesture["root"][1]) >= 5
        )
        held_ms = max(1, int((time.monotonic() - gesture["pressed_at"]) * 1000))
        if moved and end:
            self._dispatch_pointer_action(self.hd_serial, start, end, max(100, min(1500, held_ms)))
        elif held_ms >= 500:
            self._dispatch_pointer_action(
                self.hd_serial, start, start, max(500, min(5000, held_ms))
            )
        else:
            self._dispatch_pointer_action(self.hd_serial, start)
        self.hd_canvas.focus_set()
        return "break"

    def open_scrcpy(self, serial):
        adb = shutil.which("adb")
        scrcpy = shutil.which("scrcpy")
        if not adb:
            messagebox.showerror("ADB not found", "Install Android Platform Tools and restart the app.")
            return
        if not scrcpy:
            messagebox.showerror("scrcpy not found", "Install it with: brew install scrcpy")
            return
        rc, out, err = self.run_cmd([adb, "-s", serial, "get-state"], timeout=5)
        if rc != 0 or out.strip() != "device":
            messagebox.showwarning(
                "Phone not ready",
                f"ADB cannot use {serial} yet. Unlock the phone and approve USB debugging."
            )
            return
        try:
            subprocess.Popen([
                scrcpy, "-s", serial, "--window-title", f"Phone Control — {serial}",
                "--max-size", "1200"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.status.set(f"Opened scrcpy control window for {serial}.")
        except Exception as exc:
            messagebox.showerror("Could not open scrcpy", str(exc))

    def _selection_changed(self, key, var):
        if key in self.phones:
            self.phones[key].selected = bool(var.get())
        self.update_selected_count()

    def update_selected_count(self):
        n = sum(p.selected for p in self.phones.values())
        self.selected_stat.config(text=f"Selected: {n}")

    def select_all(self):
        for phone in self.phones.values():
            phone.selected = True
        self.render_devices()
        self.update_selected_count()

    def select_none(self):
        for phone in self.phones.values():
            phone.selected = False
        self.render_devices()
        self.update_selected_count()

    def selected_androids(self):
        return [p for p in self.phones.values() if p.selected]

    def run_bulk_android(self, action, label, package=None, phones=None, on_complete=None):
        phones = list(phones) if phones is not None else self.selected_androids()
        phones = [phone for phone in phones if phone.state.lower() == "device"]
        if not phones:
            messagebox.showinfo("No Android devices selected", "Select one or more authorized Android phones.")
            return False
        adb = shutil.which("adb")
        if not adb:
            messagebox.showerror("ADB not installed", "Install Android Platform Tools, then restart the app.")
            return False
        if action == "launch" and not package:
            messagebox.showerror("Choose an app", "Use one of the app buttons to choose what to open.")
            return False

        self.status.set(f"Running {label.lower()} on {len(phones)} Android device(s)…")
        def worker():
            successes, failures = 0, []
            for phone in phones:
                if action == "wake":
                    args = [adb, "-s", phone.key, "shell", "input", "keyevent", "224"]
                else:
                    args = [
                        adb, "-s", phone.key, "shell", "monkey", "-p", package,
                        "-c", "android.intent.category.LAUNCHER", "1"
                    ]
                rc, out, err = self.run_cmd(args, timeout=12)
                if rc == 0:
                    successes += 1
                else:
                    failures.append(f"{phone.label}: {err or out or 'command failed'}")
            if action == "wake":
                msg = (
                    f"Wake key sent to {successes}/{len(phones)} Android devices. "
                    "ADB cannot confirm that each display is awake or unlocked."
                )
            else:
                msg = f"{label} finished: {successes}/{len(phones)} Android devices."
            if failures:
                msg += " Issues: " + "; ".join(failures[:4])
            if not self.closing:
                def finish_bulk_action():
                    self.status.set(msg)
                    if on_complete:
                        on_complete(successes, len(phones))
                self.after(0, finish_bulk_action)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def wake_selected(self):
        self.run_bulk_android("wake", "Wake screens")

    def launch_selected_app(self, app_name, package):
        self.run_bulk_android("launch", f"Open {app_name}", package=package)

    def start_manual_session(self):
        if self.session_active:
            return
        phones = [
            phone for phone in self.selected_androids()
            if phone.state.lower() == "device"
        ]
        if not phones:
            messagebox.showinfo("No Android devices selected", "Select one or more authorized Android phones first.")
            return
        self.session_phones = list(phones)
        self.session_active = True
        self.session_paused = False
        self.session_index = -1
        self.launch_manual_session_app(0)

    def launch_manual_session_app(self, index):
        if self.session_tick_id:
            self.after_cancel(self.session_tick_id)
            self.session_tick_id = None
        if index >= len(self.session_apps):
            self.stop_manual_session(completed=True)
            return
        self.session_index = index
        self.session_seconds_remaining = 180
        self.session_paused = False
        self.session_launching = True
        app_name, package = self.session_apps[index]
        self.session_info_var.set(f"Opening {app_name} on {len(self.session_phones)} selected phone(s)…")
        self._render_manual_session()
        started = self.run_bulk_android(
            "launch", f"Open {app_name}", package=package,
            phones=self.session_phones,
            on_complete=self._manual_session_app_opened,
        )
        if not started:
            self.session_active = False
            self.session_launching = False
            self.session_info_var.set("Session could not start. Check the selected phones and ADB.")
            self._render_manual_session()

    def _manual_session_app_opened(self, successes, total):
        if not self.session_active:
            return
        self.session_launching = False
        if successes <= 0:
            self.session_active = False
            self.session_phones = []
            self.session_info_var.set("App did not open on any selected phone; session stopped.")
            self._render_manual_session()
            return
        app_name = self.session_apps[self.session_index][0]
        failed = total - successes
        suffix = f" · {failed} phone(s) failed" if failed else ""
        self.session_info_var.set(f"{app_name} · use manually; timer ends before the next app{suffix}")
        self._render_manual_session()
        self.session_tick_id = self.after(1000, self._tick_manual_session)

    def _tick_manual_session(self):
        self.session_tick_id = None
        if not self.session_active or self.session_paused:
            return
        if self.session_seconds_remaining > 0:
            self.session_seconds_remaining -= 1
        self._render_manual_session()
        if self.session_seconds_remaining <= 0:
            app_name = self.session_apps[self.session_index][0]
            self.session_info_var.set(f"3 minutes complete for {app_name}. Press Next app when ready.")
            return
        self.session_tick_id = self.after(1000, self._tick_manual_session)

    def next_manual_session_app(self):
        if (
            not self.session_active
            or self.session_launching
            or self.session_seconds_remaining > 0
        ):
            return
        if self.session_index + 1 >= len(self.session_apps):
            self.stop_manual_session(completed=True)
            return
        self.launch_manual_session_app(self.session_index + 1)

    def toggle_manual_session_pause(self):
        if not self.session_active or self.session_launching or self.session_seconds_remaining <= 0:
            return
        self.session_paused = not self.session_paused
        if self.session_paused:
            if self.session_tick_id:
                self.after_cancel(self.session_tick_id)
                self.session_tick_id = None
        else:
            self.session_tick_id = self.after(1000, self._tick_manual_session)
        self._render_manual_session()

    def stop_manual_session(self, completed=False):
        if self.session_tick_id:
            self.after_cancel(self.session_tick_id)
            self.session_tick_id = None
        self.session_active = False
        self.session_paused = False
        self.session_launching = False
        self.session_index = -1
        self.session_seconds_remaining = 180
        self.session_phones = []
        self.session_info_var.set(
            "Session complete." if completed else "Session stopped. Start again when ready."
        )
        self._render_manual_session()

    def _render_manual_session(self):
        minutes, seconds = divmod(max(0, self.session_seconds_remaining), 60)
        if self.session_active and self.session_index >= 0:
            self.session_timer_var.set(f"{minutes}:{seconds:02d}")
            next_name = (
                self.session_apps[self.session_index + 1][0]
                if self.session_index + 1 < len(self.session_apps)
                else "Finish"
            )
            self.session_next_button.config(
                text=f"Next: {next_name}" if next_name != "Finish" else "Finish",
                state="disabled" if self.session_launching or self.session_seconds_remaining > 0 else "normal",
            )
            self.session_pause_button.config(
                text="Resume" if self.session_paused else "Pause",
                state="normal" if not self.session_launching and self.session_seconds_remaining > 0 else "disabled",
            )
            self.session_stop_button.config(state="normal")
            self.session_start_button.config(state="disabled")
        else:
            self.session_timer_var.set("3:00 each")
            self.session_start_button.config(state="normal")
            self.session_next_button.config(state="disabled", text="Next app")
            self.session_pause_button.config(state="disabled", text="Pause")
            self.session_stop_button.config(state="disabled")

    def on_close(self):
        self.closing = True
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    app = ControlCenter()
    app.mainloop()
