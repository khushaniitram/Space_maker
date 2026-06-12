"""Safe Space Maker: a GUI disk cleanup helper.

The app scans selected drives, reports drive usage, shows large accessible files
including hidden files, and can move selected non-essential candidates to the
Recycle Bin after one batch confirmation.
"""

from __future__ import annotations

import ctypes
import heapq
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Iterable


APP_NAME = "Safe Space Maker"
DEFAULT_MIN_SIZE_MB = 25
DEFAULT_TOP_RESULTS = 300

WINDOWS_FIXED_DRIVE = 3
WINDOWS_REMOVABLE_DRIVE = 2
WINDOWS_REPARSE_POINT = 0x0400

PROTECTED_DIR_NAMES = {
    "$sysreset",
    "$windows.~bt",
    "$winreagent",
    "boot",
    "config.msi",
    "efi",
    "msocache",
    "perflogs",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "system volume information",
    "windows",
    "windows.old",
    "windowsapps",
}

PROJECT_STATE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}

APP_DATA_DIR_NAMES = {
    "appdata",
    "application data",
    "local settings",
}

USER_CONTENT_DIR_NAMES = {
    "desktop",
    "documents",
    "downloads",
    "dropbox",
    "google drive",
    "movies",
    "music",
    "onedrive",
    "pictures",
    "videos",
}

CLEANUP_DIR_NAMES = {
    "__pycache__": "Python bytecode cache folder",
    ".cache": "User cache folder",
    ".mypy_cache": "Python type-checker cache folder",
    ".pytest_cache": "Python test cache folder",
    ".ruff_cache": "Python linter cache folder",
    "_cacache": "Package manager cache folder",
    "cache": "Cache folder",
    "caches": "Cache folder",
    "code cache": "Application code cache folder",
    "crashpad": "Application crash report folder",
    "dawncache": "Graphics cache folder",
    "gpucache": "Graphics cache folder",
    "grshadercache": "Graphics shader cache folder",
    "shadercache": "Graphics shader cache folder",
    "temp": "Temporary files folder",
    "tmp": "Temporary files folder",
}

TEMP_FILE_EXTENSIONS = {
    ".bak": "Backup file",
    ".crash": "Crash report",
    ".dmp": "Crash dump",
    ".dump": "Crash dump",
    ".log": "Log file",
    ".mdmp": "Mini dump",
    ".old": "Old backup file",
    ".temp": "Temporary file",
    ".tmp": "Temporary file",
}

DOWNLOAD_REVIEW_EXTENSIONS = {
    ".7z",
    ".apk",
    ".avi",
    ".deb",
    ".dmg",
    ".exe",
    ".gz",
    ".iso",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".msi",
    ".pkg",
    ".rar",
    ".rpm",
    ".tar",
    ".tgz",
    ".webm",
    ".zip",
}


@dataclass(frozen=True)
class DriveInfo:
    root: str
    label: str
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class Candidate:
    path: str
    size: int
    kind: str
    reason: str
    modified: float


@dataclass(frozen=True)
class DeletionFailure:
    path: str
    error: str


@dataclass
class DeletionResult:
    deleted_paths: list[str]
    failures: list[DeletionFailure]

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class ScanStats:
    scanned_files: int = 0
    scanned_dirs: int = 0
    skipped_dirs: int = 0
    inaccessible_items: int = 0
    candidates_seen: int = 0
    current_path: str = ""


def normalise_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def path_parts_lower(path: str | Path) -> list[str]:
    return [part.lower() for part in Path(path).parts]


def is_drive_root(path: str | Path) -> bool:
    absolute = os.path.abspath(str(path))
    drive, tail = os.path.splitdrive(absolute)
    return bool(drive) and tail in ("\\", "/")


def is_under_path(path: str | Path, parent: str | Path) -> bool:
    child_norm = normalise_path(path)
    parent_norm = normalise_path(parent)
    try:
        return os.path.commonpath([child_norm, parent_norm]) == parent_norm
    except ValueError:
        return False


def is_under_any(path: str | Path, parents: Iterable[str | Path]) -> bool:
    return any(is_under_path(path, parent) for parent in parents if parent)


def is_excluded_path(path: str | Path, excluded_paths: Iterable[str | Path]) -> bool:
    return is_under_any(path, excluded_paths)


def get_temp_roots() -> list[str]:
    roots = []
    for variable in ("TEMP", "TMP"):
        value = os.environ.get(variable)
        if value:
            roots.append(value)

    home = Path.home()
    roots.extend(
        [
            str(home / "AppData" / "Local" / "Temp"),
            str(home / "AppData" / "LocalLow" / "Temp"),
        ]
    )

    existing = []
    seen = set()
    for root in roots:
        root_norm = normalise_path(root)
        if root_norm not in seen and os.path.isdir(root):
            seen.add(root_norm)
            existing.append(root)
    return existing


def is_in_user_area(path: str | Path) -> bool:
    home = Path.home()
    if is_under_path(path, home):
        return True
    return "users" in path_parts_lower(path)


def is_downloads_path(path: str | Path) -> bool:
    return "downloads" in path_parts_lower(path)


def is_app_data_path(path: str | Path) -> bool:
    parts = path_parts_lower(path)
    return any(part in APP_DATA_DIR_NAMES for part in parts)


def is_user_content_path(path: str | Path) -> bool:
    parts = path_parts_lower(path)
    return any(part in USER_CONTENT_DIR_NAMES for part in parts)


def is_protected_path(path: str | Path) -> bool:
    parts = path_parts_lower(path)
    return any(part in PROTECTED_DIR_NAMES for part in parts)


def should_skip_directory(path: str | Path) -> bool:
    if is_drive_root(path):
        return False

    name = Path(path).name.lower()
    if name in PROTECTED_DIR_NAMES:
        return True
    if name in PROJECT_STATE_DIR_NAMES:
        return True
    if is_protected_path(path):
        return True
    return False


def cleanup_directory_reason(path: str | Path, temp_roots: Iterable[str | Path]) -> str | None:
    if is_drive_root(path) or is_protected_path(path):
        return None

    if is_under_any(path, temp_roots):
        return "Inside a user temporary files folder"

    name = Path(path).name.lower()
    reason = CLEANUP_DIR_NAMES.get(name)
    if reason and is_in_user_area(path):
        return reason

    return None


def cleanup_file_reason(path: str | Path, temp_roots: Iterable[str | Path]) -> str | None:
    if is_protected_path(path):
        return None

    if is_under_any(path, temp_roots):
        return "Inside a user temporary files folder"

    suffix = Path(path).suffix.lower()
    if suffix in TEMP_FILE_EXTENSIONS and is_in_user_area(path):
        return TEMP_FILE_EXTENSIONS[suffix]

    if is_downloads_path(path) and suffix in DOWNLOAD_REVIEW_EXTENSIONS:
        return "Reviewable large file in Downloads"

    return None


def large_file_reason(path: str | Path, temp_roots: Iterable[str | Path]) -> str:
    cleanup_reason = cleanup_file_reason(path, temp_roots)
    if cleanup_reason:
        return cleanup_reason
    if is_protected_path(path):
        return "Protected OS/application file (audit only)"
    if is_app_data_path(path):
        return "Large application data file (audit only)"
    if is_user_content_path(path):
        return "Large user file (review before deleting)"
    if is_in_user_area(path):
        return "Large user-profile file (review before deleting)"
    return "Large file outside protected system folders"


def deletion_status_for_path(
    path: str | Path,
    kind: str,
    temp_roots: Iterable[str | Path],
    excluded_paths: Iterable[str | Path] = (),
) -> str:
    if is_excluded_path(path, excluded_paths):
        return "Blocked: user exclusion"
    if is_drive_root(path):
        return "Blocked: drive root"
    if os.path.islink(path):
        return "Blocked: link"
    if is_protected_path(path):
        return "Blocked: protected OS/app path"

    if kind == "Folder":
        if cleanup_directory_reason(path, temp_roots) is not None:
            return "Allowed: cleanup folder"
        return "Blocked: folder not cleanup-only"

    if cleanup_file_reason(path, temp_roots) is not None:
        return "Allowed: cleanup file"
    if is_app_data_path(path):
        return "Blocked: application data"
    if is_in_user_area(path):
        return "Allowed: selected user file"
    return "Blocked: outside user profile"


def is_candidate_deletable(
    candidate: Candidate,
    temp_roots: Iterable[str | Path],
    excluded_paths: Iterable[str | Path] = (),
) -> bool:
    path = candidate.path
    if not os.path.exists(path):
        return False
    return deletion_status_for_path(path, candidate.kind, temp_roots, excluded_paths).startswith("Allowed:")


