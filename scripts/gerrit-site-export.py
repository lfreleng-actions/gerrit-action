# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""\
Usage: python3 scripts/gerrit-site-export.py <seed|final> --out <dir> [options]

Copy a Gerrit site into <out>/site and <out>/git, described by manifest.json.
Standard library only, Python 3.8 or newer; reads the site, writes under --out.
  seed                Incremental rsync; safe while Gerrit runs, and to re-run.
  final               Last rsync with --checksum, then ref counts,
                      refs/meta/config heads and SSH host key fingerprints.
                      Refuses while Gerrit serves the site (logs/gerrit.pid,
                      /proc) or /proc cannot tell, unless --i-stopped-writes.
  --site <dir>        Gerrit site (default: $GERRIT_SITE).
  --git <dir>         Repositories (default: gerrit.basePath of the site).
  --out <dir>         Export directory; may not overlap the site or repositories.
  --i-stopped-writes  final: writes are stopped another way; skip the check.
  --pack              final: pack 0600 site.tar.zst and git.tar.zst; needs zstd.
  --encrypt <spec>    final: encrypt the archives with age:<recipient> or
                      gpg:<key-id>, deleting the plaintext. Implies --pack.
  --upload <url>      final: 'aws s3 cp --sse aws:kms' the archives and
                      manifest.json to s3://bucket/prefix. Implies --pack.
  --include-cache     Copy <site>/cache too; the import rebuilds caches.
  --dry-run           Print the commands without writing anything.
  -h, --help          Show this help and exit.

Left out of <out>/site whatever their type: cache, index, logs, tmp, bin,
.gerritcodereview/tmp, .chart, and the repositories, as configured and resolved.
Symlinks inside the repository store are refused, never followed; so are symlinked
outputs. Runs remove the old manifest (uploaded too) and archives first and write the
manifest last, so a manifest.json marks a complete export. Any failure exits non-zero.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from subprocess import PIPE
from types import SimpleNamespace
from typing import Any, NoReturn

PROG = "gerrit-site-export.py"
EXCLUDED = ["cache", "index", "logs", "tmp", "bin", ".gerritcodereview/tmp", ".chart"]
PROC_ENV = "GERRIT_SITE_EXPORT_PROC"  # Reserved for tests: stands in for /proc.
VALUES = ("--site", "--git", "--out", "--encrypt", "--upload")
FLAGS = ("--i-stopped-writes", "--pack", "--include-cache", "--dry-run")
HINT = "stop writes and pass --i-stopped-writes"
ELSEWHERE = "another filesystem namespace, so its site cannot be compared"
NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
ZSTD = ["zstd", "-q", "-T0", "-c"]
ENCRYPT = {"age": ["-r"], "gpg": ["--batch", "--yes", "--encrypt", "--recipient"]}
S3_COPY = ["aws", "s3", "cp", "--sse", "aws:kms"]
PARTS = ("site", "git")
ARCHIVES = [f"{p}.tar.zst{s}" for p in PARTS for s in ("", ".age", ".gpg")]
FOR_EACH_REF = ["for-each-ref", "--format=%(refname) %(objectname) %(*objectname)"]


def die(message: str | OSError) -> NoReturn:
    sys.exit(f"{PROG}: error: {' '.join(str(message).splitlines())}")


