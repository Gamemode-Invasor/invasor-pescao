"""Pescao: MAKO Renderer profiles (frame generation, scaling) from Invasor's panel.

The source of truth is MAKO's own conf.toml (mako.config_path()), never Invasor's
settings: every call reads it, changes what was asked, and writes it back with
ctx.toml (all keys kept, verified, atomic, refused if mako-ui saved meanwhile).
A game uses a profile when its id (Steam or non-Steam shortcut) is in that profile's
active_in, and MAKO only runs in games launched through mako-launch.
"""
import re
import tempfile
from pathlib import Path

from . import launcher, mako, updates, validator

ctx = None


def setup(context):
    global ctx
    ctx = context


def _msg(e):
    return str(e.args[0]) if e.args else str(e)


def _read():
    path = mako.config_path()
    data, mtime = ctx.toml.load(path)
    if not data:
        data = mako.new_config()
    try:
        mako.check(data)
    except ValueError as e:
        raise ctx.Unavailable(str(e)) from None
    return path, data, mtime


_cli_missing_logged = False


def _cli():
    """mako-cli of the MAKO in use, or None (then Pescao's own checks are all there is)."""
    global _cli_missing_logged
    found = updates.find_layer()
    cli = validator.find_cli(found[1] if found else None)
    if cli is None and not _cli_missing_logged:
        _cli_missing_logged = True
        ctx.log.info("mako-cli not found: conf.toml changes are checked by Pescao only")
    return cli


def _mako_validate(tmp_path):
    """ctx.toml.save hook: MAKO's own validator judges the new file before it replaces
    conf.toml. A validator that can't run doesn't block the change."""
    cli = _cli()
    if cli is None:
        return
    try:
        validator.validate(tmp_path, cli)
    except validator.Rejected as e:
        raise ctx.InvalidArgument(f"MAKO rejected the change: {e}") from None
    except (OSError, TimeoutError) as e:
        ctx.log.warning("mako-cli couldn't validate (%s): saving with Pescao's own checks", e)


def mako_problem(path):
    """MAKO's complaint about conf.toml at `path`, or None if it accepts it (or can't be
    asked)."""
    cli = _cli()
    if cli is None or not path.exists():
        return None
    try:
        validator.validate(path, cli)
    except validator.Rejected as e:
        return str(e)
    except (OSError, TimeoutError):
        pass
    return None


def _change(fn):
    """Read, apply fn(data), write back (checked by MAKO itself when it can).
    ValueError/KeyError from fn -> clean 400."""
    path, data, mtime = _read()
    try:
        result = fn(data)
    except (ValueError, KeyError) as e:
        raise ctx.InvalidArgument(_msg(e)) from None
    ctx.toml.save(path, data, mtime, validate=_mako_validate)
    return result


def status():
    path = mako.config_path()
    out = {"installed": updates.installed() is not None, "path": str(path), "error": None, "mako_problem": None}
    try:
        _read()
    except (ctx.Unavailable, ctx.InvalidArgument) as e:
        out["error"] = str(e)
    else:
        out["mako_problem"] = mako_problem(path)
    return out


def profiles():
    """[{name, games: [{entry, name}], power_modes}]: every profile, the games that use it,
    and whether it has separate battery/AC options (mako.has_power_modes)."""
    _, data, _ = _read()
    out = []
    for p in data.get("profile", []):
        games = []
        for entry in mako.games(p):
            label = None
            if isinstance(entry, str) and entry.isascii() and entry.isdigit():
                label = (ctx.game.info(entry) or {}).get("name")
            games.append({"entry": entry, "name": label})
        out.append({"name": p.get("name"), "games": games, "power_modes": mako.has_power_modes(p)})
    return out


def profile_get(name):
    return ctx.forms["profile"].clean(_profile(name))


def profile_set(name, key, value):
    """Set one option; options that depend on it change too, as in mako-ui (mako.change)."""
    value = ctx.forms["profile"].coerce(key, value)
    _change(lambda data: mako.change(data, mako.find(data, name), key, value))
    return value


def profile_create(name, copy_from=None):
    _change(lambda data: mako.create(data, name, ctx.forms["profile"].defaults(), copy_from))
    return True


def profile_rename(old, new):
    _change(lambda data: mako.rename(data, old, new))
    return True


def profile_delete(name):
    _change(lambda data: (mako.find(data, name), mako.delete(data, name)))
    return True


def profile_unify(name):
    """Drop the profile's separate battery/AC options: MAKO then uses its general ones."""
    _change(lambda data: mako.unify(mako.find(data, name)))
    return True


def global_get():
    _, data, _ = _read()
    return ctx.forms["global"].clean(data.get("global", {}))


def global_set(key, value):
    value = ctx.forms["global"].coerce(key, value)
    _change(lambda data: mako.set_global(data, key, value))
    return value


def _entry(appid):
    try:
        return mako.game_entry(appid)
    except ValueError as e:
        raise ctx.InvalidArgument(str(e)) from None


