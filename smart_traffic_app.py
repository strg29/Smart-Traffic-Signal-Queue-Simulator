#!/usr/bin/env python3
"""
Smart Traffic Signal Queue Simulator
====================================
Industry focus : Smart Cities
Data structure : Queue (collections.deque)
Principle      : FIFO (First In, First Out)
Integrated CO  : CO1

Architecture (layers, top to bottom)
------------------------------------
  TrafficApp          Tkinter GUI: widgets, drawing, animation, event handlers
  SimulationEngine    Orchestrates queues + signal + statistics + timing (no GUI code)
  QueueManager        Four independent FIFO deques (append = enqueue, popleft = dequeue)
  SignalController    NORTH -> EAST -> SOUTH -> WEST cycle and countdown
  Statistics          Processed count, average waiting time, peak queue size, status

Run the application :  python smart_traffic_app.py
Run the self-tests  :  python smart_traffic_app.py --test      (no GUI needed)
"""

from __future__ import annotations

import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # allows the headless self-tests to run without Tk
    tk = None
    ttk = None

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DIRECTIONS = ("NORTH", "EAST", "SOUTH", "WEST")  # signal cycle order
VEHICLE_TYPES = ("Car", "Bike", "Bus", "Truck", "Auto")

DEFAULT_GREEN_SECONDS = 10
DEFAULT_YELLOW_SECONDS = 2
DEFAULT_PROCESS_INTERVAL = 1.5  # seconds between automatic dequeues

# (length px, width px, body colour) -- length runs along the lane
VEHICLE_SPECS = {
    "Car": (38, 22, "#2563eb"),
    "Bike": (26, 10, "#f97316"),
    "Bus": (52, 26, "#16a34a"),
    "Truck": (50, 26, "#b45309"),
    "Auto": (32, 20, "#facc15"),
}

# Light theme palette
BG = "#f1f5f9"
PANEL = "#ffffff"
BORDER = "#d7dee8"
TEXT = "#1e293b"
MUTED = "#64748b"
ACCENT = "#2563eb"
GRASS = "#e3efdc"
ROAD = "#4b5563"

STATUS_COLORS = {
    "LOW": "#15803d",
    "MODERATE": "#b45309",
    "HIGH": "#c2410c",
    "HEAVY": "#b91c1c",
}


# --------------------------------------------------------------------------- #
# Model layer
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Vehicle:
    """A vehicle waiting in (or leaving) a queue."""

    number: str
    vtype: str
    direction: str
    arrival: float  # engine time (seconds) at which it joined the queue


@dataclass
class ProcessResult:
    vehicle: Optional[Vehicle]
    wait: float
    direction: str
    message: str
    blocked: bool = False  # True when the signal phase (yellow) forbids processing


class QueueManager:
    """Four independent FIFO queues, one per direction."""

    def __init__(self) -> None:
        self.queues = {d: deque() for d in DIRECTIONS}
        self._registered: set[str] = set()  # numbers currently waiting

    def enqueue(self, vehicle: Vehicle) -> int:
        """Add to the REAR of the vehicle's direction queue; returns its position."""
        queue = self.queues[vehicle.direction]
        queue.append(vehicle)
        self._registered.add(vehicle.number)
        return len(queue)

    def dequeue(self, direction: str) -> Optional[Vehicle]:
        """Remove from the FRONT of the given direction queue (None if empty)."""
        queue = self.queues[direction]
        if not queue:
            return None
        vehicle = queue.popleft()
        self._registered.discard(vehicle.number)
        return vehicle

    def contains(self, number: str) -> bool:
        return number in self._registered

    def size(self, direction: str) -> int:
        return len(self.queues[direction])

    def total(self) -> int:
        return sum(len(q) for q in self.queues.values())

    def snapshot(self, direction: str) -> list[Vehicle]:
        """Front-to-rear copy of a queue (for display only)."""
        return list(self.queues[direction])

    def clear(self) -> None:
        for queue in self.queues.values():
            queue.clear()
        self._registered.clear()


