import hashlib, hmac, os, time, urllib.parse
from typing import Optional, Literal
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()
SPOT = "https://api.binance.com"
FUT = "https://fapi.binance.com"

def key(): return os.environ.get("BINANCE_API_KEY", "")
def secret(): return os.environ.get("BINANCE_API_SECRET", "").encode()

def auth(tok):
    exp = os.environ.get("RELAY_TOKEN", "")
    if not exp or not tok or not hmac.compare_digest(tok, exp):
        raise HTTPException(401, "unauthorized")

async def signed(c, method, base, path, params=None):
    p = dict(params or {}); p["timestamp"] = int(time.time() * 1000); p["recvWindow"] = 10000
    q = urllib.parse.urlencode(p)
    q += "&signature=" + hmac.new(secret(), q.encode(), hashlib.sha256).hexdigest()
    r = await c.request(method, f"{base}{path}?{q}", headers={"X-MBX-APIKEY": key()})
    try: body = r.json()
    except Exception: body = r.text[:300]
    return r.status_code, body

@app.get("/")
async def root(): return {"ok": True}

@app.get("/probe")
async def probe():
    out = {}
    async with httpx.AsyncClient(timeout=15) as c:
        for n, u in [("spot", SPOT + "/api/v3/time"), ("futures", FUT + "/fapi/v1/time")]:
            try: out[n] = (await c.get(u)).status_code
            except Exception as e: out[n] = f"error: {type(e).__name__}"
        try: out["ip"] = (await c.get("https://api.ipify.org")).text
        except Exception as e: out["ip"] = f"error: {type(e).__name__}"
    return out

@app.get("/summary")
async def summary(x_relay_token: Optional[str] = Header(None), symbols: str = ""):
    auth(x_relay_token)
    res = {}
    async with httpx.AsyncClient(timeout=20) as c:
        s, b = await signed(c, "GET", SPOT, "/api/v3/account", {"omitZeroBalances": "true"})
        res["spot"] = {"status": s, "balances": [x for x in b.get("balances", []) if float(x["free"]) + float(x["locked"]) > 0]} if s == 200 else {"status": s, "error": b}
        s, b = await signed(c, "GET", FUT, "/fapi/v2/account")
        res["futures_account"] = {"status": s, **({k: b.get(k) for k in ("totalWalletBalance", "totalUnrealizedProfit", "availableBalance")} if s == 200 else {"error": b})}
        s, b = await signed(c, "GET", FUT, "/fapi/v2/positionRisk")
        if s == 200:
            res["positions"] = [{"symbol": p["symbol"], "qty": p["positionAmt"], "entry": p["entryPrice"], "mark": p["markPrice"], "uPnL": p["unRealizedProfit"], "side": p.get("positionSide")} for p in b if float(p["positionAmt"]) != 0]
        else: res["positions"] = {"status": s, "error": b}
        syms = [x for x in symbols.split(",") if x] or [p["symbol"] for p in res["positions"]] if isinstance(res["positions"], list) else []
        trades = {}
        for sym in syms[:10]:
            s, b = await signed(c, "GET", FUT, "/fapi/v1/userTrades", {"symbol": sym, "limit": 10})
            trades[sym] = b if s == 200 else {"status": s, "error": b}
        res["futures_last_trades"] = trades
    return res

class Order(BaseModel):
    market: Literal["spot", "futures"]
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"] = "MARKET"
    quantity: Optional[str] = None
    quoteOrderQty: Optional[str] = None
    stopPrice: Optional[str] = None
    reduceOnly: bool = False
    closePosition: bool = False

class Transfer(BaseModel):
    asset: str = "USDT"
    amount: str

@app.post("/order")
async def order(o: Order, x_relay_token: Optional[str] = Header(None)):
    auth(x_relay_token)
    async with httpx.AsyncClient(timeout=20) as c:
        if o.market == "spot":
            if o.type != "MARKET": raise HTTPException(400, "spot supports MARKET only")
            p = {"symbol": o.symbol, "side": o.side, "type": "MARKET"}
            if o.quantity: p["quantity"] = o.quantity
            elif o.quoteOrderQty: p["quoteOrderQty"] = o.quoteOrderQty
            else: raise HTTPException(400, "quantity or quoteOrderQty required")
            s, b = await signed(c, "POST", SPOT, "/api/v3/order", p)
        else:
            p = {"symbol": o.symbol, "side": o.side, "type": o.type}
            if o.type != "MARKET":
                if not o.stopPrice: raise HTTPException(400, "stopPrice required")
                p["stopPrice"] = o.stopPrice; p["workingType"] = "MARK_PRICE"
            if o.closePosition and o.type != "MARKET": p["closePosition"] = "true"
            else:
                if not o.quantity: raise HTTPException(400, "quantity required")
                p["quantity"] = o.quantity
                if o.reduceOnly: p["reduceOnly"] = "true"
            s, b = await signed(c, "POST", FUT, "/fapi/v1/order", p)
    return {"status": s, "result": b}

@app.post("/transfer")
async def transfer(t: Transfer, x_relay_token: Optional[str] = Header(None)):
    auth(x_relay_token)
    async with httpx.AsyncClient(timeout=20) as c:
        s, b = await signed(c, "POST", SPOT, "/sapi/v1/asset/transfer", {"type": "MAIN_UMFUTURE", "asset": t.asset, "amount": t.amount})
    return {"status": s, "result": b}