def say(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def show(command: list[str]) -> None:
    print("+ " + " ".join(shlex.quote(part) for part in command), flush=True)


def run(command: list[str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=capture, text=True, check=False)
    except FileNotFoundError:
        die(f"required tool not found: {command[0]}")


def step(opts: Any, command: list[str], what: str, tolerate: int = 0) -> int:
    if opts.dry_run:
        show(command)
        return 0
    if (status := run(command).returncode) not in (0, tolerate):
        die(f"{what} failed with exit status {status}")
    return status


def parse_args(argv: list[str]) -> Any:
    opts = SimpleNamespace(mode="", **{o[2:]: "" for o in VALUES})
    opts.__dict__.update({o[2:].replace("-", "_"): False for o in FLAGS})
    opts.site = os.environ.get("GERRIT_SITE", "")
    args = list(argv)
    while args:
        arg = args.pop(0)
        name, equals, value = arg.partition("=")
        if arg in ("-h", "--help"):
            sys.stdout.write(__doc__ or "")
            sys.exit(0)
        elif arg in ("seed", "final"):
            if opts.mode:
                die(f"mode given twice: {opts.mode} and {arg}")
            opts.mode = arg
        elif arg in FLAGS:
            setattr(opts, arg[2:].replace("-", "_"), True)
        elif name in VALUES:
            if not equals and args and not args[0].startswith("--"):
                value = args.pop(0)
            if not value:
                die(f"{name} needs a value")
            setattr(opts, name[2:], value)
        else:
            die(f"unknown argument: {arg} (see --help)")
    kind, _, who = opts.encrypt.partition(":")
    bad_key = opts.encrypt and (kind not in ENCRYPT or not who)
    bad_s3 = opts.upload and (opts.upload[:5] != "s3://" or opts.upload == "s3://")
    opts.pack = opts.pack or bool(opts.encrypt or opts.upload)
    seed_pack = opts.pack and opts.mode == "seed"
    for failed, message in (
        (not opts.mode, "give a mode, seed or final (see --help)"),
        (not opts.site, "no site: pass --site or set GERRIT_SITE"),
        (not opts.out, "no export directory: pass --out"),
        (bad_key, "--encrypt takes age:<recipient> or gpg:<key-id>"),
        (bad_s3, "--upload takes s3://bucket/prefix"),
        (seed_pack, "--pack, --encrypt and --upload need final mode"),
    ):
        if failed:
            die(message)
    opts.excluded = [e for e in EXCLUDED if e != "cache" or not opts.include_cache]
    return opts


def is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def readable_dir(path: str, what: str) -> str:
    if not (os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)):
        die(f"cannot read {what} directory: {path}")
    if (real := os.path.realpath(path)) != os.path.realpath(os.path.abspath(path)):
        die(f"{what} path {path} has '..' after a symlink; pass {real}")
    return real


def host_keys(etc: Path) -> list[Path]:
    keys = sorted(etc.glob("ssh_host_*"))
    return [k for k in keys if k.suffix != ".pub" and stat.S_ISREG(k.lstat().st_mode)]


def require_tools(opts: Any, site: str) -> None:
    optional = {"ssh-keygen": host_keys(Path(site, "etc")), "zstd": opts.pack}
    optional.update({opts.encrypt.partition(":")[0]: opts.encrypt, "aws": opts.upload})
    needed = ["rsync", "git", *(tool for tool, use in optional.items() if use)]
    if missing := [tool for tool in needed if not shutil.which(tool)]:
        die(f"required tool{'s' * (len(missing) > 1)} not found: {', '.join(missing)}")


def base_path(site: str) -> str:
    config = os.path.join(site, "etc", "gerrit.config")
    if not os.access(config, os.R_OK):
        die(f"cannot read {config}; pass --git")
    result = run(["git", "config", "-f", config, "--get", "gerrit.basePath"], True)
    if result.returncode > 1:
        die(f"cannot read gerrit.basePath from {config}: {result.stderr}")
    if not result.stdout.strip():
        die(f"gerrit.basePath is not set in {config}; pass --git")
    return result.stdout.strip()


def refuse_unsafe(path: Path, regular: bool) -> None:
    # rsync --delete follows a symlinked root; open and rename follow links.
    mode = os.lstat(path).st_mode if os.path.lexists(path) else stat.S_IFREG
    if stat.S_ISLNK(mode):
        die(f"refusing to write through symlink: {path}")
    if regular and not stat.S_ISREG(mode):
        die(f"not a regular file: {path}")


def served_site(argv: list[str]) -> str | None:
    """The -d/--site-path of a '*.war ... daemon' command line, "." if none."""
    wars = [i for i, arg in enumerate(argv) if arg.endswith(".war")]
    if not wars or "daemon" not in argv[wars[0] + 1 :]:
        return None
    site, args = ".", iter(argv[argv.index("daemon", wars[0] + 1) + 1 :])
    for arg in args:
        if arg in ("-d", "--site-path"):
            site = next(args, "")
        elif arg.startswith("--site-path="):
            site = arg.partition("=")[2]
        elif arg.startswith("-d") and not arg.startswith("--"):
            site = arg[2:]
    return site


def fs_view(proc: str, pid: str) -> tuple[int, int, str] | None:
    try:  # The root and mount namespace that pid resolves paths, such as -d, in.
        root = os.stat(f"{proc}/{pid}/root")
        return root.st_dev, root.st_ino, os.readlink(f"{proc}/{pid}/ns/mnt")
    except OSError:
        return None


def refuse_running(site: str) -> None:
    """Die while a Gerrit serves site; reads /proc, as ps output truncates."""
    proc = os.environ.get(PROC_ENV) or "/proc"
    running = f"Gerrit is running on {site} (pid %s); stop it, or {HINT}"
    try:
        pids = sorted((e for e in os.listdir(proc) if e.isdigit()), key=int)
    except OSError:
        die(f"cannot list processes in {proc} to check for Gerrit; {HINT}")
    if os.path.lexists(pidfile := Path(site, "logs", "gerrit.pid")):
        pid = "".join(c for c in pidfile.read_text() if c.isdigit())
        if pid and os.path.isdir(os.path.join(proc, pid)):
            die(running % pid)
    if "1" not in pids:
        die(f"{proc} hides pid 1 (hidepid?), so it may hide Gerrit too; {HINT}")
    doubt, site_stat, own = "", os.stat(site), fs_view(proc, "self")
    for pid in (p for p in pids if p != str(os.getpid())):
        base = os.path.join(proc, pid)
        vague = f"cannot tell which site Gerrit (pid {pid}) serves"
        try:
            with open(os.path.join(base, "cmdline"), "rb") as handle:
                raw = handle.read().rstrip(b"\0")
            served = served_site([os.fsdecode(a) for a in raw.split(b"\0")])
            if served is not None and (own is None or fs_view(proc, pid) != own):
                vague, served = f"Gerrit (pid {pid}) runs in {ELSEWHERE}", ""
            if served and not os.path.isabs(served):
                cwd = os.readlink(os.path.join(base, "cwd"))
                served = os.path.join(base, cwd, served)
        except OSError:  # Gone since the listing, or there but not inspectable.
            doubt = doubt or (vague if os.path.isdir(base) else "")
            continue
        try:  # stat, unlike realpath, fails where it cannot resolve a path.
            if served and os.path.samestat(os.stat(served), site_stat):
                die(running % pid)
        except (FileNotFoundError, NotADirectoryError):
            pass  # Nothing there, so not this site.
        except OSError:
            served = ""
        if served == "":
            doubt = doubt or vague
    if doubt:
        die(f"{doubt}; stop it, or {HINT}")


def copy(opts: Any, site: str, git_dir: str, git_given: str, out: Path) -> None:
    # Any type matches; a linked basePath goes as both, or as its first linked ancestor.
    names, given = list(opts.excluded), os.path.abspath(opts.site)
    for path, base in ((git_given, site), (git_given, given), (git_dir, site)):
        if path != base and is_within(path, base):
            p = Path(path[len(base.rstrip("/")) + 1 :]).parts
            up = [i for i in range(1, len(p)) if Path(base, *p[:i]).is_symlink()]
            names.append("/".join(p[: (up or [len(p)])[0]]))
    escape = "".maketrans({c: "\\" + c for c in "*?[\\"})
    filters = [f"--exclude=/{n.translate(escape)}" for n in dict.fromkeys(names)]
    rsync = ["rsync", "-a", "--delete", *(["--checksum"] * (opts.mode == "final"))]
    for source, part in ((site, "site"), (git_dir, "git")):
        extra = ["--delete-excluded", *filters] if part == "site" else []
        command = [*rsync, *extra, f"{source}/", f"{out}/{part}/"]
        # 24: files vanished mid-copy, expected while Gerrit writes.
        if step(opts, command, "rsync", 24 if opts.mode == "seed" else 0) == 24:
            say("warning: files vanished during the copy; re-run seed or final")


def pack(opts: Any, out: Path) -> list[Path]:
    # secure.config and host keys inside: all 0600. A failure ends the run.
    archives, saved, failure = [], os.umask(0o077), ""
    for part in PARTS:
        archive = out / f"{part}.tar.zst"
        if opts.dry_run:
            show(["tar", f"{part}/", "|", *ZSTD, ">", str(archive)])
        else:
            with os.fdopen(os.open(archive, NEW_FILE, 0o600), "wb") as dest:
                zstd = subprocess.Popen(ZSTD, stdin=PIPE, stdout=dest, bufsize=0)
                try:
                    with tarfile.open(fileobj=zstd.stdin, mode="w|") as tar:
                        tar.add(str(out / part), arcname=part)
                except (OSError, tarfile.TarError) as exc:
                    failure = str(exc)
                zstd.communicate()
            # zstd failing breaks the pipe, so its status is reported first.
            if status := zstd.returncode:
                die(f"zstd failed with exit status {status} packing {archive.name}")
            if failure:
                die(f"cannot pack {part}/: {failure}")
        step(opts, ["zstd", "-tq", str(archive)], f"integrity check of {archive.name}")
        if opts.encrypt:
            kind, _, who = opts.encrypt.partition(":")
            sealed = archive.with_name(f"{archive.name}.{kind}")
            output = "-o" if kind == "age" else "--output"
            command = [kind, *ENCRYPT[kind], who, output, str(sealed), str(archive)]
            step(opts, command, f"{kind} encryption of {archive.name}")
            if opts.dry_run:
                show(["rm", "-f", str(archive)])
            else:
                refuse_unsafe(sealed, regular=True)
                os.chmod(sealed, 0o600)
                archive.unlink()
            archive = sealed
        archives.append(archive)
    os.umask(saved)
    return archives


def tree_paths(root: Path | str, err: Any = die) -> Iterator[str]:
    return (os.path.join(t, n) for t, d, f in os.walk(root, onerror=err) for n in d + f)


def gerrit_version(site: str) -> str | None:
    war = Path(site, "bin", "gerrit.war")
    try:
        with zipfile.ZipFile(war) as archive:
            text = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    except (KeyError, zipfile.BadZipFile) as exc:
        die(f"cannot read the Gerrit version from {war}: {exc}")
    pairs = (line.partition(":") for line in text.splitlines())
    found = [v.strip() for k, _, v in pairs if k.strip() == "Implementation-Version"]
    return found[0] if found and found[0] else None


def build_manifest(opts: Any, site: str, git_dir: str, out: Path) -> dict[str, Any]:
    repos = {}
    for top, dirs, _files in os.walk(out / "git", onerror=die):
        for name in [d for d in dirs if d.endswith(".git")]:
            dirs.remove(name)
            rel = Path(top, name).relative_to(out / "git").as_posix()
            repo = f"--git-dir={Path(top, name)}"
            result = run(["git", "-c", "safe.directory=*", repo, *FOR_EACH_REF], True)
            if result.returncode:
                die(f"cannot read refs of repository: {rel}")
            refs = [line.split(" ") for line in result.stdout.splitlines()]
            # A tag is peeled to the commit it names.
            meta = [r[2] or r[1] for r in refs if r[0] == "refs/meta/config"]
            repos[rel] = {"refs": len(refs), "meta_config": next(iter(meta), None)}
    keys = {}
    for key in host_keys(out / "site" / "etc"):
        result = run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], True)
        fields = result.stdout.split()
        if result.returncode or len(fields) < 2 or not fields[1].startswith("SHA256:"):
            die(f"cannot fingerprint SSH host key: {key.name}")
        keys[key.name] = fields[1]
    size = {n: sum(os.lstat(p).st_size for p in tree_paths(out / n)) for n in PARTS}
    git = {"path": git_dir, "bytes": size["git"]}
    return {
        "schema": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": opts.mode,
        "gerrit_version": gerrit_version(site),
        "site": {"path": site, "bytes": size["site"]},
        "git": {**git, "repositories": dict(sorted(repos.items()))},
        "ssh_host_keys": keys,
        "excluded": opts.excluded,
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        die(f"cannot compute the sha256 of {path.name}: {exc.strerror}")
    return digest.hexdigest()


def main(argv: list[str]) -> None:
    opts = parse_args(argv)
    site = readable_dir(opts.site, "site")
    require_tools(opts, site)
    git_raw = opts.git or os.path.join(site, base_path(site))
    git_dir, git_given = readable_dir(git_raw, "repository"), os.path.abspath(git_raw)
    out = Path(opts.out).resolve()  # Before mkdir: no '..' or link dodges the check.
    for source in (site, git_dir):
        if is_within(str(out), source) or is_within(source, str(out)):
            die(f"export directory {out} overlaps {source}")
    # Walk errors pass: seed sees entries vanish, and rsync fails on unreadable ones.
    if links := [p for p in tree_paths(git_dir, err=None) if os.path.islink(p)]:
        die(f"symlinks in the repository store are not followed: {', '.join(links)}")
    outputs = [out / n for n in ("manifest.json", "manifest.json.tmp", *ARCHIVES)]
    manifest, temp = outputs[:2]
    for path in (out / "site", out / "git", *outputs):
        refuse_unsafe(path, regular=path in outputs)
    if opts.mode == "final" and not opts.i_stopped_writes:
        refuse_running(site)
    dry = "dry run, nothing is written: " if opts.dry_run else ""
    say(f"{dry}{opts.mode} export of {site} into {out}")
    # Old manifest first, then archives: a failed run must not look complete.
    stale = [path for path in outputs if os.path.lexists(path)]
    if opts.dry_run:
        if stale:
            show(["rm", "-f", *map(str, stale)])
        show(["mkdir", "-p", f"{out}/site", f"{out}/git"])
    else:
        for path in stale:
            path.unlink()
        (out / "site").mkdir(parents=True, exist_ok=True)
        (out / "git").mkdir(exist_ok=True)
    try:
        copy(opts, site, git_dir, git_given, out)
        data = {} if opts.dry_run else build_manifest(opts, site, git_dir, out)
        archives = pack(opts, out) if opts.pack else []
        if opts.dry_run:
            show(["write", str(manifest)])
        else:
            data["archives"] = {a.name: sha256_of(a) for a in archives}
            with os.fdopen(os.open(temp, NEW_FILE, 0o644), "w") as handle:
                handle.write(json.dumps(data, indent=2) + "\n")
        prefix = opts.upload.rstrip("/")
        if opts.upload:  # Old manifest first: it names archives about to change.
            remove = ["aws", "s3", "rm", f"{prefix}/manifest.json"]
            step(opts, remove, "removal of the uploaded manifest.json")
        uploads = [(a, a.name) for a in archives] + [(temp, "manifest.json")]
        for path, name in uploads if opts.upload else []:
            step(opts, [*S3_COPY, str(path), f"{prefix}/{name}"], f"upload of {name}")
        if not opts.dry_run:
            os.replace(temp, manifest)
    except BaseException:  # Archives without a manifest would look usable.
        for path in [] if opts.dry_run else filter(os.path.isfile, outputs):
            path.unlink()
        raise
    say(f"done: {manifest}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except OSError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        sys.exit(130)
