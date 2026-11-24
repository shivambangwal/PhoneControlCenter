#!/usr/bin/env python3
"""Loopback-only phone bridge for the Phone Control Center web dashboard.

Run this bridge on the Mac or Windows PC with the USB phones. Hostinger only
serves the static dashboard. Configure its exact HTTPS origin in bridge.json.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "bridge.json"
FALLBACK_INTERVAL = 0.9
LIVE_FPS = 6


def read_config() -> dict[str, str]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {
            "token": str(data.get("token", "")),
            "allowed_origin": str(data.get("allowed_origin", "")).rstrip("/"),
        }
    except (OSError, ValueError, TypeError):
        return {"token": "", "allowed_origin": ""}


CONFIG = read_config()
ADB = shutil.which("adb") or "adb"
FFMPEG = shutil.which("ffmpeg")
_devices_lock = threading.Lock()
_devices_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
_workers_lock = threading.Lock()
_workers: dict[str, "PreviewWorker"] = {}
_ui_clients = 0

app = FastAPI(title="Phone Control Center local bridge", docs_url=None, redoc_url=None)
if CONFIG["allowed_origin"]:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[CONFIG["allowed_origin"]],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def _authorized(authorization: str | None) -> bool:
    if not CONFIG["token"] or not authorization:
        return False
    prefix = "Bearer "
    return authorization.startswith(prefix) and secrets.compare_digest(
        authorization[len(prefix):], CONFIG["token"]
    )


def _require_auth(authorization: str | None) -> None:
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="Enter the local bridge token.")


def _run(args: list[str], timeout: float = 8, binary: bool = False):
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=timeout,
            text=not binary, check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, b"" if binary else "", str(exc)


def list_devices(force: bool = False) -> list[dict[str, Any]]:
    global _devices_cache
    with _devices_lock:
        cached_at, cached = _devices_cache
        if not force and time.monotonic() - cached_at < 2.0:
            return [dict(item) for item in cached]

        rc, out, _err = _run([ADB, "devices", "-l"], timeout=8)
        found: list[dict[str, Any]] = []
        if rc == 0:
            for line in out.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 2:
                    continue
                serial, state = fields[0], fields[1]
                if serial.startswith("*") or serial == "List":
                    continue
                attrs = dict(item.split(":", 1) for item in fields[2:] if ":" in item)
                model = attrs.get("model", "Android phone").replace("_", " ")
                width, height = _device_size(serial) if state == "device" else (720, 1600)
                found.append({
                    "serial": serial,
                    "label": model,
                    "state": state,
                    "width": width,
                    "height": height,
                })
        _devices_cache = (time.monotonic(), found)
        return [dict(item) for item in found]

def _device_size(serial: str) -> tuple[int, int]:
    rc, out, _ = _run([ADB, "-s", serial, "shell", "wm", "size"], timeout=4)
    matches = re.findall(r"(\d+)x(\d+)", out if rc == 0 else "")
    if not matches:
        return 720, 1600
    return tuple(map(int, matches[-1]))


class PreviewWorker:
    """Keeps the newest frame for one authorized Android device."""

    def __init__(self, serial: str):
        self.serial = serial
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.frame: dict[str, Any] | None = None
        self.sequence = 0
        self.state = "Starting preview"
        self.process_lock = threading.Lock()
        self.processes: list[subprocess.Popen] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.process_lock:
            for proc in self.processes:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass

    def snapshot(self):
        with self.lock:
            return self.sequence, dict(self.frame) if self.frame else None, self.state

    def _publish(self, image: bytes, mime: str, state: str):
        with self.lock:
            self.sequence += 1
            self.frame = {
                "serial": self.serial,
                "mime": mime,
                "image": base64.b64encode(image).decode("ascii"),
                "updated": time.time(),
            }
            self.state = state

    def _set_state(self, state: str):
        with self.lock:
            self.state = state

    def _run(self):
        retry_live_at = 0.0
        while not self.stop_event.is_set():
            if FFMPEG and time.monotonic() >= retry_live_at:
                if self._run_live_stream():
                    retry_live_at = 0.0
                    continue
                retry_live_at = time.monotonic() + 10
                self._set_state("Using ADB screenshot fallback")
            self._capture_once()
            self.stop_event.wait(FALLBACK_INTERVAL)

    def _capture_once(self):
        rc, data, _err = _run(
            [ADB, "-s", self.serial, "exec-out", "screencap", "-p"],
            timeout=6, binary=True,
        )
        if rc == 0 and isinstance(data, bytes) and data.startswith(b"\x89PNG"):
            self._publish(data, "image/png", "ADB screenshot fallback")
        else:
            self._set_state("No screen frame; wake and unlock this phone")

    def _run_live_stream(self) -> bool:
        adb_proc = ffmpeg_proc = None
        had_frames = False
        started = time.monotonic()
        frame_count = 0
        feeder_done = threading.Event()
        try:
            adb_proc = subprocess.Popen(
                [ADB, "-s", self.serial, "exec-out", "screenrecord",
                 "--output-format=h264", "--bit-rate", "8000000",
                 "--time-limit", "180", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            ffmpeg_proc = subprocess.Popen(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-threads", "1", "-f", "h264", "-i", "pipe:0",
                 "-vf", f"fps={LIVE_FPS}", "-an", "-q:v", "4",
                 "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            with self.process_lock:
                self.processes = [adb_proc, ffmpeg_proc]

            def feed():
                try:
                    while not self.stop_event.is_set():
                        chunk = adb_proc.stdout.read(65536)
                        if not chunk:
                            break
                        ffmpeg_proc.stdin.write(chunk)
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    feeder_done.set()
                    try:
                        ffmpeg_proc.stdin.close()
                    except (AttributeError, OSError, ValueError):
                        pass

            def read_frames():
                nonlocal frame_count, had_frames
                buffer = bytearray()
                try:
                    while not self.stop_event.is_set():
                        chunk = ffmpeg_proc.stdout.read(65536)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                        while True:
                            start = buffer.find(b"\xff\xd8")
                            if start < 0:
                                del buffer[:-1]
                                break
                            if start:
                                del buffer[:start]
                            end = buffer.find(b"\xff\xd9", 2)
                            if end < 0:
                                break
                            image = bytes(buffer[:end + 2])
                            del buffer[:end + 2]
                            self._publish(image, "image/jpeg", "Live H.264")
                            frame_count += 1
                            had_frames = True
                except (OSError, ValueError):
                    pass

            reader = threading.Thread(target=read_frames, daemon=True)
            feeder = threading.Thread(target=feed, daemon=True)
            reader.start()
            feeder.start()
            while not self.stop_event.is_set():
                if adb_proc.poll() is not None or ffmpeg_proc.poll() is not None:
                    break
                if not frame_count and time.monotonic() - started > 10:
                    break
                time.sleep(0.08)
        except (OSError, ValueError):
            pass
        finally:
            if adb_proc and adb_proc.poll() is None:
                try:
                    adb_proc.terminate()
                except OSError:
                    pass
            if feeder_done.wait(0.7) and ffmpeg_proc and ffmpeg_proc.poll() is None:
                try:
                    ffmpeg_proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    pass
            if ffmpeg_proc and ffmpeg_proc.poll() is None:
                try:
                    ffmpeg_proc.terminate()
                except OSError:
                    pass
            for proc in (adb_proc, ffmpeg_proc):
                if proc:
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    except OSError:
                        pass
            with self.process_lock:
                self.processes = []
        return had_frames


def _active_worker(serial: str) -> PreviewWorker:
    with _workers_lock:
        worker = _workers.get(serial)
        if worker is None or worker.stop_event.is_set() or not worker.thread.is_alive():
            worker = PreviewWorker(serial)
            _workers[serial] = worker
        return worker


def _stop_workers():
    with _workers_lock:
        workers = list(_workers.values())
        _workers.clear()
    for worker in workers:
        worker.close()


def _run_action(action: str, serials: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    devices = {item["serial"]: item for item in list_devices(force=True)}
    valid = [
        serial for serial in dict.fromkeys(serials)
        if devices.get(serial, {}).get("state") == "device"
    ]
    if not valid:
        raise HTTPException(status_code=400, detail="No authorized Android devices selected.")

    commands: list[tuple[str, list[str]]] = []
    for serial in valid:
        args: list[str]
        if action == "wake":
            args = [ADB, "-s", serial, "shell", "input", "keyevent", "224"]
        elif action == "launch":
            package = str(payload.get("package", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
                raise HTTPException(status_code=400, detail="Enter a valid Android package name.")
            args = [ADB, "-s", serial, "shell", "monkey", "-p", package,
                    "-c", "android.intent.category.LAUNCHER", "1"]
        elif action in ("tap", "swipe"):
            x1 = _pixel(payload.get("x1"), devices[serial]["width"])
            y1 = _pixel(payload.get("y1"), devices[serial]["height"])
            if action == "tap":
                input_args = ["tap", str(x1), str(y1)]
            else:
                x2 = _pixel(payload.get("x2"), devices[serial]["width"])
                y2 = _pixel(payload.get("y2"), devices[serial]["height"])
                duration = max(60, min(1200, int(payload.get("duration", 350))))
                input_args = ["swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
            args = [ADB, "-s", serial, "shell", "input", *input_args]
        elif action == "key":
            code = str(payload.get("code", ""))
            if code not in {"3", "4", "19", "20", "21", "22", "62", "66", "67"}:
                raise HTTPException(status_code=400, detail="Unsupported Android key.")
            args = [ADB, "-s", serial, "shell", "input", "keyevent", code]
        elif action == "text":
            value = str(payload.get("text", ""))
            if not value or len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9._@+/:?=,%s-]+", value):
                raise HTTPException(status_code=400, detail="Text contains unsupported characters.")
            args = [ADB, "-s", serial, "shell", "input", "text", value]
        else:
            raise HTTPException(status_code=400, detail="Unsupported action.")
        commands.append((serial, args))

    def execute(item: tuple[str, list[str]]) -> dict[str, Any]:
        serial, args = item
        rc, out, err = _run(args, timeout=12)
        return {"serial": serial, "ok": rc == 0, "message": (err or out or "").strip()}

    with ThreadPoolExecutor(max_workers=min(20, len(commands))) as pool:
        results = list(pool.map(execute, commands))
    return {"results": results}


def _pixel(value: Any, dimension: int) -> int:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid screen coordinate.")
    if not 0 <= normalized <= 1:
        raise HTTPException(status_code=400, detail="Screen coordinates must be normalized.")
    return max(0, min(dimension - 1, round(normalized * (dimension - 1))))


class ActionRequest(BaseModel):
    action: str
    serials: list[str] = Field(default_factory=list, max_length=20)
    payload: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "host": platform.system(),
        "adb": bool(shutil.which("adb")),
        "ffmpeg": bool(FFMPEG),
    }


@app.get("/api/devices")
def devices(authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    return {
        "devices": list_devices(force=True),
        "host": platform.system(),
        "ffmpeg": bool(FFMPEG),
    }


@app.post("/api/action")
def action(request: ActionRequest, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    return _run_action(request.action, request.serials, request.payload)


@app.websocket("/ws/preview")
async def preview_socket(socket: WebSocket):
    global _ui_clients
    origin = socket.headers.get("origin", "").rstrip("/")
    if not CONFIG["allowed_origin"] or origin != CONFIG["allowed_origin"]:
        await socket.close(code=1008, reason="Dashboard origin is not allowed")
        return
    await socket.accept()
    try:
        auth = await asyncio.wait_for(socket.receive_json(), timeout=8)
        if auth.get("type") != "auth" or not secrets.compare_digest(
            str(auth.get("token", "")), CONFIG["token"]
        ):
            await socket.close(code=1008, reason="Invalid bridge token")
            return
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await socket.close(code=1008)
        return

    _ui_clients += 1
    sent_sequences: dict[str, int] = {}
    sent_states: dict[str, str] = {}
    last_devices_sent = 0.0
    last_devices_signature = ""
    try:
        while True:
            phones = list_devices()
            online = [p for p in phones if p["state"] == "device"]
            for phone in online:
                _active_worker(phone["serial"])
            signature = json.dumps(phones, sort_keys=True)
            now = time.monotonic()
            if signature != last_devices_signature or now - last_devices_sent > 2.0:
                await socket.send_json({
                    "type": "devices", "devices": phones,
                    "host": platform.system(), "adb": bool(shutil.which("adb")),
                    "ffmpeg": bool(FFMPEG),
                })
                last_devices_signature = signature
                last_devices_sent = now
            for phone in online:
                worker = _active_worker(phone["serial"])
                sequence, frame, state = worker.snapshot()
                if frame and sequence != sent_sequences.get(phone["serial"]):
                    await socket.send_json({"type": "frame", **frame})
                    sent_sequences[phone["serial"]] = sequence
                if sent_states.get(phone["serial"]) != state:
                    await socket.send_json({"type": "preview_state", "serial": phone["serial"], "state": state})
                    sent_states[phone["serial"]] = state
            await asyncio.sleep(0.12)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        _ui_clients = max(0, _ui_clients - 1)
        if _ui_clients == 0:
            _stop_workers()


def _initialize_config(allowed_origin: str = "https://YOUR-HOSTINGER-DOMAIN") -> None:
    if CONFIG_PATH.exists():
        raise SystemExit(f"Config already exists: {CONFIG_PATH}")
    token = secrets.token_urlsafe(32)
    CONFIG_PATH.write_text(json.dumps({
        "token": token,
        "allowed_origin": allowed_origin.rstrip("/"),
    }, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Created {CONFIG_PATH}")
    print(f"Bridge token (save it; enter it in the dashboard): {token}")
    print("Edit allowed_origin to the exact HTTPS origin of your Hostinger site.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create a local token/config once")
    parser.add_argument("--origin", default="https://YOUR-HOSTINGER-DOMAIN",
                        help="Exact HTTPS dashboard origin for a new config")
    args = parser.parse_args()
    if args.init:
        _initialize_config(args.origin)
    else:
        CONFIG = read_config()
        if not CONFIG["token"] or not CONFIG["allowed_origin"]:
            raise SystemExit("Run python3 bridge.py --init, then set allowed_origin in bridge.json.")
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
