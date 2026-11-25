from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import engine
import updates

APP_TITLE = "Azur Promilia English Fan Patch"
SETTINGS = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "AP-English-Patcher"
    / "settings.json"
)


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> None:
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps({**load_settings(), **values}), encoding="utf-8")
    except OSError:
        pass


class App(tk.Tk):
    def __init__(self, patch: engine.PatchData):
        super().__init__()
        self.patch = patch
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.release: updates.Release | None = None
        self.last_status: engine.Status | None = None
        self.title(APP_TITLE)
        self.resizable(False, False)
        icon = (
            Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            / "data"
            / "icon.ico"
        )
        if icon.is_file():
            with contextlib.suppress(tk.TclError):
                self.iconbitmap(default=str(icon))
        scale = self.winfo_fpixels("1i") / 96.0

        def px(value):
            return int(round(value * scale))

        wrap = px(470)
        style = ttk.Style(self)
        style.configure("TButton", padding=(px(10), px(4)))

        outer = ttk.Frame(self, padding=px(16))
        outer.grid(sticky="nsew")
        ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        version_row = ttk.Frame(outer)
        version_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, px(12)))
        ttk.Label(
            version_row,
            text=f"Patch {patch.version} for {patch.game_label}",
            foreground="#666",
        ).pack(side="left")
        self.update_link = ttk.Label(
            version_row,
            foreground="#0a5fc2",
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
        )
        self.update_link.bind("<Button-1>", lambda _e: self.open_release())

        ttk.Label(outer, text="Game folder").grid(
            row=2, column=0, columnspan=2, sticky="w"
        )
        self.folder = tk.StringVar()
        ttk.Entry(outer, textvariable=self.folder, width=58, state="readonly").grid(
            row=3, column=0, sticky="we"
        )
        self.browse_btn = ttk.Button(outer, text="Browse...", command=self.browse)
        self.browse_btn.grid(row=3, column=1, padx=(px(8), 0))

        self.status = tk.StringVar(value="Looking for the game...")
        self.status_label = ttk.Label(
            outer,
            textvariable=self.status,
            font=("Segoe UI", 10, "bold"),
            wraplength=wrap,
            justify="left",
        )
        self.status_label.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(px(14), px(2))
        )
        self.detail = tk.StringVar()
        ttk.Label(
            outer,
            textvariable=self.detail,
            wraplength=wrap,
            justify="left",
            foreground="#444",
        ).grid(row=5, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(row=6, column=0, columnspan=2, sticky="we", pady=(px(14), px(8)))
        self.install_btn = ttk.Button(
            buttons, text="Install English Patch", command=self.do_install
        )
        self.install_btn.pack(side="left")
        self.restore_btn = ttk.Button(
            buttons, text="Restore Japanese", command=self.do_restore
        )
        self.restore_btn.pack(side="left", padx=(px(8), 0))

        # A stopped indeterminate bar keeps its last frame, so it is hidden when idle.
        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=wrap)
        self.progress.grid(row=7, column=0, columnspan=2, sticky="we")
        self.progress.grid_remove()
        ttk.Label(
            outer,
            text="Close the game before patching. Unofficial fan translation, "
            "not affiliated with Yostar. Use at your own risk.",
            foreground="#888",
            wraplength=wrap,
            justify="left",
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(px(10), 0))

        self.after(100, self.poll)
        self.after(50, self.pick_initial_folder)
        if load_settings().get("check_updates", True):
            self.check_for_update()

    def check_for_update(self):
        def worker():
            release = updates.newer_release(self.patch.version)
            if release:
                self.events.put(("update", release))

        threading.Thread(target=worker, daemon=True).start()

    def show_update(self, release: updates.Release):
        self.release = release
        self.update_link.configure(
            text=f"Patch {release.version} is available: download"
        )
        gap = int(round(8 * self.winfo_fpixels("1i") / 96.0))
        self.update_link.pack(side="left", padx=(gap, 0))
        if self.last_status:
            self.show_status(self.last_status)

    def open_release(self):
        if self.release:
            webbrowser.open(self.release.url)

    def pick_initial_folder(self):
        saved = load_settings().get("game_dir")
        if saved and engine.is_game_dir(Path(saved)):
            self.set_folder(Path(saved))
            return
        found = next(engine.find_game_dirs(), None)
        if found:
            self.set_folder(found)
        else:
            self.status.set("Game folder not found.")
            self.detail.set(
                f"Click Browse and choose the folder that contains {engine.EXE_NAME}."
            )
            self.update_buttons(None)

    def browse(self):
        chosen = filedialog.askdirectory(
            title=f"Choose the folder that contains {engine.EXE_NAME}"
        )
        if chosen:
            self.set_folder(Path(chosen))

    def set_folder(self, path: Path):
        path = engine.normalize_game_dir(path)
        self.folder.set(str(path))
        if engine.is_game_dir(path):
            save_settings({"game_dir": str(path)})
        self.refresh()

    def run_in_background(self, label, fn):
        self.busy = True
        self.update_buttons(None)
        self.status.set(label)
        self.detail.set("Please wait...")
        self.progress.grid()
        self.progress.start(12)

        def worker():
            try:
                self.events.put(("done", fn()))
            except engine.PatchError as e:
                self.events.put(("error", str(e)))
            except Exception as e:
                self.events.put(("error", f"Unexpected error: {type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "update":
                    self.show_update(value)
                    continue
                self.progress.stop()
                self.progress.grid_remove()
                self.busy = False
                if kind == "status":
                    self.show_status(value)
                elif kind == "done":
                    if isinstance(value, str):
                        messagebox.showinfo(APP_TITLE, value)
                    self.refresh()
                elif kind == "error":
                    messagebox.showerror(APP_TITLE, value)
                    self.refresh()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def refresh(self):
        game = Path(self.folder.get())
        self.busy = True
        self.update_buttons(None)
        self.status.set("Checking the game files...")
        self.detail.set("")

        def worker():
            try:
                self.events.put(("status", engine.inspect(game, self.patch)))
            except Exception as e:
                self.events.put(("error", f"Could not read the game files: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def show_status(self, st: engine.Status):
        titles = {
            "stock": "Japanese (original)",
            "patched": "English patch installed",
            "mixed": "Partly patched",
            "unknown": "Unrecognized game files",
            "wrong_build": "Different game version",
            "not_found": "Game folder not found",
        }
        self.last_status = st
        self.status.set(titles.get(st.kind, st.kind))
        message = st.message
        if st.kind == "wrong_build" and self.release:
            message += (
                f" Patch {self.release.version} is out and may support this version: "
                "use the download link above."
            )
        self.detail.set(message)
        self.update_buttons(st.kind)

    def update_buttons(self, kind):
        can_install = not self.busy and kind in ("stock", "mixed")
        can_restore = not self.busy and kind in ("patched", "mixed")
        self.install_btn.state(["!disabled"] if can_install else ["disabled"])
        self.restore_btn.state(["!disabled"] if can_restore else ["disabled"])
        self.browse_btn.state(["disabled"] if self.busy else ["!disabled"])

    def do_install(self):
        game = Path(self.folder.get())
        self.run_in_background(
            "Installing the English patch...", lambda: engine.install(game, self.patch)
        )

    def do_restore(self):
        if not messagebox.askyesno(APP_TITLE, "Restore the original Japanese text?"):
            return
        game = Path(self.folder.get())
        self.run_in_background(
            "Restoring the original Japanese text...",
            lambda: engine.restore(game, self.patch),
        )


def run_cli(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", choices=("status", "install", "restore"), required=True)
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--log", type=Path)
    args = ap.parse_args(argv)
    lines = []

    def say(msg):
        lines.append(msg)
        if sys.stdout:
            print(msg, flush=True)

    code = 0
    try:
        patch = engine.load_patch_data()
        say(f"patch {patch.version} for {patch.game_label}")
        if args.cli == "install":
            say(engine.install(args.game, patch, say))
        elif args.cli == "restore":
            say(engine.restore(args.game, patch, say))
        st = engine.inspect(args.game, patch)
        say(f"status: {st.kind}: {st.message}")
    except engine.PatchError as e:
        say(f"error: {e}")
        code = 1
    if args.log:
        args.log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return code


def main():
    if "--cli" in sys.argv:
        sys.exit(run_cli(sys.argv[1:]))
    enable_dpi_awareness()
    try:
        patch = engine.load_patch_data()
    except Exception as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, f"The patch data could not be loaded: {e}")
        return
    App(patch).mainloop()


if __name__ == "__main__":
    main()
