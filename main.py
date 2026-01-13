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
        
        # yfinance 的數據抽取
        return {
            "symbol": info.get("symbol", ticker_str).upper(),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice", 0),
            "future_eps": info.get("forwardEps") or info.get("trailingEps", 0),
            "target_mean": info.get("targetMeanPrice", 0),
            "beta": info.get("beta", 1.0),
            # EV/EBITDA 需要的參數
            "enterprise_value": info.get("enterpriseValue", 0),
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

    if not data or data["current_price"] == 0:
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

    price = data["current_price"]
    eps = data["future_eps"]
    ebitda = data["ebitda"]

    # 1. P/E 估值 (對應前端欄位 pe)
    v_pe = eps * ind['pe'] if eps > 0 else 0
    
    # 2. EV/EBITDA 估值 (對應前端欄位 ev)
    # 公式: (EBITDA * 產業倍數 - 負債 + 現金) / 總股數
    if ebitda > 0 and data["shares"] > 0:
        v_ev = (ebitda * ind['evebitda'] - data["total_debt"] + data["total_cash"]) / data["shares"]
    else:
        v_ev = 0

    # 3. DCF 估值 (對應前端欄位 dcf)
    rf = 0.042
    wacc = max(rf + data["beta"] * 0.055, 0.06)
    v_dcf = (eps * (1 + ind['growth'])) / (max(wacc - ind['growth'], 0.02)) if eps > 0 else 0

    # 4. 分析師預期 (對應前端原本可能預期的欄位，這裡取 target_mean)
    v_analyst = data["target_mean"]

    # 綜合目標價計算 (對應前端欄位 target)
    valid_vals = [v for v in [v_pe, v_ev, v_dcf, v_analyst] if v > 0]
    final_target = sum(valid_vals) / len(valid_vals) if valid_vals else price * 1.1

    return {
        "symbol": data["symbol"],
        "current_price": round(price, 2),
        "pe": round(v_pe, 2),        # 前端預期的 Key
        "ev": round(v_ev, 2),        # 前端預期的 Key
        "dcf": round(v_dcf, 2),      # 前端預期的 Key
        "analyst": round(v_analyst, 2), # 新增分析師欄位
        "target": round(final_target, 2), # 前端預期的 Key
        "data_source": "Yahoo Finance"
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")