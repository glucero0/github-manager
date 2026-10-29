import os
import re
import queue
import threading
import requests
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from github import Github, GithubException

try:
    from github import Auth
except ImportError:
    Auth = None

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError
except ImportError:
    keyring = None
    KeyringError = Exception
    PasswordDeleteError = Exception

KEYRING_SERVICE_NAME = "github-repository-manager"
KEYRING_ACCOUNT_NAME = "personal-access-token"


def get_stored_token() -> str | None:
    """Retrieve the GitHub PAT from the OS credential manager if available."""
    if keyring is None:
        return None
    try:
        token = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_NAME)
        return token.strip() if token and token.strip() else None
    except Exception:
        return None


def save_token_to_credential_manager(token: str) -> bool:
    """Save the GitHub PAT into the OS credential manager."""
    if keyring is None:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_NAME, token)
        return True
    except Exception:
        return False


def delete_token_from_credential_manager() -> bool:
    """Remove the stored GitHub PAT from the OS credential manager."""
    if keyring is None:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_NAME)
        return True
    except PasswordDeleteError:
        return True
    except Exception:
        return False


def sanitize_repo_filename(repo_name: str) -> str:
    """Sanitize repository name into a safe single filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name.strip())
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"Invalid repository name: {repo_name!r}")
    return f"{cleaned}.zip"


def resolve_safe_archive_path(archive_dir: str, repo_name: str) -> str:
    """
    Resolve and verify that the target ZIP path stays strictly within archive_dir.
    Prevents path traversal (CodeQL py/path-injection).
    """
    base_dir = os.path.realpath(os.path.abspath(archive_dir))
    filename = sanitize_repo_filename(repo_name)
    candidate_path = os.path.realpath(os.path.join(base_dir, filename))

    if os.path.commonpath([base_dir, candidate_path]) != base_dir or candidate_path == base_dir:
        raise ValueError("Resolved file path escapes the archive directory.")
    return candidate_path


def format_relative_activity(dt: datetime | None) -> tuple[str, float]:
    """Return formatted date + relative age string and a numeric sort key (timestamp)."""
    if not dt:
        return ("N/A", 0.0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    days = max(0, delta.days)
    if days == 0:
        hours = max(0, int(delta.total_seconds() // 3600))
        rel = f"{hours}h ago" if hours > 0 else "just now"
    elif days < 30:
        rel = f"{days}d ago"
    elif days < 365:
        rel = f"{days // 30}mo ago"
    else:
        rel = f"{days // 365}y ago"
    return (f"{dt.strftime('%Y-%m-%d')} ({rel})", dt.timestamp())


def format_size_kb(size_kb: int | None) -> str:
    """Convert GitHub repo.size (in KB) into human-readable string."""
    if size_kb is None:
        return "0 KB"
    if size_kb < 1024:
        return f"{size_kb} KB"
    mb = size_kb / 1024.0
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024.0
    return f"{gb:.2f} GB"


class GitHubManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GitHub Repository Manager")
        self.root.geometry("980x640")
        self.root.minsize(820, 520)

        self.github: Github | None = None
        self.token: str | None = None
        self.username: str | None = None
        self.repos: list = []
        self.repo_by_name: dict = {}
        self.sort_state: dict[str, bool] = {}
        self.is_busy: bool = False

        self.ui_queue: queue.Queue = queue.Queue()

        self.setup_styles()
        self.setup_ui()
        self.poll_ui_queue()

        # Automatically connect using GITHUB_TOKEN env var or OS Credential Manager
        env_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if env_token:
            self.connect_with_token(env_token, silent_prompt=True)
        else:
            stored_token = get_stored_token()
            if stored_token:
                self.connect_with_token(stored_token, silent_prompt=True)

    def setup_styles(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Danger.TButton", foreground="#a80000")

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Top Connection Bar
        conn_frame = ttk.LabelFrame(main_frame, text="GitHub Account", padding=(10, 8))
        conn_frame.pack(fill=tk.X, pady=(0, 8))

        self.conn_status_var = tk.StringVar(value="Not connected")
        self.conn_status_label = ttk.Label(
            conn_frame, textvariable=self.conn_status_var, style="Header.TLabel"
        )
        self.conn_status_label.pack(side=tk.LEFT, padx=(0, 12))

        self.connect_button = ttk.Button(
            conn_frame, text="Connect / Change Token", command=self.setup_github
        )
        self.connect_button.pack(side=tk.LEFT, padx=4)

        self.retrieve_button = ttk.Button(
            conn_frame, text="Refresh Repos", command=self.retrieve_repos, state=tk.DISABLED
        )
        self.retrieve_button.pack(side=tk.LEFT, padx=4)

        self.clear_token_button = ttk.Button(
            conn_frame,
            text="Clear Saved Token",
            command=self.clear_saved_token,
            state=tk.NORMAL if get_stored_token() else tk.DISABLED,
        )
        self.clear_token_button.pack(side=tk.LEFT, padx=4)

        self.repo_count_var = tk.StringVar(value="")
        ttk.Label(conn_frame, textvariable=self.repo_count_var).pack(side=tk.RIGHT, padx=4)

        # 2. Archive Folder Bar
        archive_frame = ttk.LabelFrame(main_frame, text="Local Backup Archive", padding=(10, 8))
        archive_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(archive_frame, text="Archive Folder:").pack(side=tk.LEFT, padx=(0, 6))

        self.archive_folder_var = tk.StringVar()
        self.archive_folder_var.trace_add("write", lambda *_: self.refresh_backup_column())
        self.archive_folder_entry = ttk.Entry(
            archive_frame, textvariable=self.archive_folder_var
        )
        self.archive_folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.browse_button = ttk.Button(
            archive_frame, text="Browse...", command=self.browse_folder
        )
        self.browse_button.pack(side=tk.LEFT, padx=4)

        self.skip_existing_var = tk.BooleanVar(value=True)
        self.skip_existing_check = ttk.Checkbutton(
            archive_frame,
            text="Skip already downloaded",
            variable=self.skip_existing_var,
        )
        self.skip_existing_check.pack(side=tk.LEFT, padx=6)

        self.download_selected_button = ttk.Button(
            archive_frame,
            text="Download Selected (ZIP)",
            command=self.download_selected_repos,
            state=tk.DISABLED,
        )
        self.download_selected_button.pack(side=tk.LEFT, padx=4)

        self.download_all_button = ttk.Button(
            archive_frame,
            text="Download All (ZIP)",
            command=self.download_all_repos,
            state=tk.DISABLED,
        )
        self.download_all_button.pack(side=tk.LEFT, padx=4)

        # 3. Filter & Selection Bar
        toolbar_frame = ttk.Frame(main_frame)
        toolbar_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(toolbar_frame, text="Search / Filter:").pack(side=tk.LEFT, padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.populate_treeview())
        self.search_entry = ttk.Entry(toolbar_frame, textvariable=self.search_var, width=32)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 12))

        self.select_all_button = ttk.Button(
            toolbar_frame, text="Select All", command=self.select_all_repos
        )
        self.select_all_button.pack(side=tk.LEFT, padx=2)

        self.deselect_all_button = ttk.Button(
            toolbar_frame, text="Deselect All", command=self.deselect_all_repos
        )
        self.deselect_all_button.pack(side=tk.LEFT, padx=2)

        self.selection_count_var = tk.StringVar(value="0 selected")
        ttk.Label(toolbar_frame, textvariable=self.selection_count_var).pack(
            side=tk.RIGHT, padx=4
        )

        # 4. Repository Table (Treeview)
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        columns = ("name", "visibility", "last_activity", "size", "backup_status")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="extended"
        )

        headings = {
            "name": ("Repository Name", 280, tk.W),
            "visibility": ("Visibility", 100, tk.CENTER),
            "last_activity": ("Last Activity", 200, tk.W),
            "size": ("Size", 110, tk.E),
            "backup_status": ("Local Backup", 150, tk.CENTER),
        }

        for col, (title, width, anchor) in headings.items():
            self.tree.heading(col, text=title, command=lambda c=col: self.sort_by_column(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))

        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self.on_selection_change)

        # 5. Bottom Action & Progress Bar
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.X)

        self.delete_button = ttk.Button(
            bottom_frame,
            text="Delete Selected from GitHub...",
            style="Danger.TButton",
            command=self.delete_selected_repos,
            state=tk.DISABLED,
        )
        self.delete_button.pack(side=tk.LEFT, padx=(0, 12))

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progressbar = ttk.Progressbar(
            bottom_frame, variable=self.progress_var, maximum=100.0, length=220, mode="determinate"
        )
        self.progressbar.pack(side=tk.RIGHT, padx=(8, 0))

        self.status_var = tk.StringVar(value="Ready. Connect to GitHub to load your repositories.")
        self.status_label = ttk.Label(
            bottom_frame, textvariable=self.status_var, style="Status.TLabel"
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # --- Thread-safe UI Queue Polling ---

    def enqueue_ui(self, callback, *args, **kwargs):
        self.ui_queue.put((callback, args, kwargs))

    def poll_ui_queue(self):
        try:
            while True:
                callback, args, kwargs = self.ui_queue.get_nowait()
                callback(*args, **kwargs)
        except queue.Empty:
            pass
        self.root.after(80, self.poll_ui_queue)

    def set_busy(self, busy: bool, status_text: str | None = None):
        self.is_busy = busy
        if status_text is not None:
            self.status_var.set(status_text)

        connected = self.github is not None
        btn_state = tk.DISABLED if busy else (tk.NORMAL if connected else tk.DISABLED)

        self.connect_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.browse_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.skip_existing_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.retrieve_button.configure(state=btn_state)
        self.clear_token_button.configure(
            state=tk.DISABLED if busy else (tk.NORMAL if get_stored_token() else tk.DISABLED)
        )
        self.download_all_button.configure(
            state=btn_state if self.repos else tk.DISABLED
        )
        self.update_selection_buttons_state()

    def update_selection_buttons_state(self):
        if self.is_busy or not self.github:
            self.delete_button.configure(state=tk.DISABLED)
            self.download_selected_button.configure(state=tk.DISABLED)
            return
        selected = self.tree.selection()
        state = tk.NORMAL if selected else tk.DISABLED
        self.delete_button.configure(state=state)
        self.download_selected_button.configure(state=state)

    def on_selection_change(self, _event=None):
        selected = self.tree.selection()
        self.selection_count_var.set(f"{len(selected)} selected")
        self.update_selection_buttons_state()

    def select_all_repos(self):
        items = self.tree.get_children()
        self.tree.selection_set(items)
        self.on_selection_change()

    def deselect_all_repos(self):
        self.tree.selection_remove(self.tree.selection())
        self.on_selection_change()

    # --- 1. Authentication & Repository Retrieval ---

    def setup_github(self):
        if self.is_busy:
            return
        token, remember = self.show_token_dialog()
        if token:
            self.connect_with_token(token, silent_prompt=False, save_to_keyring=remember)

    def show_token_dialog(self) -> tuple[str | None, bool]:
        dialog = tk.Toplevel(self.root)
        dialog.title("Connect to GitHub")
        dialog.geometry("460x230")
        dialog.minsize(420, 210)
        dialog.transient(self.root)
        dialog.grab_set()

        result: dict[str, object] = {"token": None, "remember": False}

        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text="Enter your GitHub Personal Access Token\n"
            "(requires 'repo' scope, plus 'delete_repo' to delete repositories):",
            wraplength=420,
        ).pack(anchor=tk.W, pady=(0, 8))

        token_var = tk.StringVar()
        token_entry = ttk.Entry(container, textvariable=token_var, show="*", font=("Segoe UI", 10))
        token_entry.pack(fill=tk.X, pady=(0, 10))
        token_entry.focus_set()

        remember_var = tk.BooleanVar(value=keyring is not None)
        remember_check = ttk.Checkbutton(
            container,
            text="Save token in OS Credential Manager (Windows Credential Locker / Keychain)",
            variable=remember_var,
            state=tk.NORMAL if keyring is not None else tk.DISABLED,
        )
        remember_check.pack(anchor=tk.W, pady=(0, 14))

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=tk.X)

        def on_ok(_event=None):
            val = token_var.get().strip()
            if val:
                result["token"] = val
                result["remember"] = bool(remember_var.get())
                dialog.destroy()

        def on_cancel(_event=None):
            dialog.destroy()

        connect_btn = ttk.Button(btn_frame, text="Connect", command=on_ok)
        connect_btn.pack(side=tk.RIGHT, padx=(6, 0))

        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=on_cancel)
        cancel_btn.pack(side=tk.RIGHT)

        dialog.bind("<Return>", on_ok)
        dialog.bind("<Escape>", on_cancel)

        self.root.wait_window(dialog)
        return result["token"], bool(result["remember"])  # type: ignore[return-value]

    def clear_saved_token(self):
        if self.is_busy:
            return
        if not messagebox.askyesno(
            "Clear Saved Token",
            "Remove the saved GitHub Personal Access Token from your OS Credential Manager\n"
            "and disconnect the current session?",
            parent=self.root,
        ):
            return

        delete_token_from_credential_manager()
        self.github = None
        self.token = None
        self.username = None
        self.repos = []
        self.repo_by_name = {}
        self.tree.delete(*self.tree.get_children())
        self.conn_status_var.set("Not connected")
        self.repo_count_var.set("")
        self.selection_count_var.set("0 selected")
        self.progress_var.set(0.0)
        self.set_busy(False, "Saved token removed from OS Credential Manager and disconnected.")

    def connect_with_token(
        self, token: str, silent_prompt: bool = False, save_to_keyring: bool = False
    ):
        self.set_busy(True, "Connecting to GitHub and verifying token...")
        self.progress_var.set(0)

        def worker():
            try:
                if Auth is not None:
                    gh = Github(auth=Auth.Token(token), timeout=30)
                else:
                    gh = Github(token, timeout=30)
                user = gh.get_user()
                login = user.login  # Forces immediate credential verification
                self.enqueue_ui(
                    self._on_connect_success, gh, token, login, silent_prompt, save_to_keyring
                )
            except GithubException as exc:
                msg = f"GitHub authentication failed ({exc.status}). Please verify your token."
                self.enqueue_ui(self._on_connect_error, msg, silent_prompt)
            except Exception as exc:
                self.enqueue_ui(
                    self._on_connect_error,
                    f"Could not connect to GitHub: {type(exc).__name__}",
                    silent_prompt,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_connect_success(
        self,
        gh: Github,
        token: str,
        login: str,
        silent_prompt: bool,
        save_to_keyring: bool = False,
    ):
        self.github = gh
        self.token = token
        self.username = login
        if save_to_keyring:
            save_token_to_credential_manager(token)
        self.conn_status_var.set(f"Connected as @{login}")
        self.set_busy(False, f"Connected as @{login}. Fetching owned repositories...")
        if not silent_prompt:
            messagebox.showinfo(
                "Connected", f"Successfully authenticated as @{login}.", parent=self.root
            )
        self.retrieve_repos()

    def _on_connect_error(self, error_message: str, silent_prompt: bool):
        self.set_busy(False, "Authentication failed.")
        if not silent_prompt:
            messagebox.showerror("Authentication Error", error_message, parent=self.root)

    def retrieve_repos(self):
        if not self.github:
            messagebox.showerror("Error", "Please connect to GitHub first.", parent=self.root)
            return
        if self.is_busy:
            return

        self.set_busy(True, "Retrieving your owned repositories from GitHub...")
        self.progress_var.set(0)

        def worker():
            try:
                user = self.github.get_user()
                owned_repos = list(user.get_repos(affiliation="owner", sort="updated"))
                self.enqueue_ui(self._on_retrieve_success, owned_repos)
            except GithubException as exc:
                self.enqueue_ui(
                    self._on_retrieve_error,
                    f"Failed to retrieve repositories (HTTP {exc.status}).",
                )
            except Exception as exc:
                self.enqueue_ui(
                    self._on_retrieve_error,
                    f"Unexpected error retrieving repositories: {exc}",
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_retrieve_success(self, owned_repos: list):
        self.repos = owned_repos
        self.repo_by_name = {r.name: r for r in owned_repos}
        self.repo_count_var.set(f"{len(owned_repos)} owned repositories")
        self.populate_treeview()
        self.progress_var.set(100)
        self.set_busy(False, f"Loaded {len(owned_repos)} owned repositories.")

    def _on_retrieve_error(self, error_message: str):
        self.set_busy(False, "Failed to load repositories.")
        messagebox.showerror("Error", error_message, parent=self.root)

    # --- 2. Treeview Display, Backup Status & Sorting ---

    def is_repo_backed_up(self, repo_name: str) -> bool:
        archive_folder = self.archive_folder_var.get().strip()
        if not archive_folder or not os.path.isdir(archive_folder):
            return False
        try:
            safe_path = resolve_safe_archive_path(archive_folder, repo_name)
            return os.path.isfile(safe_path) and os.path.getsize(safe_path) > 0
        except ValueError:
            return False

    def populate_treeview(self):
        selected_names = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())

        query = self.search_var.get().strip().lower()
        for repo in self.repos:
            if query and query not in repo.name.lower():
                continue

            visibility = "Private" if repo.private else "Public"
            activity_dt = repo.pushed_at or repo.updated_at or repo.created_at
            activity_str, _ = format_relative_activity(activity_dt)
            size_str = format_size_kb(repo.size)
            backup_str = "Backed Up" if self.is_repo_backed_up(repo.name) else "Not Backed Up"

            self.tree.insert(
                "",
                tk.END,
                iid=repo.name,
                values=(repo.name, visibility, activity_str, size_str, backup_str),
            )
            if repo.name in selected_names:
                self.tree.selection_add(repo.name)

        self.on_selection_change()

    def refresh_backup_column(self):
        for iid in self.tree.get_children():
            current_vals = list(self.tree.item(iid, "values"))
            if len(current_vals) == 5:
                current_vals[4] = "Backed Up" if self.is_repo_backed_up(iid) else "Not Backed Up"
                self.tree.item(iid, values=current_vals)

    def sort_by_column(self, col: str):
        reverse = self.sort_state.get(col, False)
        self.sort_state[col] = not reverse

        def sort_key(repo):
            if col == "name":
                return repo.name.lower()
            if col == "visibility":
                return "Private" if repo.private else "Public"
            if col == "last_activity":
                dt = repo.pushed_at or repo.updated_at or repo.created_at
                _, ts = format_relative_activity(dt)
                return ts
            if col == "size":
                return repo.size or 0
            if col == "backup_status":
                return 1 if self.is_repo_backed_up(repo.name) else 0
            return repo.name.lower()

        self.repos.sort(key=sort_key, reverse=reverse)
        self.populate_treeview()

    def browse_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="Select Archive Folder")
        if folder:
            self.archive_folder_var.set(folder)

    # --- 3. ZIP Archive Download Implementation ---

    def download_all_repos(self):
        if not self.repos:
            messagebox.showinfo(
                "No Repositories", "There are no repositories to download.", parent=self.root
            )
            return
        self._start_repos_download(list(self.repos))

    def download_selected_repos(self):
        selected_names = list(self.tree.selection())
        if not selected_names:
            messagebox.showerror(
                "Error", "Please select one or more repositories to download.", parent=self.root
            )
            return
        selected_repos = [
            self.repo_by_name[name] for name in selected_names if name in self.repo_by_name
        ]
        if not selected_repos:
            return
        self._start_repos_download(selected_repos)

    def _start_repos_download(self, repos_to_download: list):
        if not self.github or not self.token:
            messagebox.showerror("Error", "Please connect to GitHub first.", parent=self.root)
            return
        if self.is_busy:
            return

        archive_folder = self.archive_folder_var.get().strip()
        if not archive_folder:
            messagebox.showerror(
                "Error", "Please select an Archive Folder first.", parent=self.root
            )
            return

        try:
            os.makedirs(archive_folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Folder Error",
                f"Cannot create or access archive folder:\n{exc}",
                parent=self.root,
            )
            return

        total = len(repos_to_download)
        skip_existing = self.skip_existing_var.get()
        self.set_busy(True, f"Starting ZIP download for {total} repository(s)...")
        self.progress_var.set(0)

        token = self.token

        def worker():
            downloaded = 0
            skipped_existing = []
            empty_repos = []
            failed_repos = []

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            for idx, repo in enumerate(repos_to_download, start=1):
                try:
                    target_path = resolve_safe_archive_path(archive_folder, repo.name)
                except ValueError as exc:
                    failed_repos.append(f"{repo.name} ({exc})")
                    continue

                if skip_existing and os.path.isfile(target_path) and os.path.getsize(target_path) > 0:
                    skipped_existing.append(repo.name)
                    self.enqueue_ui(
                        self._update_download_progress,
                        idx,
                        total,
                        f"Skipping already downloaded ({idx}/{total}): {repo.name}",
                    )
                    continue

                self.enqueue_ui(
                    self._update_download_progress,
                    idx - 1,
                    total,
                    f"Downloading ({idx}/{total}): {repo.name}...",
                )

                # Empty repositories (size == 0 and no commits) may return 404/409 on zipball
                ref = repo.default_branch or ""
                url = f"https://api.github.com/repos/{repo.full_name}/zipball/{ref}".rstrip("/")

                try:
                    with requests.get(
                        url, headers=headers, stream=True, timeout=30
                    ) as response:
                        if response.status_code in (404, 409) and (repo.size == 0):
                            empty_repos.append(repo.name)
                            continue
                        response.raise_for_status()

                        temp_path = f"{target_path}.part"
                        with open(temp_path, "wb") as fh:
                            for chunk in response.iter_content(chunk_size=65536):
                                if chunk:
                                    fh.write(chunk)
                        os.replace(temp_path, target_path)
                        downloaded += 1
                        self.enqueue_ui(self.refresh_backup_column)
                except requests.exceptions.HTTPError as exc:
                    status_code = exc.response.status_code if exc.response is not None else "HTTP error"
                    if status_code in (404, 409) and (repo.size == 0):
                        empty_repos.append(repo.name)
                    else:
                        failed_repos.append(f"{repo.name} (HTTP {status_code})")
                except Exception as exc:
                    failed_repos.append(f"{repo.name} ({type(exc).__name__})")

            self.enqueue_ui(
                self._on_download_complete,
                total,
                downloaded,
                skipped_existing,
                empty_repos,
                failed_repos,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _update_download_progress(self, completed: int, total: int, status_text: str):
        pct = (completed / total) * 100.0 if total > 0 else 0.0
        self.progress_var.set(pct)
        self.status_var.set(status_text)

    def _on_download_complete(
        self,
        total: int,
        downloaded: int,
        skipped_existing: list[str],
        empty_repos: list[str],
        failed_repos: list[str],
    ):
        self.progress_var.set(100.0)
        self.refresh_backup_column()
        self.set_busy(
            False,
            f"Download complete: {downloaded} downloaded, {len(skipped_existing)} skipped ({total} total).",
        )

        summary_lines = [
            f"Downloaded {downloaded} of {total} repository(s) as ZIP archives."
        ]
        if skipped_existing:
            summary_lines.append(
                f"\nSkipped {len(skipped_existing)} already-downloaded repository(s)."
            )
        if empty_repos:
            summary_lines.append(
                f"\nSkipped {len(empty_repos)} empty repository(s) (no commits):\n- "
                + "\n- ".join(empty_repos[:10])
            )
        if failed_repos:
            summary_lines.append(
                f"\nFailed to download {len(failed_repos)} repository(s):\n- "
                + "\n- ".join(failed_repos[:10])
            )
            messagebox.showwarning(
                "Download Summary", "\n".join(summary_lines), parent=self.root
            )
        else:
            messagebox.showinfo(
                "Download Complete", "\n".join(summary_lines), parent=self.root
            )

    # --- 4. Strict Deletion Safeguards & Auto-Refresh ---

    def delete_selected_repos(self):
        if not self.github:
            messagebox.showerror("Error", "Please connect to GitHub first.", parent=self.root)
            return
        if self.is_busy:
            return

        selected_names = list(self.tree.selection())
        if not selected_names:
            messagebox.showerror(
                "Error", "Please select one or more repositories to delete.", parent=self.root
            )
            return

        selected_repos = [
            self.repo_by_name[name] for name in selected_names if name in self.repo_by_name
        ]
        if not selected_repos:
            return

        unbacked = [r.name for r in selected_repos if not self.is_repo_backed_up(r.name)]

        confirmed = self.show_type_to_confirm_dialog(selected_repos, unbacked)
        if not confirmed:
            return

        total = len(selected_repos)
        self.set_busy(True, f"Deleting {total} repository(s) from GitHub...")
        self.progress_var.set(0)

        def worker():
            deleted = []
            failed = []
            missing_scope = False

            for idx, repo in enumerate(selected_repos, start=1):
                self.enqueue_ui(
                    self._update_download_progress,
                    idx - 1,
                    total,
                    f"Deleting ({idx}/{total}): {repo.full_name}...",
                )
                try:
                    repo.delete()
                    deleted.append(repo.name)
                except GithubException as exc:
                    if exc.status == 403:
                        missing_scope = True
                        failed.append(f"{repo.name} (403 Forbidden - missing 'delete_repo' scope)")
                    else:
                        failed.append(f"{repo.name} (HTTP {exc.status})")
                except Exception as exc:
                    failed.append(f"{repo.name} ({type(exc).__name__})")

            self.enqueue_ui(
                self._on_delete_complete, deleted, failed, missing_scope
            )

        threading.Thread(target=worker, daemon=True).start()

    def show_type_to_confirm_dialog(
        self, selected_repos: list, unbacked_names: list[str]
    ) -> bool:
        dialog = tk.Toplevel(self.root)
        dialog.title("Confirm Permanent Repository Deletion")
        dialog.geometry("540x460")
        dialog.minsize(480, 400)
        dialog.transient(self.root)
        dialog.grab_set()

        confirmed = {"value": False}
        required_phrase = (
            selected_repos[0].name if len(selected_repos) == 1 else "DELETE"
        )

        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(
            container,
            text=f"You are about to permanently delete {len(selected_repos)} repository(s) from GitHub.",
            style="Header.TLabel",
            wraplength=500,
        )
        header.pack(anchor=tk.W, pady=(0, 8))

        ttk.Label(
            container,
            text="This action CANNOT be undone. The following repositories will be deleted:",
            wraplength=500,
        ).pack(anchor=tk.W, pady=(0, 6))

        list_frame = ttk.Frame(container)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        text_box = tk.Text(list_frame, height=7, wrap=tk.NONE, font=("Consolas", 10))
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=text_box.yview)
        text_box.configure(yscrollcommand=scroll.set)
        text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for repo in selected_repos:
            backup_tag = (
                "[Backed Up]" if repo.name not in unbacked_names else "[NOT BACKED UP]"
            )
            text_box.insert(tk.END, f"• {repo.full_name}  {backup_tag}\n")
        text_box.configure(state=tk.DISABLED)

        if unbacked_names:
            warn_label = tk.Label(
                container,
                text=(
                    f"WARNING: {len(unbacked_names)} selected repository(s) do NOT have a local "
                    "ZIP backup in the currently selected Archive Folder!"
                ),
                fg="#a80000",
                font=("Segoe UI", 9, "bold"),
                wraplength=500,
                justify=tk.LEFT,
            )
            warn_label.pack(anchor=tk.W, pady=(0, 10))

        prompt_label = ttk.Label(
            container,
            text=f"To confirm, type '{required_phrase}' below:",
            font=("Segoe UI", 10, "bold"),
        )
        prompt_label.pack(anchor=tk.W, pady=(0, 4))

        confirm_var = tk.StringVar()
        confirm_entry = ttk.Entry(container, textvariable=confirm_var, font=("Segoe UI", 10))
        confirm_entry.pack(fill=tk.X, pady=(0, 14))
        confirm_entry.focus_set()

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=tk.X)

        def on_confirm():
            if confirm_var.get() == required_phrase:
                confirmed["value"] = True
                dialog.destroy()

        def on_cancel():
            dialog.destroy()

        delete_btn = ttk.Button(
            btn_frame,
            text="Permanently Delete",
            style="Danger.TButton",
            command=on_confirm,
            state=tk.DISABLED,
        )
        delete_btn.pack(side=tk.RIGHT, padx=(6, 0))

        cancel_btn = ttk.Button(btn_frame, text="Cancel", command=on_cancel)
        cancel_btn.pack(side=tk.RIGHT)

        def on_entry_change(*_):
            if confirm_var.get() == required_phrase:
                delete_btn.configure(state=tk.NORMAL)
            else:
                delete_btn.configure(state=tk.DISABLED)

        confirm_var.trace_add("write", on_entry_change)
        dialog.bind("<Escape>", lambda _e: on_cancel())

        self.root.wait_window(dialog)
        return confirmed["value"]

    def _on_delete_complete(
        self, deleted: list[str], failed: list[str], missing_scope: bool
    ):
        self.progress_var.set(100.0)
        self.set_busy(False, f"Deleted {len(deleted)} repository(s).")

        if failed:
            msg_lines = [
                f"Deleted {len(deleted)} repository(s).",
                f"Failed to delete {len(failed)} repository(s):",
                "",
                *[f"- {item}" for item in failed[:10]],
            ]
            if missing_scope:
                msg_lines.extend(
                    [
                        "",
                        "How to fix 403 Forbidden:",
                        "Go to GitHub Settings -> Developer settings -> Personal access tokens,",
                        "and enable the 'delete_repo' scope (or Administration: Read and write",
                        "for fine-grained tokens), then reconnect.",
                    ]
                )
            messagebox.showerror("Deletion Errors", "\n".join(msg_lines), parent=self.root)
        else:
            messagebox.showinfo(
                "Deletion Complete",
                f"Successfully deleted {len(deleted)} repository(s) from GitHub.",
                parent=self.root,
            )

        # Auto-refresh screen after deletion attempt
        self.retrieve_repos()


if __name__ == "__main__":
    root = tk.Tk()
    app = GitHubManager(root)
    root.mainloop()