def collapse_nested_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    collapsed = []
    collapsed_paths = []
    for candidate in sorted(candidates, key=lambda item: len(Path(item.path).parts)):
        if is_under_any(candidate.path, collapsed_paths):
            continue
        collapsed.append(candidate)
        collapsed_paths.append(candidate.path)
    return collapsed


def entry_is_reparse_point(entry: os.DirEntry) -> bool:
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except (OSError, PermissionError):
        return False
    return bool(attributes & WINDOWS_REPARSE_POINT)


def human_size(size: int) -> str:
    value = float(size)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or suffix == "TB":
            if suffix == "B":
                return f"{int(value)} {suffix}"
            return f"{value:.1f} {suffix}"
        value /= 1024
    return f"{value:.1f} TB"


def human_time(timestamp: float) -> str:
    if not timestamp:
        return "Unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def get_drive_label(root: str) -> str:
    if platform.system() != "Windows":
        return "Local"

    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
    except AttributeError:
        return "Local"

    return {
        WINDOWS_REMOVABLE_DRIVE: "Removable",
        WINDOWS_FIXED_DRIVE: "Local disk",
        4: "Network",
        5: "Optical",
        6: "RAM disk",
    }.get(drive_type, "Unknown")


def discover_drives() -> list[DriveInfo]:
    roots: list[str] = []
    if platform.system() == "Windows":
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if bitmask & (1 << index):
                root = f"{chr(65 + index)}:\\"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
                if drive_type in (WINDOWS_FIXED_DRIVE, WINDOWS_REMOVABLE_DRIVE):
                    roots.append(root)
    else:
        roots.append(Path.home().anchor or "/")

    drives = []
    for root in roots:
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append(
            DriveInfo(
                root=root,
                label=get_drive_label(root),
                total=usage.total,
                used=usage.used,
                free=usage.free,
            )
        )
    return drives


class CandidateHeap:
    def __init__(self, max_items: int) -> None:
        self.max_items = max_items
        self._counter = 0
        self._heap: list[tuple[int, int, Candidate]] = []

    def add(self, candidate: Candidate) -> None:
        self._counter += 1
        item = (candidate.size, self._counter, candidate)
        if len(self._heap) < self.max_items:
            heapq.heappush(self._heap, item)
        elif candidate.size > self._heap[0][0]:
            heapq.heapreplace(self._heap, item)

    def sorted_candidates(self) -> list[Candidate]:
        return [
            item[2]
            for item in sorted(self._heap, key=lambda heap_item: heap_item[0], reverse=True)
        ]


