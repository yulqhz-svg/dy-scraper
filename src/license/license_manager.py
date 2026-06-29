import hashlib
import hmac
import json
import os
import stat
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

# 最长离线天数：超过此天数未联网验证，强制要求联网
OFFLINE_GRACE_SECONDS = 7 * 24 * 60 * 60  # 7 天


def _derive_cache_key(machine_code: str) -> bytes:
    """从机器码派生缓存签名密钥"""
    return hashlib.sha256(f"dy-scraper-cache:{machine_code}".encode()).digest()


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
                {
                    "licenseKey": license_key,
                    "expiry": data["expiry"],
                    "tier": "vip",
                    "lastVerifiedAt": time.time(),
                }
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
                        "lastVerifiedAt": time.time(),
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

        # ---- 离线兜底（Vulnerability 2 修复） ----
        # 1. 显式校验机器码（防御层：即使 _load_cache 已校验，此处再次确认）
        current_code = self.machine_code
        if cache.get("machineCode") != current_code:
            self.clear()
            return {"valid": False, "tier": "free", "reason": "设备信息已变更，请重新激活"}

        # 2. 连续离线超过 OFFLINE_GRACE_SECONDS 必须联网
        last_seen = cache.get("lastVerifiedAt", 0)
        if time.time() - last_seen > OFFLINE_GRACE_SECONDS:
            return {"valid": False, "tier": "free", "reason": "离线时间过长，请联网验证"}

        # 3. 检查过期
        if cache.get("expiry") and time.time() < cache["expiry"]:
            return {
                "valid": True,
                "tier": "vip",
                "expiry": cache["expiry"],
                "offline": True,
            }

        return {"valid": False, "tier": "free", "reason": "无法连接服务器且缓存已过期"}

    # ---- 缓存 (HIGH-1: HMAC 签名防篡改) ----
    def _load_cache(self) -> dict | None:
        try:
            if not CACHE_FILE.exists():
                return None
            raw = CACHE_FILE.read_text()
            envelope = json.loads(raw)
            payload = envelope.get("data")
            saved_sig = envelope.get("sig")
            if not payload or not saved_sig:
                # 旧格式缓存（无签名）→ 视为无效
                self.clear()
                return None
            # 验证 HMAC
            expected_sig = hmac.new(
                _derive_cache_key(self.machine_code),
                json.dumps(payload, sort_keys=True).encode(),
                "sha256",
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, saved_sig):
                self.clear()
                return None
            # 验证机器码
            if payload.get("machineCode") != self.machine_code:
                self.clear()
                return None
            return payload
        except Exception:
            pass
        return None

    def _save_cache(self, data: dict):
        data["machineCode"] = self.machine_code
        data["updatedAt"] = time.time()
        payload_str = json.dumps(data, sort_keys=True)
        sig = hmac.new(
            _derive_cache_key(self.machine_code),
            payload_str.encode(),
            "sha256",
        ).hexdigest()
        envelope = json.dumps({"data": data, "sig": sig})
        CACHE_FILE.write_text(envelope)
        try:
            os.chmod(CACHE_FILE, stat.S_IREAD | stat.S_IWRITE)  # 0o600 on Windows
        except Exception:
            pass  # Windows 下 chmod 可能无意义，忽略

    def clear(self):
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()


# 全局单例
license_manager = LicenseManager()
