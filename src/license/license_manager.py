import json
import os
import time
from pathlib import Path

import requests

from .machine_code import get_machine_code

CACHE_DIR = Path(os.path.expanduser("~/.config/dy-scraper2.2"))
CACHE_FILE = CACHE_DIR / ".license_cache"

# 默认 SCF 地址，可通过环境变量覆盖
SERVER_URL = os.environ.get(
    "DYSCRAPER_LICENSE_SERVER",
    "https://1329618480-7qe8raaszb.ap-guangzhou.tencentscf.com",
)


class LicenseManager:
    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.machine_code = get_machine_code()

    # ---- 激活 ----
    def activate(self, license_key: str) -> dict:
        try:
            resp = requests.post(
                f"{SERVER_URL}/api/activate",
                json={"machineCode": self.machine_code, "licenseKey": license_key},
                timeout=10,
            )
            data = resp.json()
        except Exception as e:
            return {"success": False, "error": f"无法连接验证服务器: {e}"}

        if data.get("success"):
            self._save_cache(
                {"licenseKey": license_key, "expiry": data["expiry"], "tier": "vip"}
            )
            return {"success": True, "expiry": data["expiry"]}
        return {"success": False, "error": data.get("error", "激活失败")}

    # ---- 验证 ----
    def verify(self) -> dict:
        cache = self._load_cache()
        if not cache:
            return {"valid": False, "tier": "free", "reason": "未激活"}

        try:
            resp = requests.post(
                f"{SERVER_URL}/api/verify",
                json={
                    "machineCode": self.machine_code,
                    "licenseKey": cache["licenseKey"],
                },
                timeout=5,
            )
            data = resp.json()
            if data.get("valid"):
                self._save_cache(
                    {
                        "licenseKey": cache["licenseKey"],
                        "expiry": data["expiry"],
                        "tier": "vip",
                    }
                )
                return {"valid": True, "tier": "vip", "expiry": data["expiry"]}
            else:
                return {
                    "valid": False,
                    "tier": "free",
                    "reason": data.get("reason", "验证失败"),
                }
        except Exception:
            pass

        # 离线兜底
        if cache.get("expiry") and time.time() < cache["expiry"]:
            return {
                "valid": True,
                "tier": "vip",
                "expiry": cache["expiry"],
                "offline": True,
            }

        return {"valid": False, "tier": "free", "reason": "无法连接服务器且缓存已过期"}

    # ---- 缓存 ----
    def _load_cache(self) -> dict | None:
        try:
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text())
                if data.get("machineCode") == self.machine_code:
                    return data
        except Exception:
            pass
        return None

    def _save_cache(self, data: dict):
        data["machineCode"] = self.machine_code
        data["updatedAt"] = time.time()
        CACHE_FILE.write_text(json.dumps(data))

    def clear(self):
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()


# 全局单例
license_manager = LicenseManager()
