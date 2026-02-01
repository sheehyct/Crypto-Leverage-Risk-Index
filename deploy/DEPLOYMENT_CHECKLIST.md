# CLRI Hetzner EU Deployment Checklist

## Pre-Deployment (Local)

### Credentials Ready
- [ ] Coinglass API key (from https://www.coinglass.com/pricing)
- [ ] Discord webhook URL configured
- [ ] Generate DB password: `openssl rand -base64 24`

### Test Locally (Optional)
```bash
cd clri
python core/liquidations.py  # Test Coinglass API
```

---

## VPS Setup

### 1. Create Hetzner VPS
- [ ] Go to https://console.hetzner.cloud
- [ ] Create new project: "CLRI"
- [ ] Add server:
  - Location: **Falkenstein (eu-central)** or **Nuremberg**
  - Image: Ubuntu 22.04
  - Type: CX22 (2 vCPU, 4GB RAM)
  - SSH Key: Add your public key
  - Name: clri-eu

### 2. Initial Server Setup
```bash
# SSH into server
ssh root@YOUR_VPS_IP

# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker

# Install Docker Compose plugin
apt install -y docker-compose-plugin

# Create clri user
useradd -m -s /bin/bash clri
usermod -aG docker clri

# Setup directories
mkdir -p /opt/clri
chown clri:clri /opt/clri
```

### 3. Configure Firewall
```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
# ufw allow 8000/tcp  # Only if using dashboard
ufw --force enable
```

### 4. Deploy Code
```bash
# Option A: SCP from local machine
scp -r clri/* root@YOUR_VPS_IP:/opt/clri/

# Option B: Git clone (if repo is on GitHub)
# git clone YOUR_REPO /opt/clri
```

### 5. Create Production .env
```bash
# On VPS
cd /opt/clri/deploy
cat > .env << 'EOF'
# Database
DB_PASSWORD=YOUR_GENERATED_PASSWORD

# Alerts
DISCORD_WEBHOOK_URL=YOUR_WEBHOOK_URL

# Coinglass
COINGLASS_API_KEY=YOUR_COINGLASS_KEY

# Exchanges
ENABLED_EXCHANGES=binanceusdm,bybit,okx
TRACKED_SYMBOLS=BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA

# Optional: Discord cleanup bot
# DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
# DISCORD_CHANNEL_ID=YOUR_CHANNEL_ID

# Optional: Dashboard
# SESSION_SECRET=generate-random-string
# INVITE_CODES=clri2024
EOF

# Secure the file
chmod 600 .env
```

### 6. Build and Start
```bash
cd /opt/clri/deploy

# Build and start services
docker compose -f docker-compose.prod.yml up -d

# View logs
docker compose -f docker-compose.prod.yml logs -f worker
```

---

## Verification

### Check Services Running
```bash
docker compose -f docker-compose.prod.yml ps
```

Expected output:
```
NAME              STATUS          PORTS
deploy-db-1       Up (healthy)
deploy-worker-1   Up
```

### Check Worker Logs
```bash
docker compose -f docker-compose.prod.yml logs -f worker
```

Look for:
- "Starting multi-exchange collector"
- "Initialized binanceusdm with X markets"
- "Starting liquidation collector"
- "Discord cleanup bot initialized" (if configured)
- CLRI scores being calculated

### Verify Discord Alerts
- [ ] Check Discord channel for startup message
- [ ] Wait for first CLRI calculation (within 1-2 min)

---

## Maintenance Commands

### View logs
```bash
docker compose -f docker-compose.prod.yml logs -f worker
```

### Restart services
```bash
docker compose -f docker-compose.prod.yml restart worker
```

### Rebuild after code changes
```bash
docker compose -f docker-compose.prod.yml up -d --build worker
```

### Stop all
```bash
docker compose -f docker-compose.prod.yml down
```

### Update code
```bash
cd /opt/clri

# Pull new code (if using git)
git pull

# Or SCP new files
# scp -r local/clri/* root@VPS_IP:/opt/clri/

# Rebuild and restart
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

### View database
```bash
docker compose -f docker-compose.prod.yml exec db psql -U clri -d clri
```

---

## Troubleshooting

### Worker not connecting to exchanges
```bash
# Check network from container
docker compose -f docker-compose.prod.yml exec worker curl -I https://fapi.binance.com
```

### Database connection issues
```bash
# Check db is healthy
docker compose -f docker-compose.prod.yml ps db

# Check logs
docker compose -f docker-compose.prod.yml logs db
```

### No Discord alerts
1. Verify webhook URL in .env
2. Check worker logs for errors
3. Test webhook manually:
```bash
curl -X POST YOUR_WEBHOOK_URL \
  -H "Content-Type: application/json" \
  -d '{"content":"Test"}'
```

---

## Architecture

```
+-----------------------------------------------------------+
|                    Hetzner EU VPS                         |
|  +-------------+  +-------------+  +-------------+        |
|  |  PostgreSQL |  |   Worker    |  |  Dashboard  |        |
|  |   (db)      |<-|  (clri)     |  |   (web)     |        |
|  |             |  |             |  |  [optional] |        |
|  +-------------+  +------+------+  +-------------+        |
|                          |                                |
+--------------------------|--------------------------------+
                           |
         +-----------------+-----------------+
         v                 v                 v
   +----------+     +----------+     +----------+
   | Binance  |     | Coinglass|     | Discord  |
   | Bybit    |     |   API    |     | Webhook  |
   | OKX      |     |          |     |          |
   +----------+     +----------+     +----------+
   Exchange Data    Liquidations      Alerts
```
