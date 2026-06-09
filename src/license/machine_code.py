import hashlib
import platform
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


def _get_mac_address() -> str:
    mac = uuid.getnode()
    return ":".join(f"{(mac >> i) & 0xFF:02x}" for i in range(40, -1, -8))


def get_machine_code() -> str:
    raw = "|".join(
        [
            _get_cpu_id(),
            _get_motherboard_serial(),
            _get_mac_address(),
            platform.system(),
            platform.machine(),
        ]
    )
    hash_bytes = hashlib.sha256(raw.encode()).digest()
    return hash_bytes[:8].hex()