class Statistics:
    """Running traffic statistics."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.processed = 0
        self.total_wait = 0.0
        self.peak = 0

    def record_processed(self, wait: float) -> None:
        self.processed += 1
        self.total_wait += wait

    def observe_total(self, total_waiting: int) -> None:
        self.peak = max(self.peak, total_waiting)

    @property
    def average_wait(self) -> float:
        """Total waiting time of processed vehicles / vehicles processed (0 if none)."""
        return self.total_wait / self.processed if self.processed else 0.0

    @staticmethod
    def traffic_status(total_waiting: int) -> str:
        if total_waiting <= 5:
            return "LOW"
        if total_waiting <= 12:
            return "MODERATE"
        if total_waiting <= 20:
            return "HIGH"
        return "HEAVY"


class SignalController:
    """One direction at a time: GREEN, then YELLOW, then the next direction turns GREEN.
    Order: NORTH -> EAST -> SOUTH -> WEST."""

    def __init__(self, green_duration: int = DEFAULT_GREEN_SECONDS,
                 yellow_duration: int = DEFAULT_YELLOW_SECONDS) -> None:
        self.green_duration = green_duration
        self.yellow_duration = yellow_duration
        self.index = 0
        self.phase = "GREEN"            # GREEN | YELLOW (for the active direction)
        self.remaining = float(green_duration)

    @property
    def active(self) -> str:
        return DIRECTIONS[self.index]

    @property
    def upcoming(self) -> str:
        return DIRECTIONS[(self.index + 1) % len(DIRECTIONS)]

    @property
    def is_green(self) -> bool:
        return self.phase == "GREEN"

    def to_yellow(self) -> None:
        self.phase = "YELLOW"
        self.remaining = float(self.yellow_duration)

    def advance(self) -> None:
        self.index = (self.index + 1) % len(DIRECTIONS)
        self.phase = "GREEN"
        self.remaining = float(self.green_duration)

    def reset(self) -> None:
        self.index = 0
        self.phase = "GREEN"
        self.remaining = float(self.green_duration)


class SimulationEngine:
    """GUI-independent core: queues + signal + statistics + timing."""

    NUMBER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9 \-]*$")

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.queues = QueueManager()
        self.stats = Statistics()
        self.signal = SignalController()
        self.process_interval = DEFAULT_PROCESS_INTERVAL
        self._paused_since: Optional[float] = None
        self._paused_total = 0.0
        self._accum = self.process_interval  # first vehicle may go immediately

    # ---- time (paused periods do not count as waiting time) --------------- #
    def now(self) -> float:
        reference = self._paused_since if self._paused_since is not None else self._clock()
        return reference - self._paused_total

    def pause(self) -> None:
        if self._paused_since is None:
            self._paused_since = self._clock()

    def resume(self) -> None:
        if self._paused_since is not None:
            self._paused_total += self._clock() - self._paused_since
            self._paused_since = None

    def set_timing(self, green_seconds: int, process_interval: float,
                   yellow_seconds: Optional[int] = None) -> None:
        sig = self.signal
        if yellow_seconds is None:
            yellow_seconds = sig.yellow_duration
        old_full = sig.green_duration if sig.is_green else sig.yellow_duration
        new_full = green_seconds if sig.is_green else yellow_seconds
        if abs(sig.remaining - old_full) < 1e-9:  # phase not started yet
            sig.remaining = float(new_full)
        else:
            sig.remaining = min(sig.remaining, float(new_full))
        sig.green_duration = green_seconds
        sig.yellow_duration = yellow_seconds
        self.process_interval = process_interval

    # ---- enqueue ----------------------------------------------------------- #
    def validate_number(self, raw: str) -> tuple[Optional[str], str]:
        """Return (clean_number, error_message)."""
        number = " ".join(raw.strip().upper().split())
        if not number:
            return None, "Vehicle number cannot be empty."
        if len(number) > 15:
            return None, "Vehicle number is too long (max 15 characters)."
        if not self.NUMBER_PATTERN.match(number):
            return None, "Use only letters, digits, spaces or hyphens."
        return number, ""

    def add_vehicle(self, raw_number: str, vtype: str, direction: str) -> tuple[Optional[Vehicle], str]:
        number, error = self.validate_number(raw_number)
        if number is None:
            return None, error
        if vtype not in VEHICLE_TYPES:
            return None, "Select a valid vehicle type."
        if direction not in DIRECTIONS:
            return None, "Select a valid direction."
        if self.queues.contains(number):
            return None, f"Vehicle {number} is already waiting in a queue."
        vehicle = Vehicle(number, vtype, direction, self.now())
        position = self.queues.enqueue(vehicle)
        self.stats.observe_total(self.queues.total())
        return vehicle, f"{number} ({vtype}) joined the {direction} queue at position {position}."

    # ---- dequeue ----------------------------------------------------------- #
    def process_next(self) -> ProcessResult:
        """Dequeue from the queue that currently has the GREEN signal - never another.
        During the YELLOW phase nothing is processed."""
        direction = self.signal.active
        if not self.signal.is_green:
            return ProcessResult(None, 0.0, direction,
                                 f"{direction} signal is YELLOW - vehicles must stop.", blocked=True)
        vehicle = self.queues.dequeue(direction)
        if vehicle is None:
            return ProcessResult(None, 0.0, direction, f"{direction} queue is empty - nothing to process.")
        wait = max(0.0, self.now() - vehicle.arrival)
        self.stats.record_processed(wait)
        return ProcessResult(
            vehicle, wait, direction,
            f"{vehicle.number} ({vehicle.vtype}) left {direction} after waiting {wait:.1f}s.",
        )

    # ---- simulation step ------------------------------------------------- #
    def tick(self, dt: float) -> list[tuple]:
        """Advance the simulation by dt seconds. Returns events for the GUI to display."""
        events: list[tuple] = []
        sig = self.signal
        sig.remaining -= dt
        self._accum += dt
        if sig.is_green and self._accum >= self.process_interval:
            self._accum = 0.0
            result = self.process_next()
            if result.vehicle is not None:  # empty queues are skipped silently when automatic
                events.append(("processed", result))
        if sig.remaining <= 0:
            if sig.is_green and sig.yellow_duration > 0:
                sig.to_yellow()
                events.append(("yellow", sig.active))
            else:
                old = sig.active
                sig.advance()
                self._accum = self.process_interval  # new green phase: first vehicle goes at once
                events.append(("signal", old, sig.active))
        return events

    def reset(self) -> None:
        self.queues.clear()
        self.stats.reset()
        self.signal.reset()
        self._paused_since = None
        self._paused_total = 0.0
        self._accum = self.process_interval


# --------------------------------------------------------------------------- #
# GUI layer
# --------------------------------------------------------------------------- #
class TrafficApp:
    ROAD_W = 110                     # full road width (two lanes of 55 px)
    SLOT = 56                        # length of one queue slot along the lane
    START = ROAD_W // 2 + 6          # distance from centre to first slot (stop line + gap)
    TICK_MS = 100                    # simulation timer period
    ANIM_MS = 25                     # animation frame period
    ANIM_SPEED = 400.0               # px per second for departing vehicles

    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.engine = SimulationEngine()
        self.state = "STOPPED"       # STOPPED | RUNNING | PAUSED
        self._tick_id: Optional[str] = None
        self._last_tick = 0.0
        self._anim_id: Optional[str] = None
        self._anims: list[dict] = []
        self._ever_started = False
        self.cv_w, self.cv_h = 800, 560
        self._origin: Optional[tuple[float, float]] = None  # drawing-origin override (legend icons)

        root.title("Smart Traffic Signal Queue Simulator")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1320, sw - 40)}x{min(860, sh - 80)}")
        root.minsize(1100, 680)
        root.configure(bg=BG)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        self._build_style()
        self._build_header()
        self._build_body()
        self._build_bottom()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._set_state("STOPPED")
        self.log("Application ready. Add vehicles, then press Start Simulation.", "info")
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TLabelframe", background=PANEL, bordercolor=BORDER)
        style.configure("Card.TLabelframe.Label", background=PANEL, foreground=ACCENT,
                        font=("Segoe UI", 10, "bold"))
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("TEntry", fieldbackground="#ffffff", padding=4)
        style.configure("TCombobox", padding=3)
        for name, colour, hover in (
            ("Primary", ACCENT, "#1d4ed8"),
            ("Success", "#16a34a", "#15803d"),
            ("Warn", "#d97706", "#b45309"),
            ("Danger", "#dc2626", "#b91c1c"),
            ("Neutral", "#475569", "#334155"),
        ):
            style.configure(f"{name}.TButton", background=colour, foreground="#ffffff",
                            font=("Segoe UI", 10, "bold"), padding=(10, 6), borderwidth=0)
            style.map(f"{name}.TButton",
                      background=[("disabled", "#cbd5e1"), ("active", hover)],
                      foreground=[("disabled", "#f8fafc")])
        style.configure("Green.Horizontal.TProgressbar", troughcolor="#e2e8f0",
                        background="#22c55e", bordercolor="#e2e8f0", lightcolor="#22c55e",
                        darkcolor="#22c55e")
        style.configure("Yellow.Horizontal.TProgressbar", troughcolor="#e2e8f0",
                        background="#f59e0b", bordercolor="#e2e8f0", lightcolor="#f59e0b",
                        darkcolor="#f59e0b")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=PANEL)
        header.grid(row=0, column=0, sticky="ew")
        tk.Frame(header, bg=ACCENT, width=8).pack(side="left", fill="y")
        icon = tk.Canvas(header, width=44, height=62, bg=PANEL, highlightthickness=0)
        icon.pack(side="left", padx=(14, 0), pady=6)
        icon.create_rectangle(10, 4, 34, 58, fill="#1f2937", outline="#0f172a", width=2)
        for cy, colour in ((14, "#ef4444"), (31, "#fbbf24"), (48, "#22c55e")):
            icon.create_oval(15, cy - 6, 29, cy + 8, fill=colour, outline="")
        titles = tk.Frame(header, bg=PANEL)
        titles.pack(side="left", padx=14, pady=8)
        tk.Label(titles, text="Smart Traffic Signal Queue Simulator", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(titles, text="Smart Cities  |  Queue (FIFO)  |  Python deque  |  CO1",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w")
        self.status_lbl = tk.Label(header, text="", font=("Segoe UI", 11, "bold"),
                                   padx=14, pady=6)
        self.status_lbl.pack(side="right", padx=16)
        tk.Label(header, text="System status:", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="right")
        tk.Frame(self.root, bg=BORDER, height=1).grid(row=0, column=0, sticky="sew")

    def _build_body(self) -> None:
        body = ttk.Frame(self.root)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(10, 6))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- intersection canvas ---- #
        holder = tk.Frame(body, bg=BORDER, padx=1, pady=1)
        holder.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.canvas = tk.Canvas(holder, bg=GRASS, highlightthickness=0, width=640, height=440)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # ---- right-hand control column ---- #
        side = ttk.Frame(body, width=350)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(True)

        # Vehicle entry
        entry_box = ttk.LabelFrame(side, text="Vehicle Entry", style="Card.TLabelframe", padding=8)
        entry_box.pack(fill="x", pady=(0, 8))
        for col in (1, 3):
            entry_box.columnconfigure(col, weight=1)
        ttk.Label(entry_box, text="Vehicle No.", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.num_var = tk.StringVar()
        self.num_entry = ttk.Entry(entry_box, textvariable=self.num_var, font=("Consolas", 11))
        self.num_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=3)
        self.num_entry.bind("<Return>", lambda _e: self.on_add())
        ttk.Label(entry_box, text="Type", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.type_var = tk.StringVar(value=VEHICLE_TYPES[0])
        ttk.Combobox(entry_box, textvariable=self.type_var, values=VEHICLE_TYPES, width=8,
                     state="readonly").grid(row=1, column=1, sticky="ew", padx=(6, 8), pady=3)
        ttk.Label(entry_box, text="Direction", style="Card.TLabel").grid(row=1, column=2, sticky="w")
        self.dir_var = tk.StringVar(value=DIRECTIONS[0])
        ttk.Combobox(entry_box, textvariable=self.dir_var, values=DIRECTIONS, width=8,
                     state="readonly").grid(row=1, column=3, sticky="ew", padx=(6, 0), pady=3)
        ttk.Button(entry_box, text="\uff0b  Add Vehicle", style="Primary.TButton",
                   command=self.on_add).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0), padx=(0, 4))
        ttk.Button(entry_box, text="\u2684  Random Traffic", style="Neutral.TButton",
                   command=self.on_random).grid(row=2, column=2, columnspan=2, sticky="ew", pady=(6, 0), padx=(4, 0))

        # Simulation controls
        sim_box = ttk.LabelFrame(side, text="Simulation Control", style="Card.TLabelframe", padding=8)
        sim_box.pack(fill="x", pady=(0, 8))
        sim_box.columnconfigure((0, 1), weight=1)
        self.start_btn = ttk.Button(sim_box, text="\u25b6  Start Simulation", style="Success.TButton", command=self.on_start)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
        self.pause_btn = ttk.Button(sim_box, text="\u275a\u275a  Pause", style="Warn.TButton", command=self.on_pause_toggle)
        self.pause_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=2)
        self.process_btn = ttk.Button(sim_box, text="\u279c  Process Next Vehicle", style="Primary.TButton",
                                      command=self.on_process)
        self.process_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=2)
        self.reset_btn = ttk.Button(sim_box, text="\u21bb  Reset", style="Danger.TButton", command=self.on_reset)
        self.reset_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=2)

        timing = tk.Frame(sim_box, bg=PANEL)
        timing.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.green_var = tk.StringVar(value=str(DEFAULT_GREEN_SECONDS))
        self.yellow_var = tk.StringVar(value=str(DEFAULT_YELLOW_SECONDS))
        self.interval_var = tk.StringVar(value=str(DEFAULT_PROCESS_INTERVAL))
        for col, (caption, var, lo, hi, inc) in enumerate((
                ("Green (s)", self.green_var, 3, 60, 1),
                ("Yellow (s)", self.yellow_var, 0, 10, 1),
                ("Interval (s)", self.interval_var, 0.5, 5.0, 0.5))):
            pad = (0, 0) if col == 0 else (10, 0)
            ttk.Label(timing, text=caption, style="Card.TLabel").grid(row=0, column=col, sticky="w", padx=pad)
            spin = ttk.Spinbox(timing, from_=lo, to=hi, increment=inc, width=6,
                               textvariable=var, command=self._apply_settings)
            spin.grid(row=1, column=col, sticky="w", padx=pad)
            spin.bind("<Return>", self._apply_settings)
            spin.bind("<FocusOut>", self._apply_settings)

        self.msg_lbl = tk.Label(sim_box, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left",
                                wraplength=310, height=2, font=("Segoe UI", 9))
        self.msg_lbl.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # Signal and statistics
        stat_box = ttk.LabelFrame(side, text="Signal & Live Statistics", style="Card.TLabelframe", padding=8)
        stat_box.pack(fill="both", expand=True)
        stat_box.columnconfigure((0, 1, 2), weight=1, uniform="stat")

        sig = tk.Frame(stat_box, bg=PANEL)
        sig.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.sig_dot = tk.Label(sig, text="\u25cf", fg="#22c55e", bg=PANEL, font=("Segoe UI", 18))
        self.sig_dot.pack(side="left")
        self.sig_lbl = tk.Label(sig, text="NORTH  GREEN", bg=PANEL, fg=TEXT, font=("Segoe UI", 14, "bold"))
        self.sig_lbl.pack(side="left", padx=(2, 0))
        self.count_lbl = tk.Label(sig, text="10 s", bg=PANEL, fg=ACCENT, font=("Consolas", 20, "bold"))
        self.count_lbl.pack(side="right")
        self.next_lbl = tk.Label(stat_box, text="Next: EAST", bg=PANEL, fg=MUTED, anchor="w")
        self.next_lbl.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.progress = ttk.Progressbar(stat_box, style="Green.Horizontal.TProgressbar",
                                        maximum=DEFAULT_GREEN_SECONDS, value=DEFAULT_GREEN_SECONDS)
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(2, 6))

        self.stat_vals: dict[str, tk.Label] = {}
        cells = (("waiting", "Waiting"), ("processed", "Processed"),
                 ("average", "Avg wait"), ("peak", "Peak queue"), ("status", "Traffic"))
        for i, (key, caption) in enumerate(cells):
            cell = tk.Frame(stat_box, bg="#f8fafc", highlightbackground=BORDER, highlightthickness=1)
            cell.grid(row=3 + i // 3, column=i % 3, columnspan=2 if key == "status" else 1,
                      sticky="ew", padx=2, pady=2)
            tk.Label(cell, text=caption, bg="#f8fafc", fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=8)
            value = tk.Label(cell, text="0", bg="#f8fafc", fg=TEXT, font=("Segoe UI", 13, "bold"))
            value.pack(anchor="w", padx=8, pady=(0, 2))
            self.stat_vals[key] = value

    def _build_bottom(self) -> None:
        bottom = ttk.Frame(self.root)
        bottom.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        for col in range(4):
            bottom.columnconfigure(col, weight=1, uniform="queue")
        bottom.columnconfigure(4, weight=2)

        self.monitor: dict[str, dict] = {}
        mono = ("Consolas", 9)
        for col, direction in enumerate(DIRECTIONS):
            card = tk.Frame(bottom, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 8))
            head = tk.Label(card, text=direction, anchor="w", bg=PANEL, fg=TEXT,
                            font=("Segoe UI", 10, "bold"), padx=8, pady=3)
            head.pack(fill="x")
            inner = tk.Frame(card, bg="#f8fafc")
            inner.pack(fill="both", expand=True, padx=4, pady=(0, 4))
            text = tk.Text(inner, width=24, height=5, font=mono, bg="#f8fafc", relief="flat",
                           wrap="none", state="disabled", bd=0, highlightthickness=0)
            scroll = ttk.Scrollbar(inner, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            text.tag_configure("front", background="#dcfce7", foreground="#166534", font=("Consolas", 9, "bold"))
            text.tag_configure("rear", background="#dbeafe", foreground="#1e40af", font=("Consolas", 9, "bold"))
            text.tag_configure("mid", foreground=TEXT)
            text.tag_configure("empty", foreground=MUTED)
            self.monitor[direction] = {"head": head, "text": text}

        log_card = tk.Frame(bottom, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        log_card.grid(row=0, column=4, sticky="nsew")
        tk.Label(log_card, text="Activity Log", anchor="w", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold"), padx=8, pady=3).pack(fill="x")
        inner = tk.Frame(log_card, bg="#f8fafc")
        inner.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.log_text = tk.Text(inner, width=40, height=5, font=("Consolas", 9), bg="#f8fafc",
                                relief="flat", wrap="word", state="disabled", bd=0, highlightthickness=0)
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, colour in (("info", "#334155"), ("ok", "#15803d"), ("warn", "#b45309"),
                            ("signal", "#1d4ed8"), ("error", "#b91c1c")):
            self.log_text.tag_configure(tag, foreground=colour)

    # ------------------------------------------------------------------ #
    # Small helpers: log, notifications, state
    # ------------------------------------------------------------------ #
    def log(self, message: str, kind: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n", kind)
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "50.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _notify(self, message: str, kind: str = "info") -> None:
        colours = {"info": MUTED, "ok": "#15803d", "warn": "#b45309", "error": "#b91c1c"}
        self.msg_lbl.configure(text=message, fg=colours.get(kind, MUTED))

    def _set_state(self, state: str) -> None:
        self.state = state
        look = {"STOPPED": ("#475569", "#e2e8f0"), "RUNNING": ("#166534", "#dcfce7"),
                "PAUSED": ("#92400e", "#fef3c7")}[state]
        self.status_lbl.configure(text=f"\u25cf {state}", fg=look[0], bg=look[1])
        self.start_btn.state(["disabled"] if state == "RUNNING" else ["!disabled"])
        self.pause_btn.configure(text="\u25b6  Resume" if state == "PAUSED" else "\u275a\u275a  Pause")
        self.pause_btn.state(["disabled"] if state == "STOPPED" else ["!disabled"])

    def _apply_settings(self, *_args) -> None:
        sig = self.engine.signal
        try:
            green = max(3, min(60, int(float(self.green_var.get()))))
        except (ValueError, tk.TclError):
            green = sig.green_duration
        try:
            yellow = max(0, min(10, int(float(self.yellow_var.get()))))
        except (ValueError, tk.TclError):
            yellow = sig.yellow_duration
        try:
            interval = max(0.5, min(5.0, float(self.interval_var.get())))
        except (ValueError, tk.TclError):
            interval = self.engine.process_interval
        self.green_var.set(str(green))
        self.yellow_var.set(str(yellow))
        self.interval_var.set(f"{interval:.1f}")
        self.engine.set_timing(green, interval, yellow)
        self._refresh_signal_panel()

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #
    def on_add(self) -> None:
        vehicle, message = self.engine.add_vehicle(self.num_var.get(), self.type_var.get(), self.dir_var.get())
        if vehicle is None:
            self._notify(message, "error")
            self.log(f"Rejected: {message}", "warn")
            return
        self.num_var.set("")
        self.num_entry.focus_set()
        self._notify(message, "ok")
        self.log(f"Vehicle {vehicle.number} ({vehicle.vtype}) added to {vehicle.direction} queue.", "ok")
        self._refresh_all()

    def on_random(self) -> None:
        added = 0
        letters = "ABCDEFGHJKLMNPRSTUVWXYZ"
        for _ in range(8):
            for _attempt in range(50):
                plate = f"TN38{random.choice(letters)}{random.choice(letters)}{random.randint(1000, 9999)}"
                if not self.engine.queues.contains(plate):
                    break
            vehicle, _msg = self.engine.add_vehicle(plate, random.choice(VEHICLE_TYPES), random.choice(DIRECTIONS))
            if vehicle is not None:
                added += 1
                self.log(f"Vehicle {vehicle.number} ({vehicle.vtype}) added to {vehicle.direction} queue.", "ok")
        self._notify(f"Added {added} random vehicles.", "ok")
        self._refresh_all()

    def on_start(self) -> None:
        if self.state == "RUNNING":          # never create a second timer
            return
        self._apply_settings()
        if self.state == "PAUSED":
            self.engine.resume()
            self.log("Simulation resumed.", "info")
        else:
            self.log("Simulation started.", "info")
        self._ever_started = True
        self._set_state("RUNNING")
        self._notify("Simulation running.", "ok")
        self._schedule_tick()

    def on_pause_toggle(self) -> None:
        if self.state == "RUNNING":
            self._cancel_tick()
            self.engine.pause()
            self._set_state("PAUSED")
            self.log("Simulation paused.", "info")
            self._notify("Paused. Queues, order and statistics are preserved.", "warn")
        elif self.state == "PAUSED":
            self.on_start()

    def on_process(self) -> None:
        direction = self.engine.signal.active
        if self._lane_clearing(direction):
            self._notify("The previous vehicle is still clearing the stop line.", "warn")
            return
        result = self.engine.process_next()
        if result.blocked:
            self._notify(result.message, "warn")
            self.log(f"Process blocked: {direction} signal is YELLOW.", "warn")
            return
        if result.vehicle is None:
            self._notify(result.message, "warn")
            self.log(f"Attempt to process empty {direction} queue.", "warn")
            return
        self._announce_processed(result)
        self._refresh_all()

    def on_reset(self) -> None:
        self._cancel_tick()
        self._anims.clear()
        if self._anim_id is not None:
            self.root.after_cancel(self._anim_id)
            self._anim_id = None
        self.engine.reset()
        self._apply_settings()
        self.num_var.set("")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._ever_started = False
        self._set_state("STOPPED")
        self.log("System reset: queues, statistics and signal restored.", "info")
        self._notify("System reset.", "info")
        self.canvas.delete("anim")
        self._refresh_all()

    def _on_close(self) -> None:
        self._cancel_tick()
        if self._anim_id is not None:
            self.root.after_cancel(self._anim_id)
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # Simulation timer (non-blocking, via after())
    # ------------------------------------------------------------------ #
    def _schedule_tick(self) -> None:
        if self._tick_id is None:
            self._last_tick = time.monotonic()
            self._tick_id = self.root.after(self.TICK_MS, self._tick)

    def _cancel_tick(self) -> None:
        if self._tick_id is not None:
            self.root.after_cancel(self._tick_id)
            self._tick_id = None

    def _tick(self) -> None:
        self._tick_id = None
        if self.state != "RUNNING":
            return
        now = time.monotonic()
        dt = min(now - self._last_tick, 0.5)
        self._last_tick = now
        events = self.engine.tick(dt)
        for event in events:
            if event[0] == "processed":
                self._announce_processed(event[1])
            elif event[0] == "yellow":
                self.log(f"Signal turning YELLOW for {event[1]}: vehicles must stop.", "warn")
            elif event[0] == "signal":
                self.log(f"Traffic signal changed: {event[1]} \u2192 {event[2]} (GREEN).", "signal")
        if events:
            self._refresh_all()
        else:
            self._refresh_signal_panel()
        self._tick_id = self.root.after(self.TICK_MS, self._tick)

    def _announce_processed(self, result: ProcessResult) -> None:
        vehicle = result.vehicle
        self.log(f"Vehicle {vehicle.number} ({vehicle.vtype}) processed from {result.direction}; "
                 f"waited {result.wait:.1f}s.", "ok")
        self._notify(result.message, "ok")
        self._spawn_anim(vehicle)

    # ------------------------------------------------------------------ #
    # Refresh (text widgets + canvas)
    # ------------------------------------------------------------------ #
    def _refresh_all(self) -> None:
        self._refresh_stats()
        self._refresh_signal_panel()
        self._update_monitor()
        self._draw_signals()
        self._draw_queues()

    def _refresh_stats(self) -> None:
        stats, total = self.engine.stats, self.engine.queues.total()
        status = Statistics.traffic_status(total)
        self.stat_vals["waiting"].configure(text=str(total))
        self.stat_vals["processed"].configure(text=str(stats.processed))
        self.stat_vals["average"].configure(text=f"{stats.average_wait:.2f} s")
        self.stat_vals["peak"].configure(text=str(stats.peak))
        self.stat_vals["status"].configure(text=status, fg=STATUS_COLORS[status])

    def _refresh_signal_panel(self) -> None:
        sig = self.engine.signal
        green = sig.is_green
        self.sig_lbl.configure(text=f"{sig.active}  {sig.phase}")
        self.sig_dot.configure(fg="#22c55e" if green else "#f59e0b")
        self.next_lbl.configure(
            text=f"Next: {sig.upcoming}   |   green {sig.green_duration}s + yellow {sig.yellow_duration}s")
        self.count_lbl.configure(text=f"{max(0, int(sig.remaining + 0.999))} s")
        full = sig.green_duration if green else sig.yellow_duration
        self.progress.configure(
            style="Green.Horizontal.TProgressbar" if green else "Yellow.Horizontal.TProgressbar",
            maximum=max(full, 1), value=max(0.0, sig.remaining))

    def _update_monitor(self) -> None:
        active = self.engine.signal.active
        for direction, widgets in self.monitor.items():
            items = self.engine.queues.snapshot(direction)
            is_active = direction == active
            if is_active and self.engine.signal.is_green:
                tag, bg, fg = "  [GREEN]", "#dcfce7", "#166534"
            elif is_active:
                tag, bg, fg = "  [YELLOW]", "#fef3c7", "#92400e"
            else:
                tag, bg, fg = "", PANEL, TEXT
            widgets["head"].configure(text=f"{direction}  \u2022  {len(items)} waiting{tag}", bg=bg, fg=fg)
            text = widgets["text"]
            text.configure(state="normal")
            text.delete("1.0", "end")
            if not items:
                text.insert("end", "(empty)\n", "empty")
            last = len(items) - 1
            for i, vehicle in enumerate(items):
                if i == 0:
                    label, tag = "FRONT", "front"
                elif i == last:
                    label, tag = "REAR ", "rear"
                else:
                    label, tag = f"{i + 1:>4} ", "mid"
                text.insert("end", f"{label} {vehicle.number} {vehicle.vtype}\n", tag)
            text.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Drawing: geometry helpers
    # ------------------------------------------------------------------ #
    def _on_canvas_resize(self, event) -> None:
        self.cv_w, self.cv_h = max(event.width, 300), max(event.height, 300)
        self._draw_background()
        self._draw_legend()
        self._draw_signals()
        self._draw_queues()
        self._draw_anims()

    def _extent(self, direction: str) -> float:
        return self.cv_h / 2 if direction in ("NORTH", "SOUTH") else self.cv_w / 2

    def _pt(self, direction: str, u: float, v: float) -> tuple[float, float]:
        """Local frame -> canvas. u: distance from centre along the approach (outward +),
        v: sideways offset (+ is the approach lane, left-hand traffic)."""
        cx, cy = self._origin if self._origin else (self.cv_w / 2, self.cv_h / 2)
        if direction == "NORTH":
            return cx + v, cy - u
        if direction == "SOUTH":
            return cx - v, cy + u
        if direction == "EAST":
            return cx + u, cy + v
        return cx - u, cy - v  # WEST

    def _box(self, direction, u0, u1, v0, v1) -> tuple[float, float, float, float]:
        x0, y0 = self._pt(direction, u0, v0)
        x1, y1 = self._pt(direction, u1, v1)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    @staticmethod
    def _reserves(direction: str) -> tuple[int, int]:
        """(road-name label reserve, '+N' badge reserve) along the lane, in px."""
        return (24, 22) if direction in ("NORTH", "SOUTH") else (68, 44)

    def _max_visible(self, direction: str) -> int:
        label_res, badge_res = self._reserves(direction)
        available = self._extent(direction) - label_res - badge_res - self.START
        return max(1, int(available // self.SLOT))

    # ------------------------------------------------------------------ #
    # Drawing: static scene, signals, queued vehicles, animation
    # ------------------------------------------------------------------ #
    def _draw_background(self) -> None:
        c = self.canvas
        c.delete("bg")
        w, h, r = self.cv_w, self.cv_h, self.ROAD_W / 2
        cx, cy = w / 2, h / 2
        c.create_rectangle(0, 0, w, h, fill=GRASS, outline="", tags="bg")
        c.create_rectangle(cx - r, 0, cx + r, h, fill=ROAD, outline="", tags="bg")
        c.create_rectangle(0, cy - r, w, cy + r, fill=ROAD, outline="", tags="bg")
        for d in DIRECTIONS:
            ext = self._extent(d)
            x0, y0 = self._pt(d, r, 0)
            x1, y1 = self._pt(d, ext, 0)
            c.create_line(x0, y0, x1, y1, fill="#fbbf24", width=2, dash=(10, 8), tags="bg")
            for side in (r, -r):  # road edge lines
                ex0, ey0 = self._pt(d, r, side)
                ex1, ey1 = self._pt(d, ext, side)
                c.create_line(ex0, ey0, ex1, ey1, fill="#e5e7eb", width=2, tags="bg")
            sx0, sy0 = self._pt(d, r, 0)           # stop line across the approach lane
            sx1, sy1 = self._pt(d, r, r)
            c.create_line(sx0, sy0, sx1, sy1, fill="#ffffff", width=4, tags="bg")
            label_res, _badge = self._reserves(d)  # road-name label at the outer end
            centre = ext - label_res / 2
            if d in ("NORTH", "SOUTH"):
                box = self._box(d, centre - 11, centre + 11, -34, 34)
            else:
                box = self._box(d, centre - 32, centre + 32, -11, 11)
            c.create_rectangle(*box, fill="#ffffff", outline=BORDER, tags="bg")
            c.create_text((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, text=d,
                          font=("Segoe UI", 9, "bold"), fill=TEXT, tags="bg")
        c.tag_lower("bg")

    def _draw_legend(self) -> None:
        """Vehicle legend in the top-left grass corner (icons drawn with the real vehicle drawing code)."""
        c = self.canvas
        c.delete("legend")
        x0, y0 = 12, 12
        free_h = self.cv_h / 2 - self.ROAD_W / 2 - 24 - y0
        spacing = max(26, min(32, (free_h - 34) / len(VEHICLE_TYPES)))
        height = 34 + spacing * len(VEHICLE_TYPES)
        c.create_rectangle(x0, y0, x0 + 176, y0 + height, fill="#ffffff", outline=BORDER, tags="legend")
        c.create_text(x0 + 10, y0 + 14, text="VEHICLE LEGEND", anchor="w",
                      font=("Segoe UI", 9, "bold"), fill=MUTED, tags="legend")
        for i, vtype in enumerate(VEHICLE_TYPES):
            y = y0 + 38 + spacing * i + spacing / 2 - 10
            self._origin = (x0 + 40, y + self.ROAD_W / 4)    # icon centre lands at (x0+40, y)
            self._draw_vehicle("WEST", 0, vtype, "legend")   # WEST frame: vehicle faces right
            self._origin = None
            c.create_text(x0 + 84, y, text=vtype, anchor="w", font=("Segoe UI", 10, "bold"),
                          fill=TEXT, tags="legend")

    def _draw_signals(self) -> None:
        c = self.canvas
        c.delete("sig")
        sig = self.engine.signal
        for d in DIRECTIONS:
            x, y = self._pt(d, self.ROAD_W / 2 + 32, self.ROAD_W / 2 + 30)
            lit = "RED"
            if d == sig.active:
                lit = "GREEN" if sig.is_green else "YELLOW"
            c.create_rectangle(x - 10, y - 29, x + 10, y + 29, fill="#1f2937", outline="#0f172a", tags="sig")
            for dy, name, on, off in ((-19, "RED", "#ef4444", "#7f1d1d"),
                                      (0, "YELLOW", "#fbbf24", "#78530f"),
                                      (19, "GREEN", "#22c55e", "#14532d")):
                c.create_oval(x - 7, y + dy - 7, x + 7, y + dy + 7,
                              fill=on if lit == name else off, outline="", tags="sig")
        c.tag_raise("anim")

    def _rrect(self, d, u0, u1, v0, v1, r, fill, outline="", width=1, tag="veh") -> None:
        """Rounded rectangle in the local (u, v) frame."""
        r = max(0.0, min(r, (u1 - u0) / 2, (v1 - v0) / 2))
        pts = [(u0 + r, v0), (u1 - r, v0), (u1, v0), (u1, v0 + r), (u1, v1 - r), (u1, v1),
               (u1 - r, v1), (u0 + r, v1), (u0, v1), (u0, v1 - r), (u0, v0 + r), (u0, v0)]
        flat = [coord for u, v in pts for coord in self._pt(d, u, v)]
        self.canvas.create_polygon(*flat, fill=fill, outline=outline, width=width,
                                   smooth=True, tags=tag)

    def _draw_vehicle(self, d: str, u: float, vtype: str, tag: str, front: bool = False) -> None:
        """Top-down vehicle icon. Front of the vehicle points toward the intersection (smaller u)."""
        c = self.canvas
        length, width, colour = VEHICLE_SPECS[vtype]
        hl, hw = length / 2, width / 2
        cv = self.ROAD_W / 4
        outline = "#16a34a" if front else "#0f172a"
        ow = 3 if front else 1
        glass, tyre, lamp, tail = "#bfdbfe", "#111827", "#fef08a", "#ef4444"

        def rect(a, b, c0, c1, fill, line=""):
            c.create_rectangle(*self._box(d, u + a, u + b, cv + c0, cv + c1), fill=fill, outline=line, tags=tag)

        def rr(a, b, c0, c1, r, fill, line="", w=1):
            self._rrect(d, u + a, u + b, cv + c0, cv + c1, r, fill, line, w, tag)

        def seam(a, c0, c1, fill, w=1):
            x0, y0 = self._pt(d, u + a, cv + c0)
            x1, y1 = self._pt(d, u + a, cv + c1)
            c.create_line(x0, y0, x1, y1, fill=fill, width=w, tags=tag)

        def wheels(*axles, size=9, out=2):
            for a in axles:
                rect(a - size / 2, a + size / 2, -hw - out, -hw + 2, tyre)
                rect(a - size / 2, a + size / 2, hw - 2, hw + out, tyre)

        def lights(inset=2.5):
            rect(-hl, -hl + 2.5, -hw + inset, -hw + inset + 4, lamp)
            rect(-hl, -hl + 2.5, hw - inset - 4, hw - inset, lamp)
            rect(hl - 2.5, hl, -hw + inset, -hw + inset + 4, tail)
            rect(hl - 2.5, hl, hw - inset - 4, hw - inset, tail)

        if vtype == "Car":
            wheels(-hl * 0.55, hl * 0.5)
            rr(-hl, hl, -hw, hw, 8, colour, outline, ow)
            lights()
            rect(-hl + 10, -hl + 17, -hw + 3, hw - 3, glass)                     # windscreen
            rr(-hl + 17, hl - 12, -hw + 3, hw - 3, 3, "#93c5fd")                 # roof
            rect(hl - 12, hl - 7, -hw + 4, hw - 4, glass)                        # rear window
        elif vtype == "Bike":
            rr(-hl, -hl + 9, -2, 2, 2, tyre)                                     # front wheel
            rr(hl - 9, hl, -2, 2, 2, tyre)                                       # rear wheel
            rr(-hl + 5, hl - 5, -hw + 1, hw - 1, 4, colour, outline, ow)         # frame / tank
            rect(-hl + 5, -hl + 8, -hw - 5, hw + 5, "#374151")                   # handlebar
            c.create_oval(*self._box(d, u - 1, u + 7, cv - hw - 2, cv + hw + 2), fill="#1d4ed8",
                          outline="", tags=tag)                                  # rider shoulders
            c.create_oval(*self._box(d, u - 5, u + 1, cv - 3, cv + 3), fill="#1f2937",
                          outline="", tags=tag)                                  # helmet
            rect(-hl - 1, -hl + 1.5, -1.5, 1.5, lamp)                            # headlight
        elif vtype == "Bus":
            wheels(-hl * 0.55, hl * 0.5, size=11)
            rr(-hl, hl, -hw, hw, 7, colour, outline, ow)
            lights(3)
            rect(-hl + 3, -hl + 10, -hw + 3, hw - 3, glass)                      # windscreen
            rr(-hl + 12, hl - 3, -hw + 3, hw - 3, 3, "#bbf7d0")                  # roof
            for a in range(int(-hl + 20), int(hl - 6), 9):                       # roof panels
                seam(a, -hw + 3, hw - 3, "#15803d")
            rect(hl - 22, hl - 12, -4, 4, "#86efac", "#15803d")                  # AC unit
        elif vtype == "Truck":
            wheels(-hl + 9, hl - 15, hl - 6, size=10)
            rr(-hl, -hl + 17, -hw, hw, 5, "#78350f", outline, ow)               # cab
            rect(-hl + 3, -hl + 9, -hw + 3, hw - 3, glass)
            rect(-hl, -hl + 2.5, -hw + 3, -hw + 7, lamp)
            rect(-hl, -hl + 2.5, hw - 7, hw - 3, lamp)
            rect(-hl + 16, -hl + 21, -3, 3, "#374151")                           # coupling
            rr(-hl + 20, hl, -hw, hw, 3, "#d6a373", outline, ow)                 # cargo box
            for a in range(int(-hl + 27), int(hl - 3), 8):
                seam(a, -hw + 2, hw - 2, "#a16207")
            rect(hl - 2.5, hl, -hw + 3, -hw + 7, tail)
            rect(hl - 2.5, hl, hw - 7, hw - 3, tail)
        elif vtype == "Auto":
            wheels(-hl + 6, size=8)
            rect(hl - 13, hl - 5, -hw - 2, -hw + 2, tyre)
            rect(hl - 13, hl - 5, hw - 2, hw + 2, tyre)
            rr(-hl, hl, -hw, hw, 7, colour, outline, ow)
            rect(-hl + 5, -hl + 10, -hw + 3, hw - 3, glass)                      # windscreen
            rr(-hl + 10, hl - 1, -hw + 1, hw - 1, 3, "#1f2937")                  # canopy
            rect(-hl - 1, -hl + 1.5, -1.5, 1.5, lamp)
            rect(hl - 2, hl, -3, 3, tail)

    def _draw_queues(self) -> None:
        c = self.canvas
        c.delete("veh")
        v = self.ROAD_W / 4
        for d in DIRECTIONS:
            items = self.engine.queues.snapshot(d)
            visible = self._max_visible(d)
            for k, vehicle in enumerate(items[:visible]):
                self._draw_vehicle(d, self.START + self.SLOT * (k + 0.5), vehicle.vtype, "veh", front=(k == 0))
            extra = len(items) - visible
            if extra > 0:
                _label, badge_res = self._reserves(d)
                centre = self.START + self.SLOT * visible + badge_res / 2
                if d in ("NORTH", "SOUTH"):
                    box = self._box(d, centre - 10, centre + 10, v - 19, v + 19)
                else:
                    box = self._box(d, centre - 20, centre + 20, v - 10, v + 10)
                c.create_rectangle(*box, fill="#fee2e2", outline="#b91c1c", tags="veh")
                c.create_text((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, text=f"+{extra}",
                              font=("Segoe UI", 9, "bold"), fill="#b91c1c", tags="veh")
        c.tag_raise("anim")

    # ---- departing-vehicle animation ---- #
    def _spawn_anim(self, vehicle: Vehicle) -> None:
        self._anims.append({"d": vehicle.direction, "vtype": vehicle.vtype,
                            "u": self.START + self.SLOT / 2})
        if self._anim_id is None:
            self._anim_id = self.root.after(self.ANIM_MS, self._anim_step)

    def _lane_clearing(self, direction: str) -> bool:
        first_slot = self.START + self.SLOT / 2
        return any(a["d"] == direction and a["u"] > first_slot - self.SLOT for a in self._anims)

    def _anim_step(self) -> None:
        self._anim_id = None
        step = self.ANIM_SPEED * self.ANIM_MS / 1000.0
        survivors = []
        for a in self._anims:
            a["u"] -= step
            if a["u"] > -self._extent(a["d"]) - 30:   # still on screen
                survivors.append(a)
        self._anims = survivors
        self._draw_anims()
        if self._anims:
            self._anim_id = self.root.after(self.ANIM_MS, self._anim_step)

    def _draw_anims(self) -> None:
        self.canvas.delete("anim")
        for a in self._anims:
            self._draw_vehicle(a["d"], a["u"], a["vtype"], "anim")
        self.canvas.tag_raise("anim")


# --------------------------------------------------------------------------- #
# Headless self-tests (python smart_traffic_app.py --test)
# --------------------------------------------------------------------------- #
class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def run_self_tests() -> bool:
    results: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        results.append((name, bool(condition)))
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")

    print("Running engine self-tests...")

    # Test A - FIFO
    eng = SimulationEngine(_FakeClock())
    for plate in ("A", "B", "C"):
        eng.add_vehicle(plate, "Car", "NORTH")
    order = [eng.process_next().vehicle.number for _ in range(3)]
    check("A  FIFO: A, B, C leave in the same order", order == ["A", "B", "C"])

    # Test B - direction isolation
    eng = SimulationEngine(_FakeClock())
    eng.add_vehicle("N1", "Car", "NORTH")
    eng.add_vehicle("E1", "Bus", "EAST")
    out = eng.process_next().vehicle
    check("B  Direction isolation: only NORTH vehicle removed",
          out.number == "N1" and eng.queues.size("EAST") == 1)
    check("B  EAST vehicle untouched while NORTH is green", eng.process_next().vehicle is None
          and eng.queues.size("EAST") == 1)

    # Test C - empty queue
    eng = SimulationEngine(_FakeClock())
    result = eng.process_next()
    check("C  Empty queue: no crash, message returned, nothing counted",
          result.vehicle is None and "empty" in result.message and eng.stats.processed == 0)

    # Input validation and duplicates
    eng = SimulationEngine(_FakeClock())
    check("   Empty number rejected", eng.add_vehicle("   ", "Car", "NORTH")[0] is None)
    eng.add_vehicle("tn38ab1001", "Car", "SOUTH")
    check("   Duplicate (case-insensitive) rejected across queues",
          eng.add_vehicle("TN38AB1001", "Bike", "WEST")[0] is None)

    # Test D - automatic simulation (green -> yellow -> next direction)
    clock = _FakeClock()
    eng = SimulationEngine(clock)
    for i, d in enumerate(DIRECTIONS * 4):
        eng.add_vehicle(f"V{i:02d}", VEHICLE_TYPES[i % 5], d)
    seen_signals, isolated, processed, yellows = [eng.signal.active], True, 0, 0
    for _ in range(900):
        active_before, green_before = eng.signal.active, eng.signal.is_green
        clock.t += 0.1
        for event in eng.tick(0.1):
            if event[0] == "processed":
                processed += 1
                isolated &= green_before and event[1].vehicle.direction == active_before
            elif event[0] == "yellow":
                yellows += 1
            elif event[0] == "signal":
                seen_signals.append(event[2])
    check("D  Signals cycle NORTH>EAST>SOUTH>WEST>NORTH...",
          seen_signals[:6] == ["NORTH", "EAST", "SOUTH", "WEST", "NORTH", "EAST"])
    check("D  Vehicles processed only from the green direction", isolated and processed > 0)
    check("D  Every green phase is followed by a yellow phase", yellows >= len(seen_signals) - 1 >= 4)

    # Yellow phase blocks processing
    eng = SimulationEngine(_FakeClock())
    eng.add_vehicle("Y1", "Car", "NORTH")
    eng.signal.to_yellow()
    blocked = eng.process_next()
    check("   Yellow: processing blocked, vehicle stays queued",
          blocked.blocked and blocked.vehicle is None and eng.queues.size("NORTH") == 1)

    # Test E - pause / resume excludes paused time from waiting time
    clock = _FakeClock()
    eng = SimulationEngine(clock)
    eng.add_vehicle("P1", "Car", "NORTH")      # arrives at t=0
    clock.t = 1.0
    eng.pause()
    clock.t = 6.0                               # 5 s paused
    eng.resume()
    clock.t = 8.0
    wait = eng.process_next().wait
    check("E  Pause/resume: paused time excluded (wait == 3.0s)", abs(wait - 3.0) < 1e-9)
    check("E  Average waiting time formula", abs(eng.stats.average_wait - 3.0) < 1e-9)

    # Test F - reset
    eng.add_vehicle("R1", "Bus", "EAST")
    eng.tick(5.0)
    eng.reset()
    check("F  Reset: queues, stats, signal and countdown restored",
          eng.queues.total() == 0 and eng.stats.processed == 0 and eng.stats.peak == 0
          and eng.stats.average_wait == 0.0 and eng.signal.active == "NORTH"
          and eng.signal.remaining == eng.signal.green_duration and eng.signal.is_green)

    check("   Traffic status thresholds", [Statistics.traffic_status(n) for n in (0, 6, 13, 21)]
          == ["LOW", "MODERATE", "HIGH", "HEAVY"])

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    return not failed


def main() -> None:
    if "--test" in sys.argv:
        sys.exit(0 if run_self_tests() else 1)
    if tk is None:
        print("Tkinter is not available in this Python installation.")
        sys.exit(1)
    root = tk.Tk()
    TrafficApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()