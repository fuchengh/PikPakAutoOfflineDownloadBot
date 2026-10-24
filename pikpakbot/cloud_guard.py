"""Account-wide, restart-safe circuit breaker. Contains no credentials or task state."""
from __future__ import annotations

from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time


class CloudBlocked(Exception):
    pass


def rate_limited(status=0, description=""):
    text = str(description).lower().replace("_", " ")
    return status == 429 or "too frequent" in text or "too many requests" in text


def retry_delay(value, now):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(str(value)).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


class CloudGuard:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path or os.environ.get("PIKPAK_CLOUD_STATE", str(
            Path.home() / ".local/state/pikpak-next/cloud-status.json")))
        self.clock = clock
        self._mutex = threading.RLock()
        self._locks = {}
        self._records = {}
        self._broken = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported cloud state")
            self._records = data["accounts"]
            if not isinstance(self._records, dict):
                raise ValueError("Invalid accounts")
            for row in self._records.values():
                if not isinstance(row, dict) or any(not isinstance(row.get(k, 0), (int, float))
                    or not math.isfinite(row.get(k, 0)) for k in
                    ("retry_at", "last_checked", "last_success", "failures")):
                    raise ValueError("Invalid cloud record")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError):
            self._broken = True  # Never bypass a saved cooldown on corrupt state.

    @staticmethod
    def key(account):
        return hashlib.sha256(account.encode()).hexdigest()

    def lock(self, account):
        with self._mutex:
            return self._locks.setdefault(self.key(account), threading.RLock())

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self.path.with_suffix(".new")
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
                json.dump({"version": 1, "accounts": self._records}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.path)
        except OSError:
            self._broken = True
            raise CloudBlocked("PikPak 冷卻紀錄無法儲存，已暫停雲端請求") from None

    def check(self, account):
        with self._mutex:
            if self._broken:
                raise CloudBlocked("PikPak 冷卻紀錄無法讀取，請檢查主機狀態檔")
            row = self._records.get(self.key(account), {})
            remaining = math.ceil(row.get("retry_at", 0) - self.clock())
            if remaining > 0:
                raise CloudBlocked(f"PikPak 雲端冷卻中，{remaining} 秒後才會重新檢查；請勿重送")

    def fail(self, account, reason="transient", retry_after=None):
        with self._mutex:
            now = self.clock()
            row = self._records.setdefault(self.key(account), {})
            # Blocked callers never increase backoff or move its deadline.
            if row.get("retry_at", 0) > now:
                return
            failures = min(20, int(row.get("failures", 0)) + 1)
            base, cap = (900, 7200) if reason == "rate_limit" else (60, 900)
            if reason == "auth":
                base, cap = 900, 7200
            delay = max(min(cap, base * 2 ** (failures - 1)), retry_delay(retry_after, now))
            row.update(reason=reason, failures=failures, retry_at=now + delay, last_checked=now)
            self._save()

    def success(self, account):
        with self._mutex:
            now = self.clock()
            self._records[self.key(account)] = dict(reason="", failures=0, retry_at=0,
                                                     last_checked=now, last_success=now)
            self._save()

    def account_status(self, account):
        with self._mutex:
            row = dict(self._records.get(self.key(account), {}))
            now = self.clock()
            remaining = max(0, math.ceil(row.get("retry_at", 0) - now))
            status, message = "unknown", "尚未取得 PikPak 雲端回應"
            if self._broken:
                status, message = "error", "冷卻紀錄無法讀取或儲存，雲端請求已暫停"
            elif remaining:
                status = "cooldown"
                message = {"rate_limit": "PikPak 限流，已暫停雲端請求",
                           "auth": "PikPak 驗證失敗，等待重新確認"}.get(row.get("reason"), "PikPak 暫時無法連線，等待重試")
            elif row.get("reason"):
                status, message = "recovering", "冷卻已結束，等待雲端重新確認"
            elif row.get("last_success") and now - row["last_success"] < 600:
                status, message = "ok", "已取得 PikPak 雲端實際回應"
            elif row.get("last_success"):
                message = "雲端狀態已過期，等待重新確認"
            return dict(status=status, message=message, reason=row.get("reason"),
                        last_checked=row.get("last_checked"), last_success=row.get("last_success"),
                        retry_at=row.get("retry_at") or None, retry_after_seconds=remaining)

    def status(self, accounts):
        rows = [self.account_status(a) for a in accounts]
        if not rows:
            return dict(status="unknown", message="主機尚未設定 PikPak 帳號")
        rank = {"error": 5, "cooldown": 4, "recovering": 3, "unknown": 2, "ok": 1}
        # Present the least healthy account, never a misleading all-green status.
        worst = max(rows, key=lambda r: (rank[r["status"]], r.get("retry_at") or 0))
        return dict(worst, accounts=len(rows), healthy_accounts=sum(r["status"] == "ok" for r in rows))


guard = CloudGuard()
