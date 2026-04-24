#!/usr/bin/env python3
"""Tkinter desktop client for the image generation API."""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from image_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ApiError,
    generate_images,
    load_env_file,
    normalize_base_url,
    parse_extra_json,
    request_json,
    validate_api_key,
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
ENV_PATH = APP_DIR / ".env"
DEFAULT_OUTPUT_DIR = APP_DIR / "outputs"


class ImageClientApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Image API Client")
        self.root.geometry("1040x720")
        self.root.minsize(900, 620)

        load_env_file(ENV_PATH)

        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.current_worker: threading.Thread | None = None
        self.preview_source: tk.PhotoImage | None = None
        self.preview_image: tk.PhotoImage | None = None
        self.generated_paths: list[Path] = []

        self.api_key_var = tk.StringVar(value=os.environ.get("IMAGE_API_KEY", ""))
        self.base_url_var = tk.StringVar(value=os.environ.get("IMAGE_API_BASE", DEFAULT_BASE_URL))
        self.model_var = tk.StringVar(value=os.environ.get("IMAGE_MODEL", DEFAULT_MODEL))
        self.size_var = tk.StringVar(value="1024x1024")
        self.count_var = tk.IntVar(value=1)
        self.timeout_var = tk.IntVar(value=240)
        self.name_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.show_key_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self._build_style()
        self._build_ui()
        self.root.after(120, self._poll_worker_queue)

    def _build_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton", padding=(10, 6))
        style.configure("Primary.TButton", padding=(12, 7))
        style.configure("Status.TLabel", foreground="#475569")

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        left = ttk.Frame(main, width=360)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self._build_settings(left)
        self._build_prompt(left)
        self._build_generation_controls(left)
        self._build_status(left)
        self._build_preview(right)

    def _build_settings(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="API")
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Key").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
        self.key_entry = ttk.Entry(frame, textvariable=self.api_key_var, show="*")
        self.key_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=(10, 5))

        ttk.Checkbutton(
            frame,
            text="Show",
            variable=self.show_key_var,
            command=self._toggle_key_visibility,
        ).grid(row=1, column=1, sticky="w", pady=(0, 6))

        ttk.Label(frame, text="Base").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(frame, textvariable=self.base_url_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5
        )

        ttk.Label(frame, text="Model").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(frame, textvariable=self.model_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5
        )

        ttk.Button(frame, text="Save", command=self._save_config).grid(
            row=4, column=1, sticky="w", pady=(5, 10)
        )
        ttk.Button(frame, text="Models", command=self._list_models).grid(
            row=4, column=2, sticky="e", padx=10, pady=(5, 10)
        )

    def _build_prompt(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Prompt")
        frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.prompt_text = tk.Text(frame, height=8, wrap="word", undo=True)
        self.prompt_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        scrollbar = ttk.Scrollbar(frame, command=self.prompt_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", pady=10, padx=(0, 10))
        self.prompt_text.configure(yscrollcommand=scrollbar.set)

    def _build_generation_controls(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Output")
        frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Size").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
        size_box = ttk.Combobox(
            frame,
            textvariable=self.size_var,
            values=("1024x1024", "2048x2048"),
            state="normal",
            width=14,
        )
        size_box.grid(row=0, column=1, sticky="w", pady=(10, 5))

        ttk.Label(frame, text="Count").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        ttk.Spinbox(frame, from_=1, to=4, textvariable=self.count_var, width=8).grid(
            row=1, column=1, sticky="w", pady=5
        )

        ttk.Label(frame, text="Name").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(frame, textvariable=self.name_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5
        )

        ttk.Label(frame, text="Folder").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(frame, textvariable=self.output_dir_var).grid(
            row=3, column=1, sticky="ew", pady=5
        )
        ttk.Button(frame, text="Browse", command=self._choose_output_dir).grid(
            row=3, column=2, padx=10, pady=5
        )

        ttk.Label(frame, text="Extra").grid(row=4, column=0, sticky="w", padx=10, pady=5)
        self.extra_entry = ttk.Entry(frame)
        self.extra_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5)

        self.generate_button = ttk.Button(
            frame,
            text="Generate",
            style="Primary.TButton",
            command=self._generate,
        )
        self.generate_button.grid(row=5, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 10))

    def _build_status(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(frame, textvariable=self.status_var, style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )

    def _build_preview(self, parent: ttk.Frame) -> None:
        preview_frame = ttk.LabelFrame(parent, text="Preview")
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview_label = tk.Label(
            preview_frame,
            text="No image",
            bg="#f8fafc",
            fg="#64748b",
            anchor="center",
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        files_frame = ttk.Frame(preview_frame)
        files_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        files_frame.columnconfigure(0, weight=1)

        self.files_list = tk.Listbox(files_frame, height=5, exportselection=False)
        self.files_list.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.files_list.bind("<<ListboxSelect>>", self._on_file_selected)

        files_scrollbar = ttk.Scrollbar(files_frame, command=self.files_list.yview)
        files_scrollbar.grid(row=0, column=3, sticky="ns")
        self.files_list.configure(yscrollcommand=files_scrollbar.set)

        ttk.Button(files_frame, text="Open Image", command=self._open_selected_image).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Button(files_frame, text="Open Folder", command=self._open_output_folder).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )

    def _toggle_key_visibility(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "*")

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        if status:
            self.status_var.set(status)
        if busy:
            self.generate_button.configure(state="disabled")
            self.progress.start(12)
        else:
            self.generate_button.configure(state="normal")
            self.progress.stop()

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(APP_DIR))
        if selected:
            self.output_dir_var.set(selected)

    def _save_config(self) -> None:
        api_key = self.api_key_var.get().strip()
        base_url = normalize_base_url(self.base_url_var.get().strip() or DEFAULT_BASE_URL)
        model = self.model_var.get().strip() or DEFAULT_MODEL

        try:
            validate_api_key(api_key)
        except ApiError as exc:
            messagebox.showerror("Config", str(exc))
            return

        ENV_PATH.write_text(
            "\n".join(
                [
                    f"IMAGE_API_KEY={api_key}",
                    f"IMAGE_API_BASE={base_url}",
                    f"IMAGE_MODEL={model}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.environ["IMAGE_API_KEY"] = api_key
        os.environ["IMAGE_API_BASE"] = base_url
        os.environ["IMAGE_MODEL"] = model
        self.base_url_var.set(base_url)
        self.model_var.set(model)
        self.status_var.set(f"Saved config to {ENV_PATH.name}")

    def _list_models(self) -> None:
        if self._is_worker_running():
            return

        api_key = self.api_key_var.get().strip()
        base_url = normalize_base_url(self.base_url_var.get().strip() or DEFAULT_BASE_URL)
        try:
            validate_api_key(api_key)
        except ApiError as exc:
            messagebox.showerror("API", str(exc))
            return

        self._set_busy(True, "Loading models...")

        def worker() -> None:
            try:
                result = request_json("GET", f"{base_url}/models", api_key, timeout=self.timeout_var.get())
                data = result.get("data")
                model_ids: list[str] = []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            model_ids.append(item["id"])
                image_models = [model for model in model_ids if "image" in model]
                self.worker_queue.put(("models", image_models or model_ids))
            except Exception as exc:
                self.worker_queue.put(("error", str(exc)))

        self.current_worker = threading.Thread(target=worker, daemon=True)
        self.current_worker.start()

    def _generate(self) -> None:
        if self._is_worker_running():
            return

        prompt = self.prompt_text.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("Prompt", "Prompt is empty.")
            return

        try:
            count = int(self.count_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Output", "Count must be a number.")
            return

        api_key = self.api_key_var.get().strip()
        base_url = normalize_base_url(self.base_url_var.get().strip() or DEFAULT_BASE_URL)
        model = self.model_var.get().strip() or DEFAULT_MODEL
        size = self.size_var.get().strip()
        name = self.name_var.get().strip() or None
        output_dir = Path(self.output_dir_var.get()).expanduser()
        extra_raw = self.extra_entry.get().strip()

        try:
            validate_api_key(api_key)
            extra = parse_extra_json(extra_raw) if extra_raw else {}
        except ApiError as exc:
            messagebox.showerror("Input", str(exc))
            return

        self._set_busy(True, "Generating image...")

        def worker() -> None:
            try:
                paths = generate_images(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    prompt=prompt,
                    size=size,
                    n=max(1, min(count, 4)),
                    extra=extra,
                    output_dir=output_dir,
                    name=name,
                    timeout=self.timeout_var.get(),
                )
                self.worker_queue.put(("generated", paths))
            except Exception as exc:
                self.worker_queue.put(("error", str(exc)))

        self.current_worker = threading.Thread(target=worker, daemon=True)
        self.current_worker.start()

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                event, payload = self.worker_queue.get_nowait()
                if event == "generated":
                    self._handle_generated(payload)
                elif event == "models":
                    self._handle_models(payload)
                elif event == "error":
                    self._handle_error(payload)
        except queue.Empty:
            pass

        self.root.after(120, self._poll_worker_queue)

    def _handle_generated(self, paths: list[Path]) -> None:
        self._set_busy(False, f"Saved {len(paths)} image(s)")
        for path in paths:
            self.generated_paths.append(path)
            self.files_list.insert("end", str(path))
        if paths:
            last_index = self.files_list.size() - len(paths)
            self.files_list.selection_clear(0, "end")
            self.files_list.selection_set(last_index)
            self.files_list.see(last_index)
            self._display_image(paths[0])

    def _handle_models(self, models: list[str]) -> None:
        self._set_busy(False, f"Loaded {len(models)} model(s)")
        if "gpt-image-2" in models:
            self.model_var.set("gpt-image-2")
        model_text = "\n".join(models[:80]) if models else "No models returned."
        messagebox.showinfo("Models", model_text)

    def _handle_error(self, error: str) -> None:
        self._set_busy(False, "Error")
        messagebox.showerror("Error", error)

    def _is_worker_running(self) -> bool:
        if self.current_worker and self.current_worker.is_alive():
            self.status_var.set("Still working...")
            return True
        return False

    def _on_file_selected(self, _event: tk.Event[Any]) -> None:
        path = self._selected_path()
        if path:
            self._display_image(path)

    def _selected_path(self) -> Path | None:
        selection = self.files_list.curselection()
        if not selection:
            return None
        return Path(self.files_list.get(selection[0]))

    def _display_image(self, path: Path) -> None:
        try:
            source = tk.PhotoImage(file=str(path))
            preview_width = max(1, self.preview_label.winfo_width() - 24)
            preview_height = max(1, self.preview_label.winfo_height() - 24)
            factor = max(
                1,
                math.ceil(source.width() / preview_width),
                math.ceil(source.height() / preview_height),
            )
            self.preview_source = source
            self.preview_image = source.subsample(factor, factor)
            self.preview_label.configure(image=self.preview_image, text="", bg="#0f172a")
            self.status_var.set(f"Previewing {path.name}")
        except tk.TclError:
            self.preview_source = None
            self.preview_image = None
            self.preview_label.configure(
                image="",
                text=f"Preview unavailable\n{path.name}",
                bg="#f8fafc",
                fg="#64748b",
            )

    def _open_selected_image(self) -> None:
        path = self._selected_path()
        if path:
            open_path(path)

    def _open_output_folder(self) -> None:
        path = self._selected_path()
        if path:
            open_path(path.parent)
            return
        open_path(Path(self.output_dir_var.get()).expanduser())


def open_path(path: Path) -> None:
    path = path.resolve()
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def main() -> None:
    root = tk.Tk()
    ImageClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
