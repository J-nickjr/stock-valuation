import os
import asyncio
import datetime
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
executor = ThreadPoolExecutor(max_workers=5)

def get_stock_data_sync(ticker_str: str):
    try:
        stock = yf.Ticker(ticker_str)
        info = stock.info
        if not info or 'currentPrice' not in info and 'regularMarketPrice' not in info:
            return None
            
        return {
            "symbol": info.get("symbol", ticker_str).upper(),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice", 0),
            "future_eps": info.get("forwardEps") or info.get("trailingEps", 0),
            "target_mean": info.get("targetMeanPrice", 0),
            "beta": info.get("beta", 1.0),
            "ebitda": info.get("ebitda", 0),
            "total_debt": info.get("totalDebt", 0),
            "total_cash": info.get("totalCash", 0),
            "shares": info.get("sharesOutstanding", 1)
        }
    except Exception as e:
        print(f"YFinance Error: {e}")
        return None

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(executor, get_stock_data_sync, ticker)

    if not data:
        raise HTTPException(status_code=404, detail="無法獲取數據")

    # 產業參數
    ind_map = {
        '科技': {'pe': 28, 'evebitda': 16, 'growth': 0.04},
        '醫療': {'pe': 22, 'evebitda': 14, 'growth': 0.03},
        '金融': {'pe': 12, 'evebitda': 10, 'growth': 0.02},
        '能源': {'pe': 10, 'evebitda': 8, 'growth': 0.02},
        '消費': {'pe': 18, 'evebitda': 12, 'growth': 0.03},
        '工業': {'pe': 16, 'evebitda': 11, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    # 1. 計算 WACC (前端有用到 result.wacc)
    rf = 0.042
    wacc_val = max(rf + data["beta"] * 0.055, 0.06)
    wacc_str = f"{round(wacc_val * 100, 2)}%"

    # 2. P/E 估值 (對應前端 result.pe)
    v_pe = data["future_eps"] * ind['pe'] if data["future_eps"] > 0 else 0
    
    # 3. EV/EBITDA 估值 (對應前端 result.ev)
    if data["ebitda"] > 0 and data["shares"] > 0:
        v_ev = (data["ebitda"] * ind['evebitda'] - data["total_debt"] + data["total_cash"]) / data["shares"]
    else:
        v_ev = 0

    # 4. DCF 估值 (對應前端 result.dcf)
    v_dcf = (data["future_eps"] * (1 + ind['growth'])) / (max(wacc_val - ind['growth'], 0.02)) if data["future_eps"] > 0 else 0

    # 5. 分析師目標價 (對應前端 result.analyst_target)
    v_analyst = data["target_mean"] if data["target_mean"] > 0 else data["current_price"] * 1.1

    # 綜合目標價 (對應前端 result.target)
    valid_vals = [v for v in [v_pe, v_ev, v_dcf, v_analyst] if v > 0]
    final_target = sum(valid_vals) / len(valid_vals) if valid_vals else data["current_price"]

    # --- 重要：回傳的 Key 必須完全對齊 HTML ---
    return {
        "symbol": data["symbol"],
        "current_price": round(data["current_price"], 2),
        "target": round(final_target, 2),
        "analyst_target": round(v_analyst, 2),
        "pe": round(v_pe, 2),
        "ev": round(v_ev, 2),
        "dcf": round(v_dcf, 2),
        "wacc": wacc_str,
        "data_source": "Yahoo Finance"
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")