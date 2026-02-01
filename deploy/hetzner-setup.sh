#!/bin/bash
# ===========================================
# CLRI Hetzner EU Deployment Script
# ===========================================
# Run on fresh Ubuntu 22.04 VPS in EU (Frankfurt/Nuremberg)
#
# Prerequisites:
#   - Hetzner CX22 or similar (2 vCPU, 4GB RAM)
#   - Ubuntu 22.04
#   - Root access
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/yourusername/repo/main/crypto/clri/deploy/hetzner-setup.sh | bash
#   OR
#   scp hetzner-setup.sh root@your-vps:/root/ && ssh root@your-vps 'bash hetzner-setup.sh'

set -e

echo "=========================================="
echo "CLRI Hetzner EU Deployment"
echo "=========================================="

# -----------------------------------------
# 1. System Updates
# -----------------------------------------
echo "[1/6] Updating system..."
apt-get update && apt-get upgrade -y

# -----------------------------------------
# 2. Install Docker
# -----------------------------------------
echo "[2/6] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "Docker already installed"
fi

# Install Docker Compose plugin
apt-get install -y docker-compose-plugin

# -----------------------------------------
# 3. Create CLRI user
# -----------------------------------------
echo "[3/6] Creating clri user..."
if ! id "clri" &>/dev/null; then
    useradd -m -s /bin/bash clri
    usermod -aG docker clri
    echo "Created user: clri"
else
    echo "User clri already exists"
fi

# -----------------------------------------
# 4. Setup directories
# -----------------------------------------
echo "[4/6] Setting up directories..."
mkdir -p /opt/clri
chown clri:clri /opt/clri

# -----------------------------------------
# 5. Configure firewall (UFW)
# -----------------------------------------
echo "[5/6] Configuring firewall..."
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 8000/tcp  # CLRI dashboard (optional - can be removed if not using dashboard)
ufw --force enable

# -----------------------------------------
# 6. Create deployment directory structure
# -----------------------------------------
echo "[6/6] Creating deployment structure..."
cat > /opt/clri/docker-compose.yml << 'COMPOSE'
version: '3.8'

services:
  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: clri
      POSTGRES_PASSWORD: ${DB_PASSWORD:-changeme}
      POSTGRES_DB: clri
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U clri"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  worker:
    image: ghcr.io/yourusername/clri-worker:latest
    # OR build locally:
    # build: .
    environment:
      DATABASE_URL: postgresql://clri:${DB_PASSWORD:-changeme}@db:5432/clri
      DISCORD_WEBHOOK_URL: ${DISCORD_WEBHOOK_URL}
      COINGLASS_API_KEY: ${COINGLASS_API_KEY}
      ENABLED_EXCHANGES: ${ENABLED_EXCHANGES:-binanceusdm,bybit,okx}
      TRACKED_SYMBOLS: ${TRACKED_SYMBOLS:-BTC,ETH,SOL,XRP,DOGE}
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  # Optional: Dashboard (comment out if not needed)
  # web:
  #   image: ghcr.io/yourusername/clri-web:latest
  #   ports:
  #     - "8000:8000"
  #   environment:
  #     DATABASE_URL: postgresql://clri:${DB_PASSWORD:-changeme}@db:5432/clri
  #     SESSION_SECRET: ${SESSION_SECRET}
  #     INVITE_CODES: ${INVITE_CODES:-clri2024}
  #   depends_on:
  #     db:
  #       condition: service_healthy
  #   restart: unless-stopped

volumes:
  postgres_data:
COMPOSE

# Create .env template
cat > /opt/clri/.env.template << 'ENVFILE'
# ===========================================
# CLRI Production Configuration
# ===========================================

# Database password (generate: openssl rand -base64 24)
DB_PASSWORD=

# Discord webhook for alerts
DISCORD_WEBHOOK_URL=

# Coinglass API key
COINGLASS_API_KEY=

# Exchanges to monitor
ENABLED_EXCHANGES=binanceusdm,bybit,okx

# Symbols to track
TRACKED_SYMBOLS=BTC,ETH,SOL,XRP,DOGE,AVAX,LINK

# Dashboard settings (if using web service)
SESSION_SECRET=
INVITE_CODES=clri2024
ENVFILE

chown -R clri:clri /opt/clri

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Create .env file:"
echo "   cp /opt/clri/.env.template /opt/clri/.env"
echo "   nano /opt/clri/.env"
echo ""
echo "2. Add your credentials:"
echo "   - DB_PASSWORD (generate: openssl rand -base64 24)"
echo "   - DISCORD_WEBHOOK_URL"
echo "   - COINGLASS_API_KEY"
echo ""
echo "3. Deploy the code (choose one):"
echo "   a) Clone repo: git clone YOUR_REPO /opt/clri/app"
echo "   b) SCP files: scp -r crypto/clri/* clri@VPS:/opt/clri/app/"
echo ""
echo "4. Build and start:"
echo "   cd /opt/clri"
echo "   docker compose up -d"
echo ""
echo "5. Check logs:"
echo "   docker compose logs -f worker"
echo ""
echo "=========================================="
