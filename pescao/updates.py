"""MAKO Renderer updates: find the latest stable Renderer release on GitHub and install
it with MAKO's own installer, the way MAKO documents for standalone installs ("run the
installer again to update").

Pure stdlib, no Invasor imports: tested on its own (tests/test_updates.py). The backend reads
the release list and downloads with ctx.http (checked against the sha256 GitHub publishes for each
file), and unpacks with the `extract` it passes in (ctx.archive.extract).

- What's installed is read from MAKO itself: its layer library carries its version as
  a string ("4.0.0"), and ~/.local/share/mako-render/active-renderer.json (written by
  both of MAKO's installers) says who owns it, "standalone" or "decky". A Renderer
  managed by MAKO Decky (or installed outside ~/.local) is never touched.
- Releases: GitHub's release list for eugeniosegala/MAKO. Renderer releases are tagged
  render-vX.Y.Z (MAKO Decky's are plugin-v…); drafts and pre-releases are skipped.
- Installing: only what a Renderer archive holds is unpacked to a temporary folder
  (extract refuses links and paths leading out), its version file must say the expected
  version, and then its own "Install MAKO Renderer" runs without questions
  (MAKO_INSTALLER_ASSUME_YES=1, MAKO_INSTALLER_NO_LAUNCH=1, no display). That installer checks each file against its
  manifest, installs in one transaction with rollback and keeps the profiles.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = "eugeniosegala/MAKO"
RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
MAX_RELEASES = 4 << 20  # GitHub's answer for 30 releases is about 300 KB
INSTALL_TIMEOUT = 180
PREFIX = Path.home() / ".local"
LAYER_JSON = "VkLayer_MAKO_render.json"
STATE = "share/mako-render/active-renderer.json"
# Where Vulkan looks for implicit layers: the user's first, then the system's.
LAYER_DIRS = (
    PREFIX / "share/vulkan/implicit_layer.d",
    Path("/usr/local/share/vulkan/implicit_layer.d"),
    Path("/usr/share/vulkan/implicit_layer.d"),
    Path("/etc/vulkan/implicit_layer.d"),
)

# What a Renderer archive holds at its top level.
INSTALLER = "Install MAKO Renderer"
VERSION_FILE = "MAKO-Renderer-version.txt"
MANIFEST = "MAKO-Renderer-install-manifest.txt"
TOP_DIRS = ("bin", "lib", "lib32", "share")
TOP_FILES = (INSTALLER, VERSION_FILE, MANIFEST, "README.txt")

TAG = re.compile(r"^render-v(\d+\.\d+\.\d+)$")
DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?")
# Version strings inside the layer library: whole NUL-separated strings.
EMBEDDED = re.compile(rb"(?<![\w.-])(\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[0-9A-Za-z.]{1,20})?)\x00")

# The MAKO series this Pescao was written and tested against. Same major, newer minor:
# usable (new options just don't show here). Another major: never installed by Pescao.
TESTED = (4, 6)
SUPPORTED_MAJOR = 4


class UpdateError(Exception):
    pass


def parse_version(text):
    """Sortable version, or None: 4.0.0-rc1 < 4.0.0 < 4.0.1."""
    m = VERSION.search(text or "")
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    stage = (0, pre) if pre else (1, "")  # a pre-release sorts before its release
    return (int(major), int(minor), int(patch), stage)


def compatibility(version):
    """"tested", "newer_minor", "older" or "unsupported" (another major version); None if unknown."""
    v = parse_version(version)
    if v is None:
        return None
    if v[0] != SUPPORTED_MAJOR:
        return "unsupported"
    if (v[0], v[1]) == TESTED:
        return "tested"
    return "newer_minor" if (v[0], v[1]) > TESTED else "older"


def compare(installed, latest):
    """"newer_available", "up_to_date" or "ahead" (installed is past the latest stable).
    An unknown installed version can always take the stable."""
    new = parse_version(latest)
    if new is None:
        raise UpdateError(f"can't read the latest version {latest!r}")
    have = parse_version(installed)
    if have is None or have < new:
        return "newer_available"
    return "up_to_date" if have == new else "ahead"


def asset_name(version):
    return f"MAKO-Renderer-v{version}-linux.tar.xz"


def latest_stable(releases):
    """{version, url, sha256} of the newest stable Renderer release in GitHub's release list
    (sha256: of its Linux archive, as GitHub publishes it; None if it has none)."""
    best = None
    for r in releases if isinstance(releases, list) else []:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        m = TAG.match(str(r.get("tag_name", "")))
        if not m:
            continue  # MAKO Decky's releases and anything else
        version = m.group(1)
        asset = next((a for a in r.get("assets") or []
                      if isinstance(a, dict) and a.get("name") == asset_name(version)), {})
        url = asset.get("browser_download_url")
        if url != f"{DOWNLOAD_PREFIX}render-v{version}/{asset_name(version)}":
            continue  # no Linux archive (or an unexpected link)
        digest = DIGEST.match(str(asset.get("digest") or ""))
        if best is None or parse_version(version) > parse_version(best["version"]):
            best = {"version": version, "url": url, "sha256": digest and digest.group(1)}
    if best is None:
        raise UpdateError("GitHub lists no stable MAKO Renderer release")
    return best


# ---------- what's installed ----------

def library_versions(path):
    """Every version-looking string MAKO compiled into its layer library."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    return [m.group(1).decode() for m in EMBEDDED.finditer(data)]