def game_profile(appid):
    """{entry, profile, custom}: what identifies the game in active_in, its profile (or
    None), and whether that profile is the game's own (only this game uses it)."""
    entry = _entry(appid)
    _, data, _ = _read()
    name = mako.game_profile(data, entry)
    return {"entry": entry, "profile": name, "custom": name is not None and mako.is_custom(data, name, entry)}


def make_custom(appid, title=None):
    """Give the game its own profile (named after it, starting from its current values)."""
    entry = _entry(appid)
    title = title or (ctx.game.info(appid) or {}).get("name") or entry
    name = _change(lambda data: mako.make_custom(data, entry, title, ctx.forms["profile"].defaults()))
    ctx.log.info("game %s (%s) -> its own profile %r", appid, entry, name)
    return {"entry": entry, "profile": name, "custom": True}


def set_game_profile(appid, name=None):
    """Use profile `name` for the game from its next launch; None turns it off."""
    entry = _entry(appid)
    _change(lambda data: mako.assign(data, entry, name))
    ctx.log.info("game %s (%s) -> profile %r", appid, entry, name)
    return {"entry": entry, "profile": name}


def _profile(name):
    _, data, _ = _read()
    try:
        return mako.find(data, name)
    except KeyError as e:
        raise ctx.InvalidArgument(_msg(e)) from None


def _quote(value):
    """Shell quoting as mako-ui does it: always single quotes."""
    return "'" + str(value).replace("'", "'\\''") + "'"


# ---------- mako-launch's own settings ----------

# Shown as "HDR" (on/off), as mako-ui does; launcher.conf keeps it the other way round.
HDR = "enable_hdr"


def _launcher_load():
    try:
        return launcher.load()
    except launcher.Invalid as e:
        raise ctx.Unavailable(f"{launcher.path()} isn't valid ({e}): mako-launch ignores it; fix or remove it") from None


def launcher_get():
    settings = _launcher_load()
    settings[HDR] = not settings.pop("disable_hdr_exposure")
    return settings


def launcher_set(key, value):
    value = ctx.forms["launcher"].coerce(key, value)
    settings = _launcher_load()
    if key == HDR:
        settings["disable_hdr_exposure"] = not value
    else:
        settings[key] = value
    try:
        launcher.save(settings)
    except OSError as e:
        raise ctx.Unavailable(f"can't write {launcher.path()}: {e}") from None
    return value


# ---------- the game's launch option ----------

LAUNCH = "~/.local/bin/mako-launch %command%"
# What a line may already hold of an earlier launch option: MAKO_PROFILE=… (quoted or not) and mako-launch.
# Each with the space before it, so the rest of the line is left exactly as it was.
OLD_PROFILE = re.compile(r"""(?:^|\s+)MAKO_PROFILE=(?:'(?:[^']|'\\'')*'|"[^"]*"|\S*)(?=\s|$)""")
OLD_LAUNCH = re.compile(r"(?:^|\s+)\S*mako-launch(?=\s|$)")


def _rest(current):
    """The launch options without anything of Pescao's (MAKO_PROFILE, mako-launch)."""
    return OLD_LAUNCH.sub("", OLD_PROFILE.sub("", current or "")).strip()


def merged(current, name):
    """The game's launch options with this profile's launch option in: an earlier one is
    replaced, the user's own variables and wrappers stay before mako-launch (MAKO_PROFILE
    goes first, so any wrapper passes it on), and game arguments stay after %command%.
    A line that already runs mako-launch with this profile is left as it is."""
    current, profile = current or "", f"MAKO_PROFILE={_quote(name)}"
    if re.search(rf"(?:^|\s){re.escape(profile)}(?=\s|$)", current) and OLD_LAUNCH.search(current):
        return current
    rest = _rest(current)
    before, sep, after = rest.partition("%command%")
    if not sep:  # no %command%: what's there are the game's arguments
        before, after = "", (" " + rest if rest else "")
    return " ".join(p for p in (profile, before.strip(), LAUNCH) if p) + after.rstrip()


def without(current):
    """The game's launch options with Pescao's launch option taken out; nothing at all if
    only %command% is left."""
    rest = _rest(current)
    return "" if rest == "%command%" else rest


def _can_apply():
    try:
        return ctx.steam.supports("set_launch_options")
    except ctx.Unavailable:
        return False


def launch_option(name=None):
    """{line, found, can_apply}: the launch option for a profile, as mako-ui builds it,
    whether mako-launch is installed, and whether Steam lets Pescao set it on a game.
    MAKO_PROFILE is what makes MAKO use the profile: MAKO 4 matches active_in against
    executables and process names, not Steam ids."""
    line = LAUNCH
    if name is not None:
        _profile(name)
        line = f"MAKO_PROFILE={_quote(name)} {LAUNCH}"
    return {"line": line, "found": (updates.PREFIX / "bin" / "mako-launch").is_file(), "can_apply": _can_apply()}


