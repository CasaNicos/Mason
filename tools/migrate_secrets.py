#!/usr/bin/env python3
"""
tools/migrate_secrets.py — copy Mason's secrets into the private secrets file (phase 2, B1a).

NICO RUNS THIS (RULINGS #17 step 4, #20). Agents only write and test it, on synthetic values.

What it does
  * Reads the CURRENT sources, read-only:
      - mason.config.json   (apis.*.key, apis.finviz.key, apis.sec.user_agent,
                             paper_trading.api_key / api_secret, robinhood.token_file)
      - keys.txt            ("Label: value" lines: FMP, AlphaVantage, Anthropic, TwilioASID,
                             TwilioAuth, TwilioNum)
    The config wins over keys.txt when both hold a key (the config is what Mason runs on today);
    a disagreement is reported by NAME only.
  * Writes ~/.config/mason/mason.secrets.json (or $MASON_SECRETS_FILE, or --out), creating the
    directory 0700 and the file 0600, atomically (temp file + fsync + rename), then re-reads it
    and verifies every value by SHA-256 comparison.
  * Never prints a value. Output is key names, sources, actions and counts only.
  * COPIES, never moves: the source files are never touched (RULINGS #22.3: the legacy fallback
    stays), and os.environ is never written.
  * Never prints any token of a keys.txt line (notes carry line numbers only) and never echoes
    command-line arguments or tracebacks.
  * RULINGS #27: checks run on the resolved real path (a symlinked parent directory is fine); a
    final-component symlink is refused; a real directory outside $HOME must already exist, be yours
    and not be group/world-writable (refused, never chmodded); a missing one is created 0700.
  * A re-run tightens an existing file to 0600 / directory to 0700.
  * C1 (RULINGS #33): every MISSING directory level is created 0700 (umask-proof); existing
    ancestors are never chmodded, and an existing ancestor inside $HOME (up to $HOME itself) that is
    group- or world-writable is refused (exit 1) with its path and the reason.
  * Env vars are NOT migrated (they are transient); the tool lists which ones are set, because
    they override the file at runtime.
  * Never invents a "robinhood" section (absent = legacy label scope, RULINGS #2). Sections and
    keys already in an existing secrets file are preserved. A key whose existing value differs
    from the source is KEPT unless --overwrite.
  * Idempotent: a second run with the same sources leaves the file byte-identical.

Usage
  python3 tools/migrate_secrets.py                 # dry run (default): names and counts only
  python3 tools/migrate_secrets.py --confirm       # write + verify
  options: --config PATH  --keys-txt PATH  --out PATH  --overwrite  --dry-run
Exit codes: 0 ok · 1 refused (malformed / symlinked target, bad args) · 2 verification failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import mason_secrets as S  # noqa: E402  (stdlib-only module; imports nothing else from Mason)

# keys.txt label (normalised: lowercase, alphanumerics only) -> canonical name
KEYS_TXT_LABELS = {
    "fmp": "FMP_API_KEY",
    "alphavantage": "ALPHA_VANTAGE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "twilioasid": "TWILIO_ACCOUNT_SID",
    "twilioaccountsid": "TWILIO_ACCOUNT_SID",
    "twilioauth": "TWILIO_AUTH_TOKEN",
    "twilioauthtoken": "TWILIO_AUTH_TOKEN",
    "twilionum": "TWILIO_PHONE_NUMBER",
    "twiliophonenumber": "TWILIO_PHONE_NUMBER",
}
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]{0,39}$")


def _h(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _say(msg: str) -> None:
    print(msg)


def read_config(p: Path) -> dict:
    """{canonical name: value} from the legacy config. Missing / broken file -> {}."""
    out = {}
    try:
        cfg = json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except Exception:
        _say(f"  ! config {p}: unreadable or not JSON; skipped")
        return {}
    if not isinstance(cfg, dict):
        return {}
    for name, spec in S.REGISTRY.items():
        node = cfg
        for part in spec["legacy"] or ():
            node = node.get(part) if isinstance(node, dict) else None
        if not spec["legacy"]:
            continue
        # Same rule as mason_secrets._legacy_value, so a key resolves to the same value with or
        # without the secrets file (RULINGS #22.3): strings verbatim (QA N5), numbers as str() (N1).
        if isinstance(node, str) and node.strip():
            out[name] = node
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            out[name] = str(node)
    return out


def read_keys_txt(p: Path) -> tuple:
    """({canonical name: value}, [notes]). Notes carry LINE NUMBERS and canonical names only —
    never any token of the line, because an unrecognised "label" may itself be (part of) a secret
    (QA B1a R1: a bare base64 value, a KEY_ID:SECRET pair, a token ending in '=')."""
    out, notes = {}, []
    try:
        raw = Path(p).read_bytes()
    except FileNotFoundError:
        return {}, []
    except OSError:
        return {}, [f"keys.txt {p}: unreadable; skipped"]
    try:
        txt = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {}, [f"keys.txt {p}: not UTF-8; skipped"]
    for i, line in enumerate(txt.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([^:=]*)[:=](.*)$", s)
        label = m.group(1).strip() if m else ""
        if not m or not _LABEL_RE.match(label):
            notes.append(f"keys.txt line {i}: unparseable (skipped)")
            continue
        val = m.group(2).strip()
        name = KEYS_TXT_LABELS.get(re.sub(r"[^a-z0-9]", "", label.lower()))
        if not name:
            notes.append(f"keys.txt line {i}: unrecognised label (skipped)")
            continue
        if not val:
            continue
        if name in out and _h(out[name].strip()) != _h(val):
            notes.append(f"keys.txt line {i}: second value for {name}; first one kept")
            continue
        out.setdefault(name, val)
    return out, notes


def _load_existing(p: Path):
    """(dict, None) for an existing usable file, ({}, None) when absent, (None, reason) to refuse."""
    try:
        lst = os.lstat(p)
    except FileNotFoundError:
        return {}, None
    if stat.S_ISLNK(lst.st_mode):
        return None, "target is a symlink; write to the real path with --out"
    if not stat.S_ISREG(lst.st_mode):
        return None, "target exists and is not a regular file"
    if lst.st_size > S.MAX_BYTES:
        return None, "existing file is larger than 1 MiB"
    try:
        obj = json.loads(Path(p).read_bytes().decode("utf-8-sig"))
    except Exception:
        return None, "existing secrets file is not valid JSON; fix or move it first"
    if obj is None:
        return {}, None
    if not isinstance(obj, dict):
        return None, "existing secrets file is not a JSON object"
    if obj.get("_schema", S.SCHEMA) != S.SCHEMA:
        return None, "existing secrets file has an unsupported _schema"
    return obj, None


def _missing_levels(d: Path) -> list:
    """The missing directory levels of ``d``, outermost first ([] when ``d`` exists)."""
    levels, cur = [], Path(os.path.abspath(d))
    while not cur.exists() and not cur.is_symlink():
        levels.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return list(reversed(levels))


def _ancestor_problem(d: Path):
    """C1 (REVIEW_B1a N6, RULINGS #33): refusal reason or None.

    Every EXISTING strict ancestor of the target directory ``d`` that sits inside $HOME — up to and
    including $HOME itself — must be neither group- nor world-writable: a writable ancestor lets
    another user swap the levels below it. Checked on real paths; existing ancestors are never
    chmodded. (``d`` itself, when it exists, is tightened to 0700 by _ensure_dir as before.)"""
    home = Path(os.path.realpath(os.path.expanduser("~")))
    missing = _missing_levels(d)
    start = (missing[0].parent if missing else Path(os.path.abspath(d)).parent)
    cur = Path(os.path.realpath(start))
    while True:
        try:
            cur.relative_to(home)
        except ValueError:
            return None                     # reached outside $HOME: not this check's business
        try:
            ast_ = os.stat(cur)
        except OSError:
            return f"cannot stat ancestor directory {cur}"
        if not stat.S_ISDIR(ast_.st_mode):
            return f"ancestor path {cur} is not a directory"
        m = stat.S_IMODE(ast_.st_mode)
        if m & 0o022:
            return (f"existing ancestor directory {cur} is group- or world-writable ({oct(m)}); "
                    f"fix it with: chmod go-w {cur}")
        if cur == home or cur.parent == cur:
            return None
        cur = cur.parent


def _ensure_dir(d: Path) -> None:
    missing = _missing_levels(d)
    if missing:
        for lvl in missing:            # C1: each created level is 0700 regardless of the umask
            os.mkdir(lvl, 0o700)
            os.chmod(lvl, 0o700)
            _say(f"  created directory {lvl} (0700)")
        return
    m = stat.S_IMODE(d.stat().st_mode)
    if m & 0o077:
        os.chmod(d, 0o700)
        _say(f"  tightened directory {d} from {oct(m)} to 0700")


def _atomic_write(p: Path, obj: dict) -> None:
    body = (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".mason.secrets.", suffix=".tmp", dir=str(p.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        tmp = None
        os.chmod(p, 0o600)
        try:
            dfd = os.open(str(p.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _QuietParser(argparse.ArgumentParser):
    """argparse echoes unrecognised arguments; a pasted value must never be echoed back."""

    def error(self, message):  # noqa: D401
        self.print_usage(sys.stderr)
        sys.stderr.write("migrate_secrets: invalid arguments (not echoed). See --help.\n")
        raise SystemExit(1)


def _path_guard(out: Path):
    """(refusal reason or None, real_parent_outside_home: bool). No values involved.

    RULINGS #27, mirrored from the loader: checks run on the fully resolved real path, so a
    symlinked parent directory is accepted. (A final-component symlink is refused separately by
    _load_existing.) When the real parent directory is outside $HOME it must already exist, be
    owned by this user and be neither group- nor world-writable when it exists (a group/world-writable
    or foreign directory is refused, never chmodded). A missing one is created 0700 at write time; an
    own, safe one wider than 0700 is tightened like the in-$HOME default (ruling 1.2)."""
    real_parent = Path(os.path.realpath(out.parent))
    home = Path(os.path.realpath(os.path.expanduser("~")))
    try:
        real_parent.relative_to(home)
        return None, False
    except ValueError:
        pass
    try:
        pst = os.stat(real_parent)
    except OSError:
        return None, True   # missing: created 0700 by this tool at write time (then re-checked)
    if not stat.S_ISDIR(pst.st_mode):
        return "the target directory is not a directory", True
    if pst.st_uid != os.getuid() or stat.S_IMODE(pst.st_mode) & 0o022:
        return ("the target directory is outside $HOME and is not owned by you or is group/world-"
                "writable (RULINGS #27)"), True
    return None, True


def main(argv=None) -> int:
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException as e:  # never let a traceback (with its locals / source lines) reach the terminal
        _say(f"error: {type(e).__name__}; nothing further done. No value was printed.")
        return 1


def _main(argv=None) -> int:
    ap = _QuietParser(description="Migrate Mason secrets into the private secrets file "
                                             "(dry run unless --confirm). Never prints values.")
    ap.add_argument("--config", default=str(_REPO / "mason.config.json"))
    ap.add_argument("--keys-txt", default=str(_REPO / "keys.txt"))
    ap.add_argument("--out", default=None, help="target file (default: $MASON_SECRETS_FILE or "
                                                "~/.config/mason/mason.secrets.json)")
    ap.add_argument("--confirm", action="store_true", help="actually write the file")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace existing values that differ from the source")
    a = ap.parse_args(argv)
    if a.confirm and a.dry_run:
        _say("refused: --confirm and --dry-run together")
        return 1

    out = Path(os.path.expanduser(a.out)) if a.out else S.path()
    mode = "CONFIRM" if a.confirm else "DRY RUN"
    _say(f"migrate_secrets — {mode}")
    _say(f"  target: {out}")

    cfg_vals = read_config(Path(a.config))
    kt_vals, notes = read_keys_txt(Path(a.keys_txt))
    _say(f"  sources: config {a.config} ({len(cfg_vals)} keys) · keys.txt {a.keys_txt} ({len(kt_vals)} keys)")
    for n in notes:
        _say(f"  ! {n}")

    why, outside = _path_guard(out)
    if not why:
        why = _ancestor_problem(out.parent)
    if why:
        _say(f"refused: {why}. Nothing written.")
        return 1
    existing, why = _load_existing(out)
    if existing is None:
        _say(f"refused: {why}. Nothing written.")
        return 1

    merged = dict(existing)
    merged["_schema"] = S.SCHEMA
    counts = {"write": 0, "unchanged": 0, "kept": 0, "absent": 0}
    _say("  key                      source      action")
    for name in S.REGISTRY:
        c, k = cfg_vals.get(name), kt_vals.get(name)
        src_val = c if c is not None else k
        src = "config" if c is not None else ("keys.txt" if k is not None else "-")
        if c is not None and k is not None and _h(c.strip()) != _h(k.strip()):
            _say(f"  ! conflict: {name} differs between config and keys.txt; config used")
        cur = existing.get(name)
        cur = cur if isinstance(cur, str) and cur.strip() else None
        if src_val is None:
            action = "present in file" if cur is not None else "absent"
            if cur is None:
                counts["absent"] += 1
            else:
                counts["unchanged"] += 1
        elif cur is None:
            merged[name] = src_val
            action = "write"
            counts["write"] += 1
        elif _h(cur) == _h(src_val):
            action = "unchanged"
            counts["unchanged"] += 1
        elif a.overwrite:
            merged[name] = src_val
            action = "overwrite (differs)"
            counts["write"] += 1
        else:
            action = "kept existing (differs from source; --overwrite to replace)"
            counts["kept"] += 1
        _say(f"  {name:<24} {src:<11} {action}")

    env_set = sorted(e for spec in S.REGISTRY.values() for e in spec["env"] if (os.environ.get(e) or "").strip())
    if env_set:
        _say(f"  note: env vars set now (not migrated; they override the file at runtime): {', '.join(env_set)}")

    verb = "would write" if not a.confirm else "write"
    _say(f"  summary: {verb} {counts['write']} · unchanged {counts['unchanged']} · "
         f"kept existing {counts['kept']} · absent {counts['absent']}")

    if not a.confirm:
        _say("  dry run: nothing written. Re-run with --confirm to write.")
        return 0
    if counts["write"] == 0:
        if not existing:
            _say("  nothing to write; no file created.")
            return 0
        _ensure_dir(out.parent)
        m = stat.S_IMODE(out.stat().st_mode)
        if m != 0o600:
            os.chmod(out, 0o600)
            _say(f"  tightened file mode from {oct(m)} to 0600")
        _say("  nothing to write; contents left as is.")
        return _verify(out, merged)
    _ensure_dir(out.parent)
    if outside:
        why, _ = _path_guard(out)      # re-check the real directory after creating / tightening it
        why = why or _ancestor_problem(out.parent)
        if why:
            _say(f"refused: {why}. Nothing written.")
            return 1
    _atomic_write(out, merged)
    _say(f"  wrote {out} (0600)")
    return _verify(out, merged)


def _verify(out: Path, expected: dict) -> int:
    try:
        got = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        _say("  VERIFY FAILED: file unreadable after write")
        return 2
    names = [n for n in S.REGISTRY if isinstance(expected.get(n), str) and expected[n].strip()]
    ok = sum(1 for n in names if isinstance(got.get(n), str) and _h(got[n]) == _h(expected[n]))
    m = stat.S_IMODE(out.stat().st_mode)
    _say(f"  verified {ok}/{len(names)} (sha-256) · mode {oct(m)}")
    if ok != len(names) or m != 0o600:
        _say("  VERIFY FAILED")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
