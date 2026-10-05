"""Task-private Git and trusted exact-head import for the app-server adapter."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def _env():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def git(directory, *args, data=None):
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-c", "core.hooksPath=" + os.devnull,
         "-C", str(directory), *args], input=data, env=_env())


def git_dir(gitdir, *args, data=None):
    return subprocess.check_output(
        ["git", "--no-replace-objects", "--git-dir=" + str(gitdir),
         "-c", "core.hooksPath=" + os.devnull, *args], input=data, env=_env())


class PrivateGit:
    """A per-task Git store. Only validated reachable objects cross into canonical Git."""

    def __init__(self, worktree, root, branch):
        self.worktree = Path(worktree).resolve()
        self.root = Path(root).resolve()
        self.pointer = self.worktree / ".git"
        if not self.pointer.is_file() or self.pointer.is_symlink():
            raise ValueError("app-server ship requires a linked worktree")
        self.original = self.pointer.read_bytes()
        if b"\x00" in self.original or not self.original.startswith(b"gitdir: "):
            raise ValueError("invalid linked-worktree Git pointer")
        pointer = self.original.decode("utf-8").strip()
        if not pointer.startswith("gitdir: ") or "\n" in pointer:
            raise ValueError("invalid linked-worktree Git pointer")
        recorded_gitdir = Path(pointer[8:])
        if not recorded_gitdir.is_absolute():
            recorded_gitdir = self.worktree / recorded_gitdir
        self.canonical = Path(git(self.worktree, "rev-parse",
                                  "--absolute-git-dir").decode().strip()).resolve()
        if recorded_gitdir.resolve() != self.canonical:
            raise ValueError("linked-worktree Git pointer changed during setup")
        if os.stat(self.root.parent).st_dev in {
                os.stat(self.worktree).st_dev, os.stat(self.canonical).st_dev}:
            raise ValueError("task-private Git must be on a separate filesystem")
        self.base = git(self.worktree, "rev-parse", "HEAD").decode().strip()
        git(self.worktree, "check-ref-format", "--branch", branch)
        self.ref = "refs/heads/" + branch
        prior = subprocess.run(["git", "-C", str(self.worktree), "rev-parse",
                                "--verify", self.ref],
                               capture_output=True, text=True, env=_env())
        self.old = prior.stdout.strip() if prior.returncode == 0 else "0" * len(self.base)
        if self.old != "0" * len(self.base) and self.old != self.base:
            raise ValueError("task branch differs from worktree base")
        if git(self.worktree, "status", "--porcelain").strip():
            raise ValueError("private Git setup requires a clean task worktree")
        self.root.mkdir(mode=0o700)
        try:
            git(self.root, "init", "--bare", "--quiet")
            pack = git(self.worktree, "pack-objects", "--stdout", "--revs",
                       data=(self.base + "\n").encode())
            git(self.root, "index-pack", "--stdin", data=pack)
            git(self.root, "update-ref", self.ref, self.base)
            git(self.root, "symbolic-ref", "HEAD", self.ref)
            git(self.root, "config", "core.bare", "false")
            git(self.root, "config", "core.worktree", str(self.worktree))
            git(self.root, "config", "core.hooksPath", os.devnull)
            git(self.root, "config", "commit.gpgsign", "false")
            for key in ("user.name", "user.email"):
                value = subprocess.run(["git", "-C", str(self.worktree),
                    "config", "--get", key], capture_output=True, env=_env())
                if value.returncode == 0:
                    git(self.root, "config", key, value.stdout.decode().strip())
            config = self.root / "config"
            self.config_hash = hashlib.sha256(config.read_bytes()).digest()
            self.pointer_active = False
            self._write_pointer(("gitdir: " + str(self.root) + "\n").encode())
            self.pointer_active = True
            git(self.worktree, "read-tree", self.base)
        except BaseException:
            self.restore()
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    def _write_pointer(self, value):
        temporary = self.worktree / (".git-pointer-" + str(os.getpid()))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(value)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.pointer)

    def restore(self):
        if not getattr(self, "pointer_active", False):
            return
        self._write_pointer(self.original)
        self.pointer_active = False

    def _private_git(self, *args):
        return subprocess.check_output(
            ["git", "--no-replace-objects", "--git-dir=" + str(self.root),
             "--work-tree=" + str(self.worktree), "-c", "core.bare=false",
             "-c", "core.worktree=" + str(self.worktree),
             "-c", "core.hooksPath=" + os.devnull, *args], env=_env())

    def _verified_head(self):
        config = self.root / "config"
        if config.is_symlink() or not config.is_file() or hashlib.sha256(config.read_bytes()).digest() != self.config_hash:
            raise ValueError("task-private Git configuration changed")
        packed_refs = self.root / "packed-refs"
        shallow = self.root / "shallow"
        alternates = self.root / "objects/info/alternates"
        if packed_refs.is_symlink() or packed_refs.exists():
            raise ValueError("task-private packed refs are not accepted")
        if shallow.is_symlink() or shallow.exists() or alternates.is_symlink() or alternates.exists():
            raise ValueError("task-private Git has external object references")
        ref_parts = Path(self.ref).parts
        ref_parent = self.root
        for part in ref_parts[:-1]:
            ref_parent = ref_parent / part
            if ref_parent.is_symlink() or not ref_parent.is_dir():
                raise ValueError("task-private refs directory is unsafe")
        head_file = self.root / "HEAD"
        ref_file = ref_parent / ref_parts[-1]
        for path in (head_file, ref_file):
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("task-private task ref is missing or unsafe")
        if head_file.read_text().strip() != "ref: " + self.ref:
            raise ValueError("task-private HEAD is not the assigned task branch")
        oid = ref_file.read_text().strip()
        if not re.fullmatch("[0-9a-f]{" + str(len(self.base)) + "}", oid) or oid == self.base:
            raise ValueError("task-private HEAD is invalid or has no ship commit")
        refs = self._private_git("for-each-ref", "--format=%(refname)").decode().splitlines()
        if refs != [self.ref]:
            raise ValueError("task-private Git contains unrelated refs")
        if self._private_git("symbolic-ref", "HEAD").decode().strip() != self.ref:
            raise ValueError("task-private Git branch changed")
        if self._private_git("rev-parse", "HEAD").decode().strip() != oid:
            raise ValueError("task-private Git HEAD does not match its task ref")
        for args in (("diff", "--quiet", oid, "--"),
                     ("diff", "--cached", "--quiet", oid, "--")):
            result = subprocess.run(
                ["git", "--no-replace-objects", "--git-dir=" + str(self.root),
                 "--work-tree=" + str(self.worktree), "-c", "core.bare=false",
                 "-c", "core.worktree=" + str(self.worktree),
                 "-c", "core.hooksPath=" + os.devnull, *args],
                capture_output=True, env=_env())
            if result.returncode != 0:
                raise ValueError("task-private worktree has uncommitted tracked changes")
        return oid

    def publish(self, snapshot_parent):
        # Validate without loading any worker-controlled canonical Git config.
        oid = self._verified_head()
        with tempfile.TemporaryDirectory(prefix="appserver-import-", dir=snapshot_parent) as tmp:
            snapshot = Path(tmp)
            git(snapshot, "init", "--bare", "--quiet")
            source = self.root / "objects"
            for directory, dirs, files in os.walk(source, followlinks=False):
                directory = Path(directory)
                if directory.is_symlink() or any((directory / name).is_symlink() for name in dirs):
                    raise ValueError("private object directory contains symlink")
                relative = directory.relative_to(source)
                target = snapshot / "objects" / relative
                target.mkdir(exist_ok=True)
                for name in files:
                    path = directory / name
                    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                        raise ValueError("private object is not an isolated regular file")
                    if relative == Path("info"):
                        continue
                    shutil.copyfile(path, target / name)
            git(snapshot, "cat-file", "-e", oid + "^{commit}")
            git(snapshot, "merge-base", "--is-ancestor", self.base, oid)
            git(snapshot, "fsck", "--strict", "--no-reflogs", oid)
            pack = git(snapshot, "pack-objects", "--stdout", "--revs",
                       data=(oid + "\n").encode())
            # This process is outside the worker sandbox. CAS refuses a branch
            # moved by another supervisor since task setup.
            git_dir(self.canonical, "index-pack", "--stdin", data=pack)
            git_dir(self.canonical, "update-ref", self.ref, oid, self.old)
            self.restore()
            git(self.worktree, "symbolic-ref", "HEAD", self.ref)
            git(self.worktree, "read-tree", oid)
        return oid