def _set_launch(appid, change, refused, done):
    """Set the game's launch options to change(current), on the user's action: {line, verified}.
    A line that doesn't change leaves Steam alone."""
    if not _can_apply():
        raise ctx.Unavailable(f"Steam can't set launch options here: {refused}")
    current = ctx.steam.launch_options(appid)
    line = change(current)
    if line == current:
        return {"line": line, "verified": True}
    res = ctx.steam.set_launch_options(appid, line)
    ctx.log.info("game %s: %s", appid, done)
    return {"line": line, "verified": res["verified"]}


def apply_launch_option(appid, name):
    """Put the profile's launch option in the game's launch options, keeping the user's own
    (merged). Without Steam's call, Copy is the way."""
    _profile(name)
    return _set_launch(appid, lambda current: merged(current, name), "copy the line and paste it",
                       f"launch options set for profile {name!r}")


def remove_launch_option(appid):
    """Take Pescao's launch option out of the game's launch options (without()), leaving the
    user's own."""
    return _set_launch(appid, without, "remove mako-launch from them in Steam", "MAKO taken out of its launch options")


def update_status():
    """{installed, latest, state, …}: the MAKO Renderer version MAKO itself reports and the
    latest stable on GitHub (asked only now, never in the background).
    state: newer_available, up_to_date, ahead (installed is newer: nothing offered),
    decky (managed by MAKO Decky), system (outside ~/.local), not_installed."""
    return _status(_latest())


def _status(release):
    have = updates.installed()
    latest = release["version"]
    if have is None:
        state = "not_installed"
    elif not have["local"]:
        state = "system"
    elif have["owner"] == "decky":
        state = "decky"
    else:
        state = updates.compare(have["version"], latest)
    return {
        "installed": have and have["version"],
        "path": have and have["path"],
        "latest": latest,
        "state": state,
        "tested": "%d.%d" % updates.TESTED,
        "installed_compat": updates.compatibility(have and have["version"]),
        "latest_compat": updates.compatibility(latest),
    }


def _latest():
    """The newest stable Renderer release on GitHub: {version, url, sha256}."""
    try:
        return updates.latest_stable(ctx.http.get_json(updates.RELEASES, max_bytes=updates.MAX_RELEASES))
    except updates.UpdateError as e:
        raise ctx.Unavailable(str(e)) from None


def compat():
    """{installed, compat, tested}: whether the installed MAKO is the series Pescao was
    tested with (no network)."""
    have = updates.installed()
    version = have and have["version"]
    return {"installed": version, "compat": updates.compatibility(version), "tested": "%d.%d" % updates.TESTED}


def update_install():
    """Download the latest stable MAKO Renderer (checked against the sha256 GitHub publishes
    for it) and install it with MAKO's own installer, only when it's newer than what's
    installed (or MAKO isn't installed)."""
    release = _latest()
    status = _status(release)
    if status["state"] not in ("newer_available", "not_installed"):
        raise ctx.InvalidArgument({
            "up_to_date": "MAKO Renderer is already up to date",
            "ahead": "the installed MAKO Renderer is newer than the latest stable",
            "decky": "MAKO Renderer is managed by MAKO Decky: update it there",
            "system": "MAKO Renderer is installed by the system, not in ~/.local: update it there",
        }[status["state"]])
    if status["latest_compat"] == "unsupported":
        raise ctx.InvalidArgument(f"MAKO Renderer {status['latest']} is a new major version: update Pescao first")
    version = release["version"]
    checksum = ("sha256", release["sha256"]) if release["sha256"] else None  # without one, ctx.http asks
    work = updates.PREFIX / "share"
    try:
        work.mkdir(parents=True, exist_ok=True)
        with ctx.task(f"Updating MAKO Renderer to {version}") as t, \
                tempfile.TemporaryDirectory(prefix=".pescao-mako-download-", dir=work) as tmp:
            got = ctx.http.download(release["url"], Path(tmp) / updates.asset_name(version), checksum=checksum, task=t)
            t.update(None, "Installing")
            said = updates.install(got["path"], version, ctx.archive.extract)
    except updates.UpdateError as e:
        raise ctx.Unavailable(str(e)) from None
    ctx.log.info("installed MAKO Renderer %s into %s (%s)", version, updates.PREFIX, said)
    return {"installed": version}


METHODS = {
    "status": status,
    "profiles": profiles,
    "profile_get": profile_get,
    "profile_set": profile_set,
    "profile_create": profile_create,
    "profile_rename": profile_rename,
    "profile_delete": profile_delete,
    "profile_unify": profile_unify,
    "global_get": global_get,
    "global_set": global_set,
    "game_profile": game_profile,
    "set_game_profile": set_game_profile,
    "make_custom": make_custom,
    "launch_option": launch_option,
    "apply_launch_option": apply_launch_option,
    "remove_launch_option": remove_launch_option,
    "launcher_get": launcher_get,
    "launcher_set": launcher_set,
    "update_status": update_status,
    "compat": compat,
    "update_install": update_install,
}
