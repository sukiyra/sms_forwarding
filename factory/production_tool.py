#!/usr/bin/env python3
"""Windows batch flasher and factory tester for the SMS forwarding board."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


APP_NAME = "SMS 转发器量产工具"
FACTORY_PREFIX = b"@@FACTORY:"
SUPPORTED_MODEMS = {"ML307A", "ML307C", "ML307R", "ML307Y"}


def application_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def import_serial():
    try:
        import serial  # type: ignore
        from serial.tools import list_ports  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 pyserial，请运行 factory/start.ps1 自动安装") from exc
    return serial, list_ports


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class FirmwareImage:
    offset: str
    path: Path


@dataclass(frozen=True)
class FirmwareBundle:
    root: Path
    version: str
    images: tuple[FirmwareImage, ...]

    @classmethod
    def load(cls, root: Path) -> "FirmwareBundle":
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"找不到固件清单：{manifest_path}")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("chip") != "esp32c3":
            raise RuntimeError("固件清单不是 ESP32-C3")
        images: list[FirmwareImage] = []
        for item in data.get("images", []):
            path = root / str(item["file"])
            if not path.is_file():
                raise RuntimeError(f"固件文件缺失：{path.name}")
            expected = str(item.get("sha256", "")).lower()
            actual = sha256_file(path)
            if expected and actual != expected:
                raise RuntimeError(f"固件校验失败：{path.name}")
            images.append(FirmwareImage(str(item["offset"]), path))
        if not images:
            raise RuntimeError("固件清单没有可烧录镜像")
        return cls(root.resolve(), str(data.get("version", "unknown")), tuple(images))


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str
    likely_esp32: bool


def scan_ports() -> list[SerialPortInfo]:
    _, list_ports = import_serial()
    result: list[SerialPortInfo] = []
    for item in list_ports.comports():
        text = f"{item.description} {item.hwid}".upper()
        likely = item.vid == 0x303A or any(
            marker in text for marker in ("ESP32", "ESPRESSIF", "CP210", "CH340", "CH910")
        )
        if "BLUETOOTH" in text:
            continue
        result.append(SerialPortInfo(item.device, item.description or "串口", item.hwid or "", likely))
    return sorted(result, key=lambda value: value.device)


def find_esptool(explicit: str | None = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        raise RuntimeError(f"找不到 esptool：{candidate}")
    bundled = application_root() / "esptool.exe"
    if bundled.is_file():
        return bundled.resolve()
    command = shutil.which("esptool") or shutil.which("esptool.exe")
    if command:
        return Path(command).resolve()
    arduino_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Arduino15" / "packages" / "esp32" / "tools" / "esptool_py"
    candidates = sorted(arduino_root.glob("*/esptool.exe"), reverse=True)
    if candidates:
        return candidates[0].resolve()
    raise RuntimeError("找不到 esptool。请先运行 factory/build_firmware.ps1 安装 ESP32 工具链")


def run_process(command: list[str], log: Callable[[str], None], timeout: int = 180) -> str:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    lines: list[str] = []
    assert process.stdout is not None
    started = time.monotonic()
    while True:
        line = process.stdout.readline()
        if line:
            value = line.rstrip()
            lines.append(value)
            if value:
                log(value)
        if process.poll() is not None:
            break
        if time.monotonic() - started > timeout:
            process.kill()
            raise RuntimeError("命令执行超时")
    if process.returncode:
        detail = "\n".join(lines[-12:])
        raise RuntimeError(f"命令失败（退出码 {process.returncode}）\n{detail}")
    return "\n".join(lines)


def flash_device(
    port: str,
    bundle: FirmwareBundle,
    esptool: Path,
    erase: bool,
    log: Callable[[str], None],
) -> None:
    base = [str(esptool), "--chip", "esp32c3", "--port", port, "--baud", "921600"]
    if erase:
        run_process(base + ["erase-flash"], log)
    write = base + [
        "--after",
        "hard-reset",
        "write-flash",
        "--flash-mode",
        "dio",
        "--flash-freq",
        "80m",
        "--flash-size",
        "4MB",
    ]
    for image in bundle.images:
        write.extend((image.offset, str(image.path)))
    try:
        run_process(write, log, timeout=240)
    except RuntimeError:
        # Long USB hubs and marginal cables are often stable at 460800.
        log("921600 烧录失败，自动降速到 460800 重试")
        write[write.index("921600")] = "460800"
        run_process(write, log, timeout=300)


def factory_status(
    port: str,
    timeout: int,
    log: Callable[[str], None],
    accept: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    serial, _ = import_serial()
    deadline = time.monotonic() + timeout
    last_open_error = ""
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            with serial.Serial(port, 115200, timeout=0.25, write_timeout=1) as link:
                link.reset_input_buffer()
                while time.monotonic() < deadline:
                    link.write(b"FACTORY STATUS\r\n")
                    link.flush()
                    read_until = min(deadline, time.monotonic() + 2.5)
                    while time.monotonic() < read_until:
                        line = link.readline().strip()
                        if not line.startswith(FACTORY_PREFIX):
                            continue
                        payload = line[len(FACTORY_PREFIX) :].decode("utf-8", "replace")
                        status = json.loads(payload)
                        last_status = status
                        if accept is None or accept(status):
                            log("已读取固件工厂状态")
                            return status
                    time.sleep(0.5)
        except (OSError, serial.SerialException) as exc:
            last_open_error = str(exc)
            time.sleep(1)
    if last_status is not None:
        log("自检等待到期，使用最后一次工厂状态判定")
        return last_status
    suffix = f"：{last_open_error}" if last_open_error else ""
    raise RuntimeError(f"等待固件工厂状态超时{suffix}")


def evaluate_status(status: dict[str, Any], require_sim: bool, require_network: bool) -> list[str]:
    failures: list[str] = []
    chip = status.get("chip") or {}
    modem = status.get("modem") or {}
    sim = status.get("sim") or {}
    if str(chip.get("model", "")).upper() != "ESP32-C3":
        failures.append("主控不是 ESP32-C3")
    family = str(modem.get("family", "")).upper()
    if not modem.get("supported") or family not in SUPPORTED_MODEMS:
        failures.append(f"模组未识别或不受支持：{family or 'unknown'}")
    if require_sim and not sim.get("ready"):
        failures.append("SIM 未就绪")
    if require_sim and (not sim.get("smsReady") or modem.get("smsMode") not in {"direct", "stored"}):
        failures.append("短信接口未配置")
    if require_network and not modem.get("registered"):
        failures.append("未完成蜂窝网络注册")
    return failures


_record_lock = threading.Lock()


def save_record(record_root: Path, port: str, status: dict[str, Any], result: str, error: str) -> None:
    record_root.mkdir(parents=True, exist_ok=True)
    chip = status.get("chip") or {}
    modem = status.get("modem") or {}
    sim = status.get("sim") or {}
    network = status.get("network") or {}
    now = datetime.now(timezone.utc).astimezone()
    row = {
        "time": now.isoformat(timespec="seconds"),
        "result": result,
        "port": port,
        "chip_id": chip.get("id", ""),
        "firmware": status.get("firmware", ""),
        "modem_family": modem.get("family", ""),
        "modem_model": modem.get("model", ""),
        "modem_firmware": modem.get("firmware", ""),
        "sim_type": sim.get("type", ""),
        "iccid_tail": sim.get("iccidTail", ""),
        "home_plmn": sim.get("homePlmn", ""),
        "network_plmn": network.get("plmn", ""),
        "error": error,
    }
    with _record_lock:
        csv_path = record_root / "production.csv"
        new_file = not csv_path.exists()
        with csv_path.open("a", newline="", encoding="utf-8-sig") as target:
            writer = csv.DictWriter(target, fieldnames=list(row))
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        safe_id = "".join(c for c in str(chip.get("id") or port) if c.isalnum() or c in "-_")
        detail = {"result": result, "error": error, "port": port, "capturedAt": row["time"], "status": status}
        (record_root / f"{now:%Y%m%d-%H%M%S}-{safe_id}.json").write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def produce_one(
    port: str,
    bundle: FirmwareBundle | None,
    esptool: Path,
    record_root: Path,
    erase: bool,
    skip_flash: bool,
    require_sim: bool,
    require_network: bool,
    timeout: int,
    log: Callable[[str], None],
) -> tuple[bool, dict[str, Any], str]:
    status: dict[str, Any] = {}
    error = ""
    try:
        if not skip_flash:
            if bundle is None:
                raise RuntimeError("未提供固件包")
            log(f"[{port}] 开始烧录 {bundle.version}")
            flash_device(port, bundle, esptool, erase, lambda line: log(f"[{port}] {line}"))
        log(f"[{port}] 等待设备和模组自检")
        status = factory_status(
            port,
            timeout,
            lambda line: log(f"[{port}] {line}"),
            lambda value: not evaluate_status(value, require_sim, require_network),
        )
        failures = evaluate_status(status, require_sim, require_network)
        if failures:
            error = "；".join(failures)
            raise RuntimeError(error)
        log(f"[{port}] PASS · {status.get('firmware')} · {(status.get('modem') or {}).get('model')}")
        save_record(record_root, port, status, "PASS", "")
        return True, status, ""
    except Exception as exc:  # one failed unit must not stop the batch
        error = str(exc)
        log(f"[{port}] FAIL · {error}")
        try:
            save_record(record_root, port, status, "FAIL", error)
        except Exception as record_exc:
            error += f"；生产记录写入失败：{record_exc}"
            log(f"[{port}] {error}")
        return False, status, error


class ProductionApp:
    def __init__(self, root: Any, defaults: argparse.Namespace) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("940x680")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.port_rows: dict[str, str] = {}

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_NAME, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="批量烧录 ESP32-C3，并自动验收 ML307A / ML307C / ML307Y").pack(anchor="w", pady=(2, 12))

        options = ttk.Frame(frame)
        options.pack(fill="x")
        self.firmware_var = tk.StringVar(value=str(defaults.firmware_dir))
        self.esptool_var = tk.StringVar(value=str(defaults.esptool or ""))
        ttk.Label(options, text="固件目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.firmware_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(options, text="选择", command=self.choose_firmware).grid(row=0, column=2)
        ttk.Label(options, text="esptool").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(options, textvariable=self.esptool_var).grid(row=1, column=1, sticky="ew", padx=8, pady=(6, 0))
        ttk.Button(options, text="自动查找", command=self.fill_esptool).grid(row=1, column=2, pady=(6, 0))
        options.columnconfigure(1, weight=1)

        checks = ttk.Frame(frame)
        checks.pack(fill="x", pady=10)
        self.erase_var = tk.BooleanVar(value=True)
        self.sim_var = tk.BooleanVar(value=True)
        self.network_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(checks, text="全片擦除（量产推荐）", variable=self.erase_var).pack(side="left")
        ttk.Checkbutton(checks, text="要求 SIM 就绪", variable=self.sim_var).pack(side="left", padx=18)
        ttk.Checkbutton(checks, text="要求成功驻网", variable=self.network_var).pack(side="left")

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="刷新串口", command=self.refresh_ports).pack(side="left")
        self.start_button = ttk.Button(toolbar, text="开始所选设备", command=self.start)
        self.start_button.pack(side="left", padx=8)
        ttk.Label(toolbar, text="可多选；同一批设备并行烧录").pack(side="left")

        self.tree = ttk.Treeview(frame, columns=("port", "desc", "type", "status"), show="headings", height=8, selectmode="extended")
        for key, title, width in (("port", "串口", 80), ("desc", "设备", 400), ("type", "识别", 120), ("status", "结果", 240)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="x")

        ttk.Label(frame, text="运行日志").pack(anchor="w", pady=(12, 4))
        self.log_widget = tk.Text(frame, height=18, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True)
        self.refresh_ports()
        self.root.after(100, self.poll_events)

    def choose_firmware(self) -> None:
        value = self.filedialog.askdirectory(initialdir=self.firmware_var.get())
        if value:
            self.firmware_var.set(value)

    def fill_esptool(self) -> None:
        try:
            self.esptool_var.set(str(find_esptool()))
        except Exception as exc:
            self.messagebox.showerror(APP_NAME, str(exc))

    def refresh_ports(self) -> None:
        try:
            ports = scan_ports()
        except Exception as exc:
            self.messagebox.showerror(APP_NAME, str(exc))
            return
        self.tree.delete(*self.tree.get_children())
        self.port_rows.clear()
        for item in ports:
            row = self.tree.insert("", "end", values=(item.device, item.description, "ESP32 候选" if item.likely_esp32 else "普通串口", "待处理"))
            self.port_rows[item.device] = row
            if item.likely_esp32:
                self.tree.selection_add(row)

    def emit(self, kind: str, value: Any) -> None:
        self.events.put((kind, value))

    def log(self, value: str) -> None:
        self.emit("log", value)

    def start(self) -> None:
        if self.running:
            return
        selected = [str(self.tree.item(row, "values")[0]) for row in self.tree.selection()]
        if not selected:
            self.messagebox.showinfo(APP_NAME, "请先选择至少一个串口")
            return
        try:
            bundle = FirmwareBundle.load(Path(self.firmware_var.get()))
            esptool = find_esptool(self.esptool_var.get() or None)
        except Exception as exc:
            self.messagebox.showerror(APP_NAME, str(exc))
            return
        self.running = True
        self.start_button.configure(state="disabled")
        erase = self.erase_var.get()
        require_sim = self.sim_var.get()
        require_network = self.network_var.get()
        for port in selected:
            self.tree.set(self.port_rows[port], "status", "处理中")

        def worker() -> None:
            record_root = application_root() / "records"
            passed = 0
            with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
                futures = {
                    pool.submit(
                        produce_one, port, bundle, esptool, record_root,
                        erase, False, require_sim,
                        require_network, 120, self.log,
                    ): port for port in selected
                }
                for future in as_completed(futures):
                    port = futures[future]
                    ok, status, error = future.result()
                    passed += int(ok)
                    modem = (status.get("modem") or {}).get("family", "")
                    self.emit("status", (port, "PASS " + modem if ok else "FAIL " + error))
            self.emit("done", (passed, len(selected)))

        threading.Thread(target=worker, daemon=True).start()

    def poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log_widget.configure(state="normal")
                    self.log_widget.insert("end", str(value) + "\n")
                    self.log_widget.see("end")
                    self.log_widget.configure(state="disabled")
                elif kind == "status":
                    port, status = value
                    if port in self.port_rows:
                        self.tree.set(self.port_rows[port], "status", status)
                elif kind == "done":
                    passed, total = value
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.messagebox.showinfo(APP_NAME, f"本批完成：{passed}/{total} 台通过\n记录已写入 factory/records")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    root = application_root()
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--list", action="store_true", help="列出串口")
    parser.add_argument("--ports", nargs="+", help="量产指定串口，例如 COM3 COM4")
    parser.add_argument("--all", action="store_true", help="量产所有识别为 ESP32 的串口")
    parser.add_argument("--firmware-dir", type=Path, default=root / "dist")
    parser.add_argument("--esptool")
    parser.add_argument("--records", type=Path, default=root / "records")
    parser.add_argument("--skip-flash", action="store_true", help="只执行工厂验收")
    parser.add_argument("--keep-data", action="store_true", help="烧录前不擦除全片")
    parser.add_argument("--require-sim", action="store_true")
    parser.add_argument("--require-network", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    if args.list:
        for item in scan_ports():
            print(f"{item.device}\t{'ESP32' if item.likely_esp32 else '-'}\t{item.description}")
        return 0
    ports = list(args.ports or [])
    if args.all:
        ports.extend(item.device for item in scan_ports() if item.likely_esp32)
    ports = list(dict.fromkeys(ports))
    if not ports:
        import tkinter as tk
        root = tk.Tk()
        ProductionApp(root, args)
        root.mainloop()
        return 0

    bundle = None if args.skip_flash else FirmwareBundle.load(args.firmware_dir)
    esptool = find_esptool(args.esptool)
    passed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(ports)))) as pool:
        futures = {
            pool.submit(
                produce_one, port, bundle, esptool, args.records,
                not args.keep_data, args.skip_flash, args.require_sim,
                args.require_network, args.timeout, print,
            ): port for port in ports
        }
        for future in as_completed(futures):
            ok, _, _ = future.result()
            passed += int(ok)
    print(f"完成：{passed}/{len(ports)} 台通过")
    return 0 if passed == len(ports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
