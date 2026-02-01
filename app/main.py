"""
CLRI Web Application

FastAPI-based dashboard and API for:
- Viewing current CLRI scores
- Historical charts
- User authentication
- Alert configuration
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import bcrypt

from core.database import (
    Database, User, Session, CLRIReading, Alert, MarketSummary,
    get_latest_readings, get_recent_alerts, get_readings_for_timerange,
    RiskLevel, Direction,
)


# Configuration
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://localhost/clri')
SESSION_SECRET = os.getenv('SESSION_SECRET', secrets.token_urlsafe(32))
SESSION_EXPIRY_HOURS = 24 * 7  # 1 week

# Database instance (initialized on startup)
db: Optional[Database] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    global db
    
    # Startup
    db = Database(DATABASE_URL)
    await db.create_tables()
    
    yield
    
    # Shutdown
    if db:
        await db.close()


# Create FastAPI app
app = FastAPI(
    title="CLRI Dashboard",
    description="Crypto Leverage Risk Index - Real-time leverage monitoring",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for API access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates
templates = Jinja2Templates(directory="templates")


# ============== Authentication ==============

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    invite_code: str  # Simple invite system


# Invite codes (in production, store in DB)
VALID_INVITE_CODES = os.getenv('INVITE_CODES', 'clri2024,leveragerisk').split(',')


def hash_password(password: str) -> str:
    """Hash a password"""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(password.encode(), password_hash.encode())


async def get_current_user(request: Request) -> Optional[User]:
    """Get current authenticated user from session cookie"""
    token = request.cookies.get('session_token')
    
    if not token:
        return None
    
    async with db.session() as session:
        from sqlalchemy import select
        
        result = await session.execute(
            select(Session)
            .where(Session.token == token)
            .where(Session.expires_at > datetime.now(timezone.utc))
        )
        sess = result.scalar_one_or_none()
        
        if not sess:
            return None
        
        result = await session.execute(
            select(User).where(User.id == sess.user_id)
        )
        return result.scalar_one_or_none()


async def require_auth(request: Request) -> User:
    """Dependency that requires authentication"""
    user = await get_current_user(request)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    
    return user


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    """Register a new user"""
    if req.invite_code not in VALID_INVITE_CODES:
        raise HTTPException(400, "Invalid invite code")
    
    async with db.session() as session:
        from sqlalchemy import select
        
        # Check if username exists
        result = await session.execute(
            select(User).where(User.username == req.username)
        )
        if result.scalar_one_or_none():
            raise HTTPException(400, "Username already exists")
        
        # Create user
        user = User(
            username=req.username,
            password_hash=hash_password(req.password),
        )
        session.add(user)
        await session.commit()
        
        return {"message": "User created successfully"}


@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response):
    """Login and create session"""
    async with db.session() as session:
        from sqlalchemy import select
        
        result = await session.execute(
            select(User).where(User.username == req.username)
        )
        user = result.scalar_one_or_none()
        
        if not user or not verify_password(req.password, user.password_hash):
            raise HTTPException(401, "Invalid credentials")
        
        if not user.is_active:
            raise HTTPException(403, "Account is disabled")
        
        # Create session
        token = secrets.token_urlsafe(32)
        sess = Session(
            token=token,
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=SESSION_EXPIRY_HOURS),
        )
        session.add(sess)
        
        # Update last login
        user.last_login = datetime.now(timezone.utc)
        
        await session.commit()
        
        # Set cookie
        response.set_cookie(
            key='session_token',
            value=token,
            httponly=True,
            secure=True,  # Set False for local dev
            samesite='lax',
            max_age=SESSION_EXPIRY_HOURS * 3600,
        )
        
        return {"message": "Login successful", "username": user.username}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    """Logout and destroy session"""
    token = request.cookies.get('session_token')
    
    if token:
        async with db.session() as session:
            from sqlalchemy import delete
            await session.execute(
                delete(Session).where(Session.token == token)
            )
            await session.commit()
    
    response.delete_cookie('session_token')
    return {"message": "Logged out"}


@app.get("/api/auth/me")
async def get_me(user: User = Depends(require_auth)):
    """Get current user info"""
    return {
        "username": user.username,
        "created_at": user.created_at.isoformat(),
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


# ============== CLRI API ==============

@app.get("/api/clri/current")
async def get_current_clri(user: User = Depends(require_auth)):
    """Get current CLRI readings for all symbols"""
    async with db.session() as session:
        from sqlalchemy import select, func, distinct
        
        # Get latest reading for each symbol
        subq = (
            select(
                CLRIReading.symbol,
                func.max(CLRIReading.timestamp).label('max_ts')
            )
            .group_by(CLRIReading.symbol)
            .subquery()
        )
        
        result = await session.execute(
            select(CLRIReading)
            .join(
                subq,
                (CLRIReading.symbol == subq.c.symbol) &
                (CLRIReading.timestamp == subq.c.max_ts)
            )
        )
        readings = result.scalars().all()
        
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "readings": [
                {
                    "symbol": r.symbol,
                    "clri_score": r.clri_score,
                    "risk_level": r.risk_level.value,
                    "direction": r.direction.value,
                    "funding_rate": r.funding_rate,
                    "open_interest": r.open_interest,
                    "price": r.price,
                    "component_scores": {
                        "funding": r.funding_score,
                        "oi": r.oi_score,
                        "volume": r.volume_score,
                        "liquidation": r.liquidation_score,
                        "imbalance": r.imbalance_score,
                    },
                    "updated_at": r.timestamp.isoformat(),
                }
                for r in readings
            ]
        }


@app.get("/api/clri/market")
async def get_market_summary(user: User = Depends(require_auth)):
    """Get market-wide CLRI summary"""
    async with db.session() as session:
        from sqlalchemy import select
        
        result = await session.execute(
            select(MarketSummary)
            .order_by(MarketSummary.timestamp.desc())
            .limit(1)
        )
        summary = result.scalar_one_or_none()
        
        if not summary:
            return {"market_clri": 0, "risk_level": "LOW"}
        
        return {
            "timestamp": summary.timestamp.isoformat(),
            "market_clri": summary.market_clri,
            "risk_level": summary.risk_level.value,
            "market_direction": summary.market_direction,
            "symbols_tracked": summary.symbols_tracked,
            "symbols_elevated": summary.symbols_elevated,
            "symbols_high": summary.symbols_high,
            "individual_readings": summary.individual_readings,
        }


@app.get("/api/clri/history/{symbol}")
async def get_symbol_history(
    symbol: str,
    hours: int = 24,
    user: User = Depends(require_auth),
):
    """Get CLRI history for a symbol"""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    
    async with db.session() as session:
        readings = await get_readings_for_timerange(
            session, symbol.upper(), start_time, end_time
        )
        
        return {
            "symbol": symbol.upper(),
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "data_points": len(readings),
            "history": [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "clri_score": r.clri_score,
                    "risk_level": r.risk_level.value,
                    "direction": r.direction.value,
                    "price": r.price,
                    "funding_rate": r.funding_rate,
                    "open_interest": r.open_interest,
                }
                for r in readings
            ]
        }


@app.get("/api/alerts")
async def get_alerts(
    limit: int = 50,
    symbol: Optional[str] = None,
    user: User = Depends(require_auth),
):
    """Get recent alerts"""
    async with db.session() as session:
        alerts = await get_recent_alerts(session, limit, symbol)
        
        return {
            "alerts": [
                {
                    "id": a.id,
                    "triggered_at": a.triggered_at.isoformat(),
                    "symbol": a.symbol,
                    "alert_type": a.alert_type,
                    "clri_score": a.clri_score,
                    "risk_level": a.risk_level.value if a.risk_level else None,
                    "direction": a.direction.value if a.direction else None,
                    "message": a.message,
                }
                for a in alerts
            ]
        }


# ============== Dashboard Pages ==============

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard page"""
    user = await get_current_user(request)
    
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user}
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    user = await get_current_user(request)
    
    if user:
        return RedirectResponse("/", status_code=302)
    
    return templates.TemplateResponse(
        "login.html",
        {"request": request}
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Registration page"""
    return templates.TemplateResponse(
        "register.html",
        {"request": request}
    )


# Health check for Railway
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# Mount static files
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except RuntimeError:
    pass  # Static directory may not exist yet


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