def find_layer(dirs=LAYER_DIRS):
    """(layer json, library path) of the MAKO layer Vulkan finds first, or None."""
    for d in dirs:
        manifest = Path(d) / LAYER_JSON
        try:
            lib = json.loads(manifest.read_text())["layer"]["library_path"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        lib_path = Path(lib) if os.path.isabs(lib) else (manifest.parent / lib)
        return manifest, Path(os.path.normpath(lib_path))
    return None


def state(prefix=PREFIX):
    """active-renderer.json ({owner, version}) or {}."""
    try:
        data = json.loads((Path(prefix) / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def installed(dirs=LAYER_DIRS, prefix=PREFIX):
    """{version, path, local, owner}: MAKO's version (from its library, else from
    active-renderer.json; None if unknown), the library's path, whether it lives under
    ~/.local, and who installed it ("standalone", "decky" or None). None if MAKO isn't
    installed."""
    found = find_layer(dirs)
    if not found:
        return None
    _, lib = found
    try:
        local = Path(lib).resolve().is_relative_to(Path(prefix).resolve())
    except OSError:
        local = False
    st = state(prefix) if local else {}
    recorded = st.get("version") if isinstance(st.get("version"), str) else None
    found_versions = library_versions(lib)
    if recorded and recorded in found_versions:
        version = recorded
    elif len(found_versions) == 1:
        version = found_versions[0]
    else:
        version = recorded
    owner = st.get("owner") if isinstance(st.get("owner"), str) else None
    return {"version": version, "path": str(lib), "local": local, "owner": owner}


# ---------- installing ----------

def _tail(text, lines=4):
    return " / ".join([ln.strip() for ln in (text or "").splitlines() if ln.strip()][-lines:])


def install(archive, expected, extract, prefix=PREFIX):
    """Install a Renderer archive (a file) with its own installer into `prefix`, unpacked
    with extract(archive, dest, only_top=…), which raises if the archive isn't valid.
    Nothing runs unless it holds a Renderer that says it's `expected`; afterwards the
    installed Renderer must report `expected`. Returns the installer's last words."""
    prefix = Path(prefix)
    work_parent = prefix / "share"
    work_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".pescao-mako-update-", dir=work_parent))
    try:
        work = tmp / "x"
        names = set(extract(archive, work, only_top=TOP_DIRS + TOP_FILES))
        for needed in (INSTALLER, VERSION_FILE, MANIFEST):
            if needed not in names or not (work / needed).is_file():
                raise UpdateError(f"this archive has no {needed!r}: not a MAKO Renderer archive")
        version = (work / VERSION_FILE).read_text(errors="replace").strip()
        if version != expected:
            raise UpdateError(f"the archive is MAKO Renderer {version!r}, not {expected!r}")
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env.update(MAKO_INSTALLER_ASSUME_YES="1", MAKO_INSTALLER_NO_LAUNCH="1", MAKO_INSTALL_PREFIX=str(prefix))
        try:
            run = subprocess.run([str(work / INSTALLER), "--install"], cwd=work, env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise UpdateError(f"MAKO's installer didn't finish in {INSTALL_TIMEOUT}s") from None
        except OSError as e:
            raise UpdateError(f"can't run MAKO's installer ({e})") from None
        if run.returncode != 0:
            raise UpdateError(f"MAKO's installer failed: {_tail(run.stderr) or _tail(run.stdout) or run.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    found = installed([prefix / "share/vulkan/implicit_layer.d"], prefix)
    if not found or found["version"] != expected:
        raise UpdateError(f"installed, but MAKO reports {found and found['version']!r} instead of {expected!r}")
    return _tail(run.stderr) or _tail(run.stdout)