class DriveScanner:
    def __init__(
        self,
        roots: Iterable[str],
        min_size_bytes: int,
        top_results: int,
        deep_audit: bool,
        updates: queue.Queue,
        stop_event: threading.Event,
    ) -> None:
        self.roots = list(roots)
        self.min_size_bytes = min_size_bytes
        self.deep_audit = deep_audit
        self.updates = updates
        self.stop_event = stop_event
        self.temp_roots = get_temp_roots()
        self.heap = CandidateHeap(top_results)
        self.stats = ScanStats()
        self._last_progress_at = 0.0

    def run(self) -> None:
        try:
            for root in self.roots:
                if self.stop_event.is_set():
                    break
                self._send_status(f"Scanning {root} ...")
                self._scan_directory(root, inside_cleanup_dir=False)

            self.updates.put(("done", self.heap.sorted_candidates(), self.stats))
        except Exception as exc:
            self.updates.put(("error", f"{type(exc).__name__}: {exc}"))

    def _scan_directory(self, path: str, inside_cleanup_dir: bool) -> int:
        if self.stop_event.is_set():
            return 0

        if not self.deep_audit and should_skip_directory(path):
            self.stats.skipped_dirs += 1
            return 0

        self.stats.scanned_dirs += 1
        self.stats.current_path = path
        self._send_progress()

        try:
            entries = list(os.scandir(path))
        except (OSError, PermissionError):
            self.stats.inaccessible_items += 1
            return 0

        directory_reason = cleanup_directory_reason(path, self.temp_roots)
        cleanup_scope = inside_cleanup_dir or directory_reason is not None
        total_size = 0
        newest_modified = 0.0

        for entry in entries:
            if self.stop_event.is_set():
                break

            try:
                if entry.is_symlink() or entry_is_reparse_point(entry):
                    continue

                if entry.is_dir(follow_symlinks=False):
                    child_size = self._scan_directory(entry.path, cleanup_scope)
                    total_size += child_size
                    continue

                if entry.is_file(follow_symlinks=False):
                    stat_result = entry.stat(follow_symlinks=False)
                    total_size += stat_result.st_size
                    newest_modified = max(newest_modified, stat_result.st_mtime)
                    self.stats.scanned_files += 1

                    if stat_result.st_size >= self.min_size_bytes:
                        reason = None
                        if not cleanup_scope:
                            reason = cleanup_file_reason(entry.path, self.temp_roots)
                        if reason is None and self.deep_audit:
                            reason = large_file_reason(entry.path, self.temp_roots)
                        if reason is not None:
                            self._add_candidate(
                                Candidate(
                                    path=entry.path,
                                    size=stat_result.st_size,
                                    kind="File",
                                    reason=reason,
                                    modified=stat_result.st_mtime,
                                )
                            )
            except (OSError, PermissionError):
                self.stats.inaccessible_items += 1

        if directory_reason and total_size >= self.min_size_bytes:
            self._add_candidate(
                Candidate(
                    path=path,
                    size=total_size,
                    kind="Folder",
                    reason=directory_reason,
                    modified=newest_modified,
                )
            )

        return total_size

    def _add_candidate(self, candidate: Candidate) -> None:
        self.stats.candidates_seen += 1
        self.heap.add(candidate)

    def _send_status(self, message: str) -> None:
        self.updates.put(("status", message))

    def _send_progress(self) -> None:
        now = time.monotonic()
        if now - self._last_progress_at >= 0.2:
            self._last_progress_at = now
            self.updates.put(("progress", self.stats))


def recycle_paths(paths: Iterable[str]) -> DeletionResult:
    path_list = [str(path) for path in paths]
    if not path_list:
        return DeletionResult(deleted_paths=[], failures=[])

    if platform.system() == "Windows":
        return recycle_paths_windows(path_list)

    deleted_paths = []
    failures = []
    for path in path_list:
        if not os.path.exists(path):
            failures.append(DeletionFailure(path, "Path no longer exists."))
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            deleted_paths.append(path)
        except OSError as exc:
            failures.append(DeletionFailure(path, str(exc)))
    return DeletionResult(deleted_paths=deleted_paths, failures=failures)


def windows_recycle_error(code: int, aborted: bool = False) -> str:
    if aborted:
        return "Windows reported that the recycle operation was cancelled."

    hints = {
        124: (
            "Windows rejected the file list. This often means one selected path is locked, "
            "missing, too long, or cannot be moved to the Recycle Bin."
        ),
    }
    try:
        system_message = ctypes.FormatError(code).strip()
    except OSError:
        system_message = ""

    hint = hints.get(code)
    if hint and system_message:
        return f"Windows recycle failed with code {code}: {hint} ({system_message})"
    if hint:
        return f"Windows recycle failed with code {code}: {hint}"
    if system_message:
        return f"Windows recycle failed with code {code}: {system_message}"
    return f"Windows recycle failed with code {code}."


def try_recycle_paths_windows(paths: list[str]) -> tuple[bool, str]:
    if not paths:
        return True, ""

    try:
        existing_paths = [path for path in paths if os.path.exists(path)]
    except OSError:
        existing_paths = paths
    missing_paths = [path for path in paths if path not in existing_paths]
    if missing_paths:
        return False, f"Path no longer exists: {missing_paths[0]}"

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_bool),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400

    joined_paths = "\0".join(paths) + "\0\0"
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = joined_paths
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        return False, windows_recycle_error(result)
    if operation.fAnyOperationsAborted:
        return False, windows_recycle_error(0, aborted=True)
    return True, ""


def recycle_paths_windows(paths: list[str]) -> DeletionResult:
    ok, batch_error = try_recycle_paths_windows(paths)
    if ok:
        return DeletionResult(deleted_paths=paths, failures=[])

    deleted_paths = []
    failures = []
    for path in paths:
        ok, error = try_recycle_paths_windows([path])
        if ok:
            deleted_paths.append(path)
        else:
            failures.append(DeletionFailure(path, error or batch_error))

    return DeletionResult(deleted_paths=deleted_paths, failures=failures)


class SpaceMakerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 620)

        self.drives: list[DriveInfo] = []
        self.drive_vars: dict[str, tk.BooleanVar] = {}
        self.candidates_by_iid: dict[str, Candidate] = {}
        self.excluded_paths: dict[str, str] = {}
        self.updates: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.temp_roots = get_temp_roots()

        self.status_var = tk.StringVar(value="Ready. Choose drives, then scan.")
        self.summary_var = tk.StringVar(value="No scan has run yet.")
        self.min_size_var = tk.StringVar(value=str(DEFAULT_MIN_SIZE_MB))
        self.top_results_var = tk.StringVar(value=str(DEFAULT_TOP_RESULTS))
        self.deep_audit_var = tk.BooleanVar(value=True)
        self.exclude_path_var = tk.StringVar()

        self._configure_style()
        self._build_ui()
        self.refresh_drives()
        self.after(150, self._process_updates)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Muted.TLabel", foreground="#555555")
        style.configure("Danger.TButton", foreground="#8a0000")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=APP_NAME, style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=(
                "Finds large files including hidden files. Protected OS/app paths are shown "
                "for audit but blocked from deletion. Nothing is deleted automatically."
            ),
            style="Muted.TLabel",
            wraplength=1000,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        controls = ttk.Frame(self, padding=(16, 4, 16, 10))
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure(2, weight=1)

        drive_box = ttk.LabelFrame(controls, text="Drives")
        drive_box.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 12))
        self.drive_frame = ttk.Frame(drive_box, padding=8)
        self.drive_frame.grid(row=0, column=0, sticky="nsew")

        options = ttk.LabelFrame(controls, text="Scan options")
        options.grid(row=0, column=1, sticky="nw", padx=(0, 12))
        ttk.Label(options, text="Minimum item size (MB)").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        ttk.Entry(options, width=10, textvariable=self.min_size_var).grid(row=1, column=0, sticky="w", padx=8)
        ttk.Label(options, text="Top results").grid(row=0, column=1, sticky="w", padx=8, pady=(8, 2))
        ttk.Entry(options, width=10, textvariable=self.top_results_var).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Checkbutton(
            options,
            text="Deep audit: show all large accessible files",
            variable=self.deep_audit_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 8))

        actions = ttk.Frame(controls)
        actions.grid(row=0, column=2, sticky="ne")
        self.scan_button = ttk.Button(actions, text="Scan selected drives", command=self.start_scan)
        self.scan_button.grid(row=0, column=0, padx=4, pady=4)
        self.stop_button = ttk.Button(actions, text="Stop scan", command=self.stop_scan, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=4, pady=4)
        self.refresh_button = ttk.Button(actions, text="Refresh drives", command=self.refresh_drives)
        self.refresh_button.grid(row=0, column=2, padx=4, pady=4)
        self.delete_button = ttk.Button(
            actions,
            text="Delete selected...",
            style="Danger.TButton",
            command=self.delete_selected,
            state="disabled",
        )
        self.delete_button.grid(row=1, column=0, padx=4, pady=4)
        ttk.Button(actions, text="Open selected folder", command=self.open_selected_folder).grid(
            row=1, column=1, padx=4, pady=4
        )
        ttk.Button(actions, text="Select all", command=self.select_all_results).grid(
            row=2, column=0, padx=4, pady=4
        )
        ttk.Button(actions, text="Select deletable", command=self.select_deletable_results).grid(
            row=2, column=1, padx=4, pady=4
        )
        ttk.Button(actions, text="Clear selection", command=self.clear_selection).grid(
            row=2, column=2, padx=4, pady=4
        )

        exclusions = ttk.LabelFrame(controls, text="Never delete paths")
        exclusions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        exclusions.columnconfigure(0, weight=1)
        ttk.Entry(exclusions, textvariable=self.exclude_path_var).grid(
            row=0, column=0, sticky="ew", padx=(8, 4), pady=(8, 4)
        )
        ttk.Button(exclusions, text="Add typed path", command=self.add_excluded_path_from_entry).grid(
            row=0, column=1, sticky="ew", padx=4, pady=(8, 4)
        )
        ttk.Button(exclusions, text="Add selected result", command=self.add_selected_to_exclusions).grid(
            row=0, column=2, sticky="ew", padx=4, pady=(8, 4)
        )
        ttk.Button(exclusions, text="Remove selected", command=self.remove_selected_exclusions).grid(
            row=0, column=3, sticky="ew", padx=(4, 8), pady=(8, 4)
        )
        self.exclusion_listbox = tk.Listbox(exclusions, height=3, selectmode="extended", exportselection=False)
        self.exclusion_listbox.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=(0, 8))

        ttk.Label(controls, textvariable=self.summary_var, style="Muted.TLabel", wraplength=620).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=(10, 0)
        )

        table_frame = ttk.Frame(self, padding=(16, 0, 16, 8))
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("size", "kind", "delete_status", "reason", "modified", "path")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("size", text="Size")
        self.tree.heading("kind", text="Type")
        self.tree.heading("delete_status", text="Delete status")
        self.tree.heading("reason", text="Why it is shown")
        self.tree.heading("modified", text="Modified")
        self.tree.heading("path", text="Path")
        self.tree.column("size", width=100, anchor="e", stretch=False)
        self.tree.column("kind", width=80, stretch=False)
        self.tree.column("delete_status", width=210, stretch=False)
        self.tree.column("reason", width=250, stretch=False)
        self.tree.column("modified", width=140, stretch=False)
        self.tree.column("path", width=520)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(self, padding=(16, 2, 16, 12))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

    def refresh_drives(self) -> None:
        self.drives = discover_drives()
        for child in self.drive_frame.winfo_children():
            child.destroy()
        self.drive_vars.clear()

        if not self.drives:
            ttk.Label(self.drive_frame, text="No local drives found.").grid(row=0, column=0, sticky="w")
            return

        fullest = max(self.drives, key=lambda drive: drive.used / drive.total if drive.total else 0)
        for row, drive in enumerate(self.drives):
            used_percent = (drive.used / drive.total * 100) if drive.total else 0
            label = (
                f"{drive.root}  {drive.label}  "
                f"{human_size(drive.used)} used / {human_size(drive.total)} "
                f"({used_percent:.0f}% full, {human_size(drive.free)} free)"
            )
            variable = tk.BooleanVar(value=drive.label in {"Local disk", "Removable"})
            self.drive_vars[drive.root] = variable
            ttk.Checkbutton(self.drive_frame, text=label, variable=variable).grid(
                row=row, column=0, sticky="w", pady=2
            )

        self.summary_var.set(
            f"Most space-consuming drive right now: {fullest.root} "
            f"with {human_size(fullest.used)} used and {human_size(fullest.free)} free."
        )

    def start_scan(self) -> None:
        roots = [root for root, variable in self.drive_vars.items() if variable.get()]
        if not roots:
            messagebox.showinfo(APP_NAME, "Select at least one drive to scan.")
            return

        try:
            min_size_mb = max(1, int(self.min_size_var.get()))
            top_results = max(10, int(self.top_results_var.get()))
        except ValueError:
            messagebox.showerror(APP_NAME, "Minimum size and top results must be whole numbers.")
            return

        self._clear_results()
        self.stop_event.clear()
        self.scan_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.refresh_button.configure(state="disabled")
        self.delete_button.configure(state="disabled")
        self.status_var.set("Starting scan...")
        if self.deep_audit_var.get():
            self.summary_var.set(
                "Deep audit can take a while. Hidden files are included; protected OS/app files are audit-only."
            )
        else:
            self.summary_var.set("Cleanup scan can take a while. Protected system folders are skipped.")

        scanner = DriveScanner(
            roots=roots,
            min_size_bytes=min_size_mb * 1024 * 1024,
            top_results=top_results,
            deep_audit=self.deep_audit_var.get(),
            updates=self.updates,
            stop_event=self.stop_event,
        )
        self.worker = threading.Thread(target=scanner.run, name="space-maker-scan", daemon=True)
        self.worker.start()

    def stop_scan(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping scan after the current folder finishes...")

    def delete_selected(self) -> None:
        selected_ids = self.tree.selection()
        if not selected_ids:
            messagebox.showinfo(APP_NAME, "Select one or more cleanup candidates first.")
            return

        selected_candidates = [self.candidates_by_iid[item_id] for item_id in selected_ids]
        deletable_candidates = [
            candidate
            for candidate in selected_candidates
            if is_candidate_deletable(candidate, self.temp_roots, self.excluded_paths.values())
        ]
        blocked = len(selected_candidates) - len(deletable_candidates)
        deletable_candidates = collapse_nested_candidates(deletable_candidates)

        if not deletable_candidates:
            messagebox.showwarning(
                APP_NAME,
                "None of the selected items are deletable by this app. Protected, excluded, and app-data paths are blocked.",
            )
            return

        total_size = sum(candidate.size for candidate in deletable_candidates)
        target_word = "Recycle Bin" if platform.system() == "Windows" else "permanent deletion"
        details = "\n".join(f"- {candidate.path}" for candidate in deletable_candidates[:8])
        if len(deletable_candidates) > 8:
            details += f"\n- ...and {len(deletable_candidates) - 8} more"
        if blocked:
            details += (
                f"\n\n{blocked} selected item(s) will be skipped because they are protected, "
                "excluded, or app data."
            )

        confirmed = messagebox.askyesno(
            APP_NAME,
            (
                f"Move {len(deletable_candidates)} selected item(s), totaling {human_size(total_size)}, "
                f"to {target_word}?\n\n"
                f"{details}\n\n"
                "This app never deletes unselected items."
            ),
            icon="warning",
        )
        if not confirmed:
            return

        result = recycle_paths(candidate.path for candidate in deletable_candidates)
        deleted_candidates = [
            candidate
            for candidate in deletable_candidates
            if is_under_any(candidate.path, result.deleted_paths)
        ]
        deleted_size = sum(candidate.size for candidate in deleted_candidates)

        deleted_candidate_paths = [candidate.path for candidate in deleted_candidates]
        for item_id, candidate in list(self.candidates_by_iid.items()):
            if is_under_any(candidate.path, deleted_candidate_paths):
                self.tree.delete(item_id)
                del self.candidates_by_iid[item_id]

        if result.failures:
            failure_details = "\n".join(
                f"- {failure.path}\n  {failure.error}" for failure in result.failures[:6]
            )
            if len(result.failures) > 6:
                failure_details += f"\n- ...and {len(result.failures) - 6} more failure(s)"
            if result.deleted_paths:
                messagebox.showwarning(
                    APP_NAME,
                    (
                        f"Cleanup partly finished. Deleted {len(result.deleted_paths)} item(s), "
                        f"but {len(result.failures)} item(s) failed:\n\n{failure_details}"
                    ),
                )
            else:
                messagebox.showerror(
                    APP_NAME,
                    f"Cleanup failed. No selected items could be moved:\n\n{failure_details}",
                )

        self.status_var.set(
            f"Cleaned {len(result.deleted_paths)} item(s), totaling about {human_size(deleted_size)}."
        )
        self._on_tree_select()

    def select_all_results(self) -> None:
        items = self.tree.get_children()
        self.tree.selection_set(items)
        self._on_tree_select()

    def select_deletable_results(self) -> None:
        deletable_items = [
            item_id
            for item_id, candidate in self.candidates_by_iid.items()
            if is_candidate_deletable(candidate, self.temp_roots, self.excluded_paths.values())
        ]
        self.tree.selection_set(deletable_items)
        self._on_tree_select()

    def clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._on_tree_select()

    def add_excluded_path_from_entry(self) -> None:
        path = self.exclude_path_var.get().strip().strip('"')
        if not path:
            messagebox.showinfo(APP_NAME, "Type a file or folder path first.")
            return
        self._add_excluded_path(path)
        self.exclude_path_var.set("")

    def add_selected_to_exclusions(self) -> None:
        selected_ids = self.tree.selection()
        if not selected_ids:
            messagebox.showinfo(APP_NAME, "Select one or more scan results first.")
            return

        added = 0
        for item_id in selected_ids:
            candidate = self.candidates_by_iid[item_id]
            if self._add_excluded_path(candidate.path, refresh=False):
                added += 1
        self._refresh_exclusion_list()
        self._refresh_delete_statuses()
        self.status_var.set(f"Added {added} path(s) to the never-delete list.")

    def remove_selected_exclusions(self) -> None:
        selected_indexes = list(self.exclusion_listbox.curselection())
        if not selected_indexes:
            messagebox.showinfo(APP_NAME, "Select one or more never-delete paths first.")
            return

        for index in selected_indexes:
            path = self.exclusion_listbox.get(index)
            self.excluded_paths.pop(normalise_path(path), None)
        self._refresh_exclusion_list()
        self._refresh_delete_statuses()
        self.status_var.set(f"Removed {len(selected_indexes)} path(s) from the never-delete list.")

    def _add_excluded_path(self, path: str, refresh: bool = True) -> bool:
        expanded = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
        absolute = os.path.abspath(expanded)
        key = normalise_path(absolute)
        was_new = key not in self.excluded_paths
        self.excluded_paths[key] = absolute
        if refresh:
            self._refresh_exclusion_list()
            self._refresh_delete_statuses()
            self.status_var.set(f"Protected from deletion: {absolute}")
        return was_new

    def _refresh_exclusion_list(self) -> None:
        self.exclusion_listbox.delete(0, tk.END)
        for path in sorted(self.excluded_paths.values(), key=str.lower):
            self.exclusion_listbox.insert(tk.END, path)

    def _refresh_delete_statuses(self) -> None:
        for item_id, candidate in self.candidates_by_iid.items():
            self.tree.set(
                item_id,
                "delete_status",
                deletion_status_for_path(
                    candidate.path,
                    candidate.kind,
                    self.temp_roots,
                    self.excluded_paths.values(),
                ),
            )
        self._on_tree_select()

    def open_selected_folder(self) -> None:
        selected_ids = self.tree.selection()
        if not selected_ids:
            messagebox.showinfo(APP_NAME, "Select a result first.")
            return

        candidate = self.candidates_by_iid[selected_ids[0]]
        folder = candidate.path if os.path.isdir(candidate.path) else os.path.dirname(candidate.path)
        if not folder or not os.path.exists(folder):
            messagebox.showwarning(APP_NAME, "The selected item no longer exists.")
            return

        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", folder])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{exc}")

    def _clear_results(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.candidates_by_iid.clear()

    def _insert_candidates(self, candidates: list[Candidate]) -> None:
        self._clear_results()
        for index, candidate in enumerate(candidates):
            item_id = f"candidate-{index}"
            self.candidates_by_iid[item_id] = candidate
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    human_size(candidate.size),
                    candidate.kind,
                    deletion_status_for_path(
                        candidate.path,
                        candidate.kind,
                        self.temp_roots,
                        self.excluded_paths.values(),
                    ),
                    candidate.reason,
                    human_time(candidate.modified),
                    candidate.path,
                ),
            )

    def _process_updates(self) -> None:
        try:
            while True:
                update = self.updates.get_nowait()
                event_type = update[0]

                if event_type == "status":
                    self.status_var.set(update[1])
                elif event_type == "progress":
                    stats: ScanStats = update[1]
                    skip_text = (
                        f"; skipped {stats.skipped_dirs:,} protected folders"
                        if not self.deep_audit_var.get()
                        else ""
                    )
                    self.status_var.set(
                        f"Scanned {stats.scanned_files:,} files and {stats.scanned_dirs:,} folders; "
                        f"found {stats.candidates_seen:,} results{skip_text}."
                    )
                elif event_type == "done":
                    candidates: list[Candidate] = update[1]
                    stats: ScanStats = update[2]
                    self._insert_candidates(candidates)
                    self._finish_scan()
                    total_candidate_size = sum(candidate.size for candidate in candidates)
                    stopped_text = " Scan was stopped early." if self.stop_event.is_set() else ""
                    result_label = "largest accessible files" if self.deep_audit_var.get() else "safe cleanup candidates"
                    self.summary_var.set(
                        f"Showing {len(candidates):,} {result_label} "
                        f"({human_size(total_candidate_size)} visible total)."
                        f"{stopped_text}"
                    )
                    self.status_var.set(
                        f"Done. Scanned {stats.scanned_files:,} files; inaccessible items: "
                        f"{stats.inaccessible_items:,}."
                    )
                elif event_type == "error":
                    self._finish_scan()
                    messagebox.showerror(APP_NAME, f"Scan failed:\n{update[1]}")
        except queue.Empty:
            pass

        self.after(150, self._process_updates)

    def _finish_scan(self) -> None:
        self.scan_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.refresh_button.configure(state="normal")
        self.delete_button.configure(state="normal" if self.tree.selection() else "disabled")

    def _on_tree_select(self, _event: tk.Event | None = None) -> None:
        self.delete_button.configure(state="normal" if self.tree.selection() else "disabled")


def main() -> None:
    app = SpaceMakerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
