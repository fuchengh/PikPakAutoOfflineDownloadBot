# 部署指南 (Deployment Guide)

本指南將協助您將此 PikPak 下載機器人部署到您的 Media Server。

## 前置準備

1. 安裝 [Docker](https://docs.docker.com/get-docker/) 和 [Docker Compose](https://docs.docker.com/compose/install/)。
2. 準備好您的 Telegram Bot Token 和 PikPak 帳號密碼。

## 部署步驟

### 1. 複製專案 (Clone Repo)

在您的伺服器上執行：

```bash
git clone https://github.com/fuchengh/PikPakAutoOfflineDownloadBot.git
cd PikPakAutoOfflineDownloadBot
```

### 2. 設定配置 (Configuration)

編輯 `config.py` 文件，填入您的個人資訊：

```python
# config.py

TOKEN = '您的_Telegram_Bot_Token'
ADMIN_IDS = ['您的_Telegram_ID']  # 可以透過 @userinfobot 獲取
USER = ["您的PikPak帳號"]
PASSWORD = ["您的PikPak密碼"]

# 以下設定通常不需要更改 (已為 Docker 優化)
ARIA2_HOST = "aria2"
ARIA2_PORT = "6800"
ARIA2_SECRET = "pikpak_secret"
ARIA2_DOWNLOAD_PATH = "/downloads" 
```

### 3. 啟動服務 (Start Services)

執行以下指令啟動 Bot 和 Aria2 下載器：

```bash
docker-compose up -d
```

### 4. 驗證

- **Bot**: 向您的 Telegram Bot 發送 `/help`，看是否回應。
- **Web UI (新)**: 瀏覽器訪問 `http://您的伺服器IP:5000`，這是一個簡易的磁力連結提交與日誌查看儀表板。
- **AriaNg**: 瀏覽器訪問 `http://您的伺服器IP:6880`，這是一個 Aria2 的管理介面，您可以看到即時的下載任務。

## 檔案路徑說明

下載完成的檔案會出現在專案目錄下的 `downloads/` 資料夾中。

如果您想將下載路徑更改為 Media Server 的現有路徑（例如 `/mnt/media/downloads`），請修改 `docker-compose.yml`：

```yaml
# docker-compose.yml

services:
  pikpakbot:
    volumes:
      - /mnt/media/downloads:/downloads  # <--- 修改這裡 (左邊是宿主機路徑)
  
  aria2:
    volumes:
      - /mnt/media/downloads:/downloads  # <--- 修改這裡 (兩者必須一致！)
```

## 常見問題

- **Q: 下載沒速度？**
  A: 檢查防火牆是否允許 Docker 容器連網，或 Aria2 是否正常啟動。
  
- **Q: 機器人沒反應？**
  A: 使用 `docker logs pikpak_bot` 查看錯誤日誌。
