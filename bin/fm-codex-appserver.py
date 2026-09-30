#!/usr/bin/env python3
"""FirstMate's bounded Codex stdio adapter, not a general RPC transport.

run STATE TASK GEN BRIEF WORKTREE [MODEL [EFFORT]] hosts one thread and turn.
control STATE TASK GEN steer|answer|interrupt|exit [KEY] reads text from stdin.
The private Unix socket is transient transport only. Decisions remain status-log
records; callbacks exist only in this process. Worker roots exclude fleet state.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

BIN = Path(__file__).resolve().parent
LIMIT = 8192
TOKEN = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")
TOOL = {"type": "function", "name": "firstmate_report",
        "description": "Report to FirstMate. needs-decision waits for the supervisor's answer in this same turn. result is delivery evidence, not terminal success.",
        "inputSchema": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["progress", "needs-decision", "result"]},
            "message": {"type": "string", "minLength": 1, "maxLength": 500}},
            "required": ["type", "message"], "additionalProperties": False}}


def payload(value):
    if not isinstance(value, dict) or set(value) != {"type", "message"}:
        raise ValueError("expected only type and message")
    if value["type"] not in ("progress", "needs-decision", "result"):
        raise ValueError("unknown report type")
    text = value["message"]
    if not isinstance(text, str) or not 1 <= len(text.encode()) <= 500:
        raise ValueError("report size")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("report control character")
    return value["type"], text


def socket_path(state, task, gen):
    nonce = hashlib.sha256((task + ":" + gen).encode()).hexdigest()[:24]
    path = state / (".appserver-" + nonce + ".sock")
    if len(os.fsencode(path)) >= 104:
        raise ValueError("app-server socket path exceeds portable Unix socket limit")
    return path


class Adapter:
    def __init__(self, state, task, gen):
        self.state, self.task, self.gen = state.resolve(), task, gen
        if not TOKEN.fullmatch(task) or not TOKEN.fullmatch(gen):
            raise ValueError("invalid supervisor binding")
        self.thread = self.turn = None
        self.active = False
        self.result = None
        self.pending = None
        self.calls = set()
        self.requests = set()
        self.sequence = 0
        self.responses = {}
        self.events = queue.Queue(maxsize=1024)
        self.proc = None
        self.stopping = False
        self.terminal = None
        self.path = socket_path(self.state, task, gen)
        self.check()

    def check(self):
        if (self.state / (self.task + ".busy-gen")).read_text().strip() != self.gen:
            raise ValueError("stale generation")
        meta = dict(line.split("=", 1) for line in
                    (self.state / (self.task + ".meta")).read_text().splitlines() if "=" in line)
        if meta.get("busy_gen") != self.gen or meta.get("codex_transport") != "appserver":
            raise ValueError("metadata generation/transport mismatch")

    @contextlib.contextmanager
    def bound(self):
        lock = self.state / (self.task + ".busy-state.lock")
        deadline = time.monotonic() + 2
        while True:
            try:
                lock.mkdir()
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ValueError("generation lock timeout")
                time.sleep(.02)
        try:
            self.check()
            yield
        finally:
            lock.rmdir()

    def busy(self, state, event):
        subprocess.run(["bash", str(BIN / "fm-busy-event.sh"), "apply", str(self.state),
                        self.task, state, "--gen", self.gen, "--source", "codex-appserver",
                        "--event", event], check=True, stdout=subprocess.DEVNULL)

    def report(self, text):
        subprocess.run(["bash", str(BIN / "fm-busy-event.sh"), "report", str(self.state),
                        self.task, "--gen", self.gen], input=text, text=True, check=True)

    def retire_pending(self):
        if self.pending:
            key = self.pending["key"]
            self.pending = None
            self.report("resolved [key=" + key + "]: callback retired because its turn stopped")

    def write(self, message):
        with self.bound():
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()

    def reader(self):
        try:
            while True:
                line = self.proc.stdout.readline(1048577)
                if not line:
                    break
                if len(line) > 1048576 or not line.endswith("\n"):
                    self.events.put(ValueError("oversized protocol message"))
                    break
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    self.events.put(ValueError("malformed protocol message"))
                    break
        finally:
            self.events.put(EOFError("app-server stdout closed"))

    def rpc(self, method, params, timeout=30):
        self.sequence += 1
        ident = "fm-" + str(self.sequence)
        self.write({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while ident not in self.responses:
            self.pump(max(.01, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise TimeoutError(method)
        reply = self.responses.pop(ident)
        if "error" in reply:
            raise ValueError(str(reply["error"]))
        return reply["result"]

    def tool_reply(self, ident, text, success=True):
        self.write({"id": ident, "result": {"contentItems": [
            {"type": "inputText", "text": text}], "success": success}})

    def correlate(self, params):
        self.check()
        if params.get("threadId") != self.thread or params.get("turnId") != self.turn or not self.active:
            raise ValueError("wrong or retired thread/turn")

    def tool(self, msg):
        p = msg["params"]
        ident = msg["id"]
        # Never answer a repeated request id: it could prematurely resolve the
        # original pending decision. Treat broken server correlation as fatal.
        if type(ident) not in (int, str) or ident in self.requests:
            raise ValueError("duplicate or malformed request identity")
        try:
            self.correlate(p)
            if p.get("tool") != "firstmate_report" or p.get("namespace") not in (None, ""):
                raise ValueError("unknown tool")
            call = p.get("callId")
            if (not isinstance(call, str) or not 1 <= len(call) <= 160
                    or call in self.calls or len(self.calls) >= 4096):
                raise ValueError("duplicate or malformed call")
            kind, text = payload(p.get("arguments"))
            self.calls.add(call)
            self.requests.add(ident)
            if self.pending:
                raise ValueError("a decision already owns the pending response")
            if kind == "needs-decision":
                key = "codex-" + self.gen + "-" + str(len(self.calls))
                self.report("needs-decision [key=" + key + "]: " + text)
                self.pending = {"id": msg["id"], "key": key, "answer": None}
                return
            if kind == "result":
                if self.result is not None:
                    raise ValueError("result already reported")
                self.result = text
            else:
                self.report("working: " + text)
            self.tool_reply(msg["id"], "accepted " + kind)
        except ValueError as exc:
            self.tool_reply(msg["id"], str(exc), False)

    def pump(self, timeout=.1):
        try:
            msg = self.events.get(timeout=min(timeout, 30))
        except queue.Empty:
            return
        if isinstance(msg, Exception):
            raise msg
        if not isinstance(msg, dict):
            raise ValueError("protocol object required")
        method = msg.get("method")
        if method is None and "id" in msg:
            if not isinstance(msg["id"], str) or msg["id"] != "fm-" + str(self.sequence):
                raise ValueError("unexpected response identity")
            self.responses[msg["id"]] = msg
            return
        if not isinstance(method, str):
            raise ValueError("protocol method required")
        p = msg.get("params", {})
        if not isinstance(p, dict):
            raise ValueError("notification params must be an object")
        if "id" in msg:
            if method == "item/tool/call":
                self.tool(msg)
            elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self.write({"id": msg["id"], "result": {"decision": "decline"}})
            else:
                self.write({"id": msg["id"], "error": {"code": -32601, "message": "unsupported worker request"}})
            return
        if method in ("turn/started", "turn/completed") and not isinstance(p.get("turn"), dict):
            raise ValueError("notification turn must be an object")
        if method.startswith("item/") and "item" in p and not isinstance(p["item"], dict):
            raise ValueError("notification item must be an object")
        if method in ("turn/started", "turn/completed") or (
                method == "item/completed" and p.get("item", {}).get("type") == "commandExecution"):
            print(json.dumps({"method": method, "params": p}), flush=True)
        if method == "turn/started":
            turn = p.get("turn", {})
            if p.get("threadId") != self.thread or turn.get("status") != "inProgress" or not isinstance(turn.get("id"), str):
                raise ValueError("invalid turn start")
            if self.turn is not None and self.turn != turn["id"]:
                raise ValueError("unexpected replacement turn")
            self.turn, self.active = turn["id"], True
            self.busy("busy", "turn-started")
        elif method == "turn/completed":
            turn = p.get("turn", {})
            self.correlate({"threadId": p.get("threadId"), "turnId": turn.get("id")})
            status = turn.get("status")
            if status not in ("completed", "failed", "interrupted"):
                raise ValueError("invalid terminal status")
            self.active = False
            self.retire_pending()
            self.terminal = status
            if status == "completed" and self.result is not None:
                self.report("done: " + self.result)
            elif status != "completed":
                self.result = None
                self.report("failed: app-server turn " + status)
            event = "turn-completed-result" if status == "completed" and self.result is not None else "turn-" + status
            self.busy("idle", event)
        elif method == "item/agentMessage/delta":
            print(p.get("delta", ""), end="", flush=True)

    def decisions(self):
        return subprocess.check_output(["bash", "-c",
            'source "$1/fm-classify-lib.sh"; status_open_decisions "$2"',
            "bash", str(BIN), str(self.state / (self.task + ".status"))], text=True)

    def answer_ready(self):
        if self.pending and self.pending["answer"] is not None:
            keys = [row.split("\t")[0] for row in self.decisions().splitlines()]
            if self.pending["key"] not in keys:
                self.tool_reply(self.pending["id"], self.pending["answer"])
                self.pending = None

    def command(self, data):
        if not isinstance(data, dict) or set(data) != {"generation", "operation", "key", "text"}:
            raise ValueError("malformed control")
        if data["generation"] != self.gen:
            raise ValueError("stale generation")
        self.check()
        op, text = data["operation"], data["text"]
        if not isinstance(text, str) or len(text.encode()) > LIMIT:
            raise ValueError("control text too large")
        if op == "status":
            if self.proc.poll() is not None:
                raise ValueError("app-server process exited")
            return "alive " + str(self.thread) + " " + str(self.turn)
        if op == "answer":
            if not self.active or not self.pending or self.pending["key"] != data["key"]:
                raise ValueError("no matching pending decision")
            if self.pending["answer"] is not None or not text.strip():
                raise ValueError("duplicate or empty answer")
            self.pending["answer"] = text
            return "answer accepted; waiting for canonical decision closure"
        if data["key"]:
            raise ValueError("key only valid for answer")
        if op == "steer":
            if not text.strip() or not self.active or self.pending or self.decisions().strip():
                raise ValueError("steer requires active turn without an open decision")
            reply = self.rpc("turn/steer", {"threadId": self.thread, "expectedTurnId": self.turn,
                "input": [{"type": "text", "text": text, "text_elements": []}]})
            if reply.get("turnId") != self.turn:
                raise ValueError("steer turn mismatch")
            return "steered " + self.turn
        if op in ("interrupt", "exit"):
            if self.active:
                self.rpc("turn/interrupt", {"threadId": self.thread, "turnId": self.turn})
                deadline = time.monotonic() + 15
                while self.active and time.monotonic() < deadline:
                    self.pump(.1)
                if self.active:
                    raise TimeoutError("interrupt terminal event missing")
            self.pending = None
            self.stopping = True
            self.shutdown()
            return "stopped app-server pid=" + str(self.proc.pid) + " exit=" + str(self.proc.returncode)
        raise ValueError("unknown control operation")

    def serve_control(self, listener):
        try:
            conn, _ = listener.accept()
        except BlockingIOError:
            return
        with conn:
            conn.settimeout(2)
            try:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(LIMIT + 1024 - len(data))
                    if not chunk or len(data) >= LIMIT + 1024:
                        raise ValueError("invalid control framing")
                    data += chunk
                result = {"ok": True, "message": self.command(json.loads(data))}
            except (ValueError, TimeoutError, OSError) as exc:
                result = {"ok": False, "message": str(exc)}
            try:
                conn.sendall(json.dumps(result).encode() + b"\n")
            except BrokenPipeError:
                pass

    def shutdown(self):
        self.pending = None
        if self.proc is None:
            return
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()

    def run(self, brief, worktree, model, effort):
        worktree = worktree.resolve()
        if self.state == worktree or worktree in self.state.parents:
            raise ValueError("worker workspace would grant fleet state")
        listener = socket.socket(socket.AF_UNIX)
        # bind is exclusive for this generation. A competing launch must not
        # overwrite lifecycle evidence or remove the existing owner's socket.
        listener.bind(str(self.path))
        try:
            os.chmod(self.path, 0o600)
            listener.listen(4)
            listener.setblocking(False)
            self.proc = subprocess.Popen(["codex", "app-server", "--stdio", "--disable", "hooks",
                "-c", 'approval_policy="never"', "-c", 'sandbox_mode="workspace-write"'],
                cwd=worktree, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                start_new_session=True)
            print("app-server started pid=" + str(self.proc.pid), flush=True)
            threading.Thread(target=self.reader, daemon=True).start()
            self.rpc("initialize", {"clientInfo": {"name": "firstmate", "version": "1"},
                "capabilities": {"experimentalApi": True}})
            self.write({"method": "initialized", "params": {}})
            params = {"cwd": str(worktree), "ephemeral": True, "sandbox": "workspace-write",
                "approvalPolicy": "never", "dynamicTools": [TOOL], "config": {
                    "features.hooks": False, "sandbox_workspace_write.writable_roots": [],
                    "sandbox_workspace_write.network_access": False,
                    "sandbox_workspace_write.exclude_slash_tmp": True,
                    "sandbox_workspace_write.exclude_tmpdir_env_var": True}}
            if model:
                params["model"] = model
            reply = self.rpc("thread/start", params)
            sandbox = reply.get("sandbox", {})
            if (reply.get("approvalPolicy") != "never" or reply.get("cwd") != str(worktree)
                    or reply.get("runtimeWorkspaceRoots") != [str(worktree)]
                    or sandbox.get("type") != "workspaceWrite"
                    or sandbox.get("networkAccess") is not False
                    or sandbox.get("excludeSlashTmp") is not True
                    or sandbox.get("excludeTmpdirEnvVar") is not True
                    or any(Path(root).resolve() != worktree for root in sandbox.get("writableRoots", []))):
                raise ValueError("server did not establish the required worker sandbox")
            self.thread = reply["thread"]["id"]
            prompt = brief.read_text() + "\nUse firstmate_report for all supervisor reporting. Never write FirstMate state directly. Use type needs-decision to ask and wait for an answer. Use type result for your final delivery evidence. Do not use a shell status/inbox channel."
            params = {"threadId": self.thread, "input": [{"type": "text", "text": prompt, "text_elements": []}]}
            if effort:
                params["effort"] = effort
            turn = self.rpc("turn/start", params)["turn"]
            if self.turn is not None and self.turn != turn["id"]:
                raise ValueError("turn response mismatch")
            self.turn = turn["id"]
            while not self.stopping:
                self.check()
                self.pump(.1)
                self.serve_control(listener)
                self.answer_ready()
        except BaseException:
            try:
                self.retire_pending()
                self.report("failed: app-server transport stopped without controlled shutdown")
                self.busy("unknown", "process-failed")
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            raise
        finally:
            self.pending = None
            listener.close()
            self.path.unlink(missing_ok=True)
            self.shutdown()


def main():
    mode, state, task, gen, *args = sys.argv[1:]
    state = Path(state)
    if not TOKEN.fullmatch(task) or not TOKEN.fullmatch(gen):
        raise ValueError("invalid binding")
    if mode == "run":
        def stopped(signum, frame):
            raise RuntimeError("adapter signal " + str(signum))
        signal.signal(signal.SIGTERM, stopped)
        signal.signal(signal.SIGHUP, stopped)
        adapter = Adapter(state, task, gen)
        adapter.run(Path(args[0]), Path(args[1]), args[2] if len(args) > 2 else "", args[3] if len(args) > 3 else "")
    elif mode == "control":
        text = sys.stdin.read(LIMIT + 1)
        if len(text.encode()) > LIMIT:
            raise ValueError("control too large")
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(50)
            conn.connect(str(socket_path(state.resolve(), task, gen)))
            conn.sendall(json.dumps({"generation": gen, "operation": args[0],
                "key": args[1] if len(args) > 1 else "", "text": text}).encode() + b"\n")
            result = json.loads(conn.makefile().readline(LIMIT))
            print(result["message"])
            if not result["ok"]:
                return 1
    else:
        raise ValueError("unknown mode")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, EOFError, TimeoutError, subprocess.SubprocessError) as error:
        print("app-server: " + str(error), file=sys.stderr)
        sys.exit(1)
