import hashlib
import platform
import re
import subprocess
import uuid


def _run_ps(cmd: str) -> str:
    """Run a PowerShell command, return stdout stripped or '' on failure."""
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", cmd],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return output.decode(errors="ignore").strip()
    except Exception:
        return ""


def _get_cpu_id() -> str:
    system = platform.system()
    if system == "Windows":
        return _run_ps("Get-CimInstance Win32_Processor | Select-Object -ExpandProperty ProcessorId")
    elif system == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "Serial" in line:
                        return line.split(":")[1].strip()
        except Exception:
            pass
    elif system == "Darwin":
        try:
            return (
                subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5
                )
                .decode()
                .strip()
            )
        except Exception:
            pass
    return ""


def _get_motherboard_serial() -> str:
    system = platform.system()
    if system == "Windows":
        return _run_ps("Get-CimInstance Win32_BaseBoard | Select-Object -ExpandProperty SerialNumber")
    elif system == "Linux":
        try:
            with open("/sys/class/dmi/id/board_serial") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _get_disk_serial() -> str:
    """获取系统盘序列号，提高机器区分度 (Vulnerability 4 fix)"""
    system = platform.system()
    if system == "Windows":
        # 只取固定硬盘的系统盘序列号
        output = _run_ps(
            "Get-CimInstance Win32_DiskDrive | Where-Object { $_.MediaType -eq 'Fixed hard disk media' } | Select-Object -First 1 -ExpandProperty SerialNumber"
        )
        if output:
            return output.strip()
        # 备选: wmic (兼容旧系统)
        try:
            raw = subprocess.check_output(
                ["wmic", "diskdrive", "where", "MediaType='Fixed hard disk media'",
                 "get", "SerialNumber"],
                timeout=5
            ).decode(errors="ignore")
            lines = raw.strip().split("\n")
            return lines[1].strip() if len(lines) > 1 else ""
        except Exception:
            return ""
    elif system == "Linux":
        try:
            output = subprocess.check_output(
                ["lsblk", "-o", "SERIAL", "-nd"], timeout=5
            ).decode(errors="ignore")
            return output.strip().split("\n")[0].strip()
        except Exception:
            try:
                # 备选: /sys/block/sda/device/serial
                with open("/sys/block/sda/device/serial") as f:
                    return f.read().strip()
            except Exception:
                return ""
    elif system == "Darwin":
        try:
            output = subprocess.check_output(
                ["system_profiler", "SPSerialATADataType"], timeout=5
            ).decode(errors="ignore")
            match = re.search(r"Serial Number:\s*(\S+)", output)
            return match.group(1) if match else ""
        except Exception:
            return ""
    return ""


def _get_mac_address() -> str:
    mac = uuid.getnode()
    return ":".join(f"{(mac >> i) & 0xFF:02x}" for i in range(40, -1, -8))


def get_machine_code() -> str:
    raw = "|".join(
        [
            _get_cpu_id(),
            _get_motherboard_serial(),
            _get_disk_serial(),
            _get_mac_address(),
            platform.system(),
            platform.machine(),
        ]
    )
    hash_bytes = hashlib.sha256(raw.encode()).digest()
    return hash_bytes[:8].hex()
