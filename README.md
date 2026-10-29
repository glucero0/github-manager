# GitHub Repository Manager

A desktop GUI application built with Python and Tkinter for inspecting, backing up, and safely cleaning up your owned GitHub repositories.

---

## Features

- **Flexible & Secure GitHub Authentication**
  - Connect interactively using a GitHub Personal Access Token (PAT), automatically via your **OS Credential Manager** (Windows Credential Locker, macOS Keychain, or Linux Secret Service via `keyring`), or via the `GITHUB_TOKEN` environment variable.
  - Optional checkbox in the connection dialog lets you save your verified token to the OS Credential Manager so future launches sign in automatically, with a **Clear Saved Token** button to remove stored credentials at any time.
  - Supports modern `PyGithub` token authentication (`Auth.Token`) with immediate credential verification.

- **Interactive Repository Dashboard**
  - Lists all repositories owned by the authenticated user.
  - Displays **Repository Name**, **Visibility** (`Public` / `Private`), **Last Activity** (date and relative age), **Size** (auto-formatted in KB, MB, or GB), and **Local Backup** status (`Backed Up` / `Not Backed Up`).
  - Click any column header to sort ascending or descending.
  - Filter repositories in real time with the **Search / Filter** bar, and manage selections with **Select All** / **Deselect All** or standard multi-select (`Ctrl+Click` / `Shift+Click`).

- **Local ZIP Backup Archives**
  - Download **Selected** or **All** owned repositories as `.zip` archives directly from GitHub's REST API (`zipball` endpoint).
  - **Incremental Backups**: Enable *Skip already downloaded* (checked by default) to skip repositories that already have a non-empty `.zip` file in the chosen archive folder.
  - **Atomic Downloads**: Streams archives to a temporary `.part` file first and atomically renames it to `.zip` upon completion, ensuring interrupted downloads never leave corrupted archives marked as backed up.
  - **Empty Repository Detection**: Gracefully skips and summarizes repositories that have no commits (`size == 0`).

- **Strict Deletion Safeguards**
  - Warns prominently in red if any selected repositories do not have a verified local `.zip` backup in the current **Archive Folder**.
  - **Type-to-Confirm Dialog**: Prevents accidental deletion by requiring you to type the exact repository name (when deleting 1 repository) or `DELETE` (when deleting multiple repositories) before the **Permanently Delete** button is enabled.
  - Automatically refreshes your repository list after deletion and provides clear troubleshooting steps if your token lacks deletion permissions (`403 Forbidden`).

- **Security-First Design**
  - **Path Traversal Protection**: Repository filenames are strictly sanitized and resolved with `os.path.realpath` and `os.path.commonpath` checks to prevent path injection (`CodeQL py/path-injection`).
  - **Zero Plaintext Credential Files**: Tokens are never written to plaintext files or logs; persistent storage uses the native OS Credential Manager (`keyring`) only after successful authentication.
  - **Responsive UI**: All network and disk operations run on background worker threads communicating via a thread-safe UI queue.

---

## Requirements

- **Python** 3.10 or newer (includes `tkinter` in standard Windows/macOS installers; on Linux, install `python3-tk` if needed)
- Dependencies listed in `requirements.txt`:
  - `PyGithub>=2.3.0`
  - `requests>=2.32.0`
  - `keyring>=25.2.0`

---

## Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd github-manager
   ```

2. **Create and activate a virtual environment (recommended):**
   ```powershell
   # Windows (PowerShell)
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   ```bash
   # macOS / Linux
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## Configuring & Storing a GitHub Personal Access Token

To list, download, and optionally delete repositories, create a Personal Access Token on GitHub under **Settings → Developer settings → Personal access tokens**:

- **Personal access tokens (classic):**
  - `repo` — Required to list and download private and public repositories.
  - `delete_repo` — Required **only** if you intend to delete repositories from GitHub.
- **Fine-grained personal access tokens:**
  - **Repository access**: *All repositories* (or selected repositories you want to manage).
  - **Permissions**:
    - **Metadata**: *Read-only*
    - **Contents**: *Read-only* (required for downloading ZIP archives)
    - **Administration**: *Read and write* (required **only** for deleting repositories)

### Where Is the PAT Stored?

On startup, the application checks for a token in the following order:

1. **`GITHUB_TOKEN` Environment Variable**: If set in your environment, the application uses it for the current session without modifying your saved OS credentials.
2. **OS Credential Manager (`keyring`)**: When you connect via **Connect / Change Token** and leave *"Save token in OS Credential Manager"* checked, the verified token is encrypted and stored in your operating system's native credential vault:
   - **Windows**: Windows Credential Manager (*Generic Credentials* under service `github-repository-manager` / account `personal-access-token`)
   - **macOS**: macOS Keychain
   - **Linux**: Freedesktop Secret Service (GNOME Keyring / KWallet)
3. **Session-Only Memory**: If you uncheck *"Save token in OS Credential Manager"*, the token is held strictly in memory (`self.token`) for the active process and discarded when the window closes.

To remove a saved token at any time, click **Clear Saved Token** in the top **GitHub Account** bar.

> **Security Tip:** Grant the `delete_repo` / `Administration: Read and write` permission only when you plan to delete repositories, and revoke or rotate tokens when finished.

---

## Usage

1. **Start the application:**
   ```bash
   python app.py
   ```
   *(Optional)* You can also pass a one-time token via the `GITHUB_TOKEN` environment variable:
   ```powershell
   # Windows (PowerShell)
   $env:GITHUB_TOKEN="ghp_your_token_here"
   python app.py
   ```
   ```bash
   # macOS / Linux
   GITHUB_TOKEN="ghp_your_token_here" python app.py
   ```

2. **Connect to GitHub:**
   - Click **Connect / Change Token**, paste your Personal Access Token, and choose whether to save it in your OS Credential Manager.
   - Once authenticated, your owned repositories are automatically fetched and displayed.
   - Click **Clear Saved Token** at any time to wipe the token from your OS Credential Manager and disconnect.

3. **Back Up Repositories to Local ZIP Files:**
   - Click **Browse...** in the **Local Backup Archive** section to choose a destination folder.
   - The **Local Backup** column immediately updates to show which repositories already have a `.zip` backup in that folder.
   - Click **Download Selected (ZIP)** or **Download All (ZIP)** to back up your repositories.

4. **Delete Repositories from GitHub:**
   - Select one or more repositories in the table and click **Delete Selected from GitHub...**.
   - Review the list of repositories and their backup status (`[Backed Up]` vs. `[NOT BACKED UP]`).
   - Type the confirmation phrase (`<repo-name>` for a single repository, or `DELETE` for multiple repositories) and click **Permanently Delete**.
