# PikPak 雲端冷卻與桌面整合

PikPak 回覆 HTTP 429、`too frequent` 或 `too many requests` 時，同帳號的登入、雲端查詢及提交共用冷卻。等待時間為 15 → 30 → 60 → 120 分鐘，遵守更長的 `Retry-After`；這是本機退避政策，不是供應商保證的解除時間。網路／5xx 使用 60 秒起的退避，上限 15 分鐘。

狀態保存在 `~/.local/state/pikpak-next/cloud-status.json`，可由 `PIKPAK_CLOUD_STATE` 指定持久化路徑。此路徑沿用已部署桌面版本，讓程式升級與重啟保留原冷卻期限；只保存帳號的 SHA-256 識別及時間，不保存密碼或 token。檔案損壞或不能寫入時，停止雲端請求。Docker 使用者需要將此檔案所在目錄持久化。

登入優先更新 refresh token；SDK 每次僅嘗試一次，並在其處理失敗回應前攔截，避免 SDK 0.1.11 在 refresh 本身遇到 `error_code=16` 時遞迴。只有明確拒絕 refresh 才允許一次密碼登入；限流與 5xx 不會改用密碼連續嘗試。提交遇到逾時、5xx 或不完整回應，保留原有結果不明及來源占用保護，不自動重送或切換其他帳號。

Bot 啟動時自行開啟唯讀健康探測，不依賴桌面程式或 Desktop API。探測每 5 秒檢查本機冷卻；冷卻到期，或最後成功回應超過 5 分鐘，才發出一次有界限的 GET。倒數歸零不代表恢復，必須得到實際成功回應才會變成正常；超過 10 分鐘沒有成功回應則顯示未知。watchdog 在雲端狀態不正常時暫停，且不再自動接手未登記的雲端任務。

## 與 pikpak-next 的分工

- 此 repo 維護 PikPak／aria2 下載引擎、雲端冷卻、重試與任務安全。
- [pikpak-next](https://github.com/fuchengh/pikpak-next) 維護 Windows EXE、Gallery 與 `server/desktop_api.py` adapter。
- Bot 透過 `pikpakbot.desktop` 載入另行安裝在程式根目錄的 `desktop_api.py`；沒有安裝 adapter 時，Discord／Telegram 可獨立運作。已安裝時保留其啟動及桌面任務的 notifier 恢復路徑。
- Desktop API 的 `start_api()` 和雲端探測啟動具有重入保護，與既有桌面安裝器相容。桌面 API 金鑰仍在 `~/.config/pikpak-next/api.token`，不納入 Git。

此次將已在主機驗證的修正整合回正式 Bot 原始碼，取代只在桌面 repo 保存補丁的狀態。**合併此 PR 不會部署或重啟正式服務**，也不需要重發 Windows v0.1.14。

後續部署仍需確認 Bot／aria2 閒置，備份設定、SQLite、冷卻狀態及現有桌面 adapter，保留帳密與下載路徑。主機若有先前安裝器產生的修改，不要直接以 `git reset --hard` 覆蓋；先比對已部署檔案，測試完成後再安排服務更新。不要因冷卻到期而重送結果不明的任務。

測試使用隔離 SQLite、模擬 HTTP 及測試專用 localhost aria2；包括同帳號並行、跨重啟冷卻、SDK refresh 失敗、POST 不重送、13 筆待確認資料／claims 保留，以及有／無桌面 adapter 的啟動和恢復路徑。
