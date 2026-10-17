# 下載、重試與恢復

## 使用方式

- `/p` 可以接收以空白、換行、逗號分隔，或直接黏在一起的 magnet。相同 infohash 只提交一次；保留 tracker 參數，並支援 HTTP(S)、ed2k 連結。無效 magnet 會在提交前報錯。
- 失敗訊息的「重試」會建立一筆有父子關係的新嘗試，保留原本歷史。重複點擊舊按鈕不會建立第二個 worker；尚在收尾時會提示稍後再試。
- Discord 重試沿用原 thread 與進度訊息；需要時解除封存。完成訊息送出後才封存；失敗 thread 保持開啟。
- Discord `/retry-list`（Telegram `/retry list`）列出 ERROR 任務與已確認停滯的任務。ERROR 不受進度門檻限制；門檻只篩選停滯中的 RUNNING/PENDING 任務，`0` 是有效值。
- `/retry` 對已有 worker 的離線任務送出重試要求，由同一個 worker 處理；不依檔名刪除或重建任務。重複要求會合併，至少間隔 60 秒。
- PikPak 明確回報 ERROR 時立即顯示錯誤碼與訊息，不再等到監控超時。查詢失敗會顯示為查詢錯誤，不會當成「空列表」或「任務被刪除」。若提交請求結果不明，先在 PikPak 離線列表確認，再選擇 `/retry` 或 `/dl` 接續，避免重新提交 magnet。

## 資料保護與重啟

SQLite `state.db` 會新增欄位與表格，保留既有記錄。新任務儲存來源、PikPak task/file ID、每個檔案的 aria2 GID，以及 Discord thread/message ID。

資料夾會先完整列舉檔案，再加入 aria2。任何分頁、檔案資訊或提交失敗，都會保留雲端資料。部分成功後重試，只補上尚未成功的檔案；已完成的檔案會重新檢查本機大小和 `.aria2` 控制檔。

只有全部檔案都成功，且本機檔案符合 aria2 完成資訊時，才依 `AUTO_DELETE` 清理已驗證的檔案 ID。**不刪除父資料夾**，避免刪到稍後新增或別的任務使用的內容；空資料夾因此可能保留。成功下載但雲端清理失敗時，完成訊息會提醒檢查剩餘雲端內容。

Bot 和 aria2 必須能以同一個 `ARIA2_DOWNLOAD_PATH` 存取相同下載資料；現有 Docker Compose 已使用相同掛載。遠端 aria2 若沒有共享相同路徑，驗證會失敗並保留雲端資料。檔案大小驗證不是內容雜湊驗證。

aria2 RPC 暫時失聯時，保留 GID 與目的路徑的占用記錄。之後按原任務的重試按鈕，會先查詢原 GID 並接續；不會另開工作覆寫同一路徑。忽略 Discord 訊息不會取消 aria2 或解除這些記錄。

重啟只恢復先前中斷且有可靠 ID 的任務，以及確定尚未提交的排隊任務。舊版缺少 ID 的進行中記錄不會猜測來源後自動重跑，也不會掃描全部歷史雲端完成項目重新下載；請先確認 aria2 / PikPak 現況，再手動接續。

## 設定與效能

新增設定缺省時使用以下預設值：

| 設定 | 預設值 | 作用 |
| --- | --- | --- |
| `DISCORD_ADMIN_IDS` | `[]` | 可使用 slash command / 按鈕的 Discord 使用者 ID；空列表只允許 application owner / team owner 權限使用者 |
| `DOWNLOAD_WORKERS` | `4` | 同時處理的來源任務數；每個來源可能包含多個 aria2 檔案 |
| `MAX_QUEUED_TASKS` | `128` | 執行中與等待中的來源任務總上限 |
| `OFFLINE_STALL_SECONDS` | `1200` | 離線進度持續不變多久才視為停滯 |
| `OFFLINE_MAX_SECONDS` | `7200` | 每次離線監控最長時間；超時保留雲端任務 |
| `ARIA2_STALL_SECONDS` | `3600` | 本機下載完全沒有位元組進展的最長等待時間 |
| `MAX_AUTO_RETRIES` | `3` | 每次嘗試允許的離線自動重試次數；重啟恢復沿用計數 |

自動重試只處理確認停滯的 RUNNING/PENDING 任務；已終止的失敗需手動重試。新出現的 99% 不會立刻被 watchdog 重試。外部任務的停滯判定需持續成功觀察；查詢間斷超過 10 分鐘會重新計時。

效能調整包括每個帳號共享 5 秒的完整離線狀態快照、合併同時查詢、每 100 個 GID 一批的 aria2 `system.multicall`、每個執行緒重用 HTTP 連線，以及完成後移除 worker 記錄。Discord 更新使用已知 message ID，不再為每次更新先抓訊息。

PikPak 登入快取改用帳號鍵值，避免新增或刪除帳號後對錯 client；有下載任務時拒絕新增或刪除帳號。`record_config()` 使用 Python literal 與原子替換，保留設定檔中的函式。

## 測試與之後的更新

支援 Python 3.11 / 3.12；保留現有 Telegram 13.12，並鎖定其相容依賴。Python 3.13 以上不在這次相容範圍。

```sh
python -m venv .venv
# 啟用虛擬環境後
python -m pip install -r requirements-test.txt
python -m pytest -q
ruff check . --select F,E9
python -m compileall -q main.py config.py pikpakbot
```

測試使用暫存 SQLite、隔離下載目錄和模擬的 Discord/PikPak/aria2；禁止對這些服務發送真實網路請求。GitHub Actions 在 Python 3.11 與 3.12 執行相同檢查，沒有部署步驟。這些測試驗證本機邏輯，不能保證 PikPak 上游對特定資源的下載成功率。

之後要更新正式服務時，先等目前下載完成，備份程式版本、`config.py`、`state.db`（若使用 WAL，應使用 SQLite backup 或在服務停止後複製完整 DB），以及 aria2 設定和 session。保留現有帳密並合併新設定，確認共享下載路徑後再安排重啟。此 PR 本身不執行部署、服務重啟或生產資料移轉。
