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
        raw_ticker = ticker_str.strip().upper()
        search_tickers = []

        # --- 自動偵測與代碼轉換邏輯 ---
        if raw_ticker.isdigit():
            # 台股邏輯：嘗試 上市(.TW) 與 上櫃(.TWO)
            search_tickers = [f"{raw_ticker}.TW", f"{raw_ticker}.TWO"]
        else:
            # 美股邏輯：直接搜尋
            search_tickers = [raw_ticker]

        info = None
        final_ticker = ""
        
        # 依序嘗試可能的代碼格式
        for t in search_tickers:
            stock = yf.Ticker(t)
            try:
                temp_info = stock.info
                # 檢查是否有抓到關鍵價格數據
                if temp_info and ('currentPrice' in temp_info or 'regularMarketPrice' in temp_info):
                    info = temp_info
                    final_ticker = t
                    break
            except:
                continue

        if not info:
            return None
            
        return {
            "symbol": info.get("symbol", final_ticker).upper(),
            "name": info.get("shortName", ""),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice", 0),
            "future_eps": info.get("forwardEps") or info.get("trailingEps", 0),
            "target_mean": info.get("targetMeanPrice", 0),
            "beta": info.get("beta", 1.0),
            "ebitda": info.get("ebitda", 0),
            "total_debt": info.get("totalDebt", 0),
            "total_cash": info.get("totalCash", 0),
            "shares": info.get("sharesOutstanding", 1),
            "currency": info.get("currency", "USD")
        }
    except Exception as e:
        print(f"YFinance Error: {e}")
        return None

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(executor, get_stock_data_sync, ticker)

    if not data or data["current_price"] == 0:
        raise HTTPException(status_code=404, detail="找不到該股票數據，請確認代碼是否正確 (台股輸入數字, 美股輸入代號)")

    # --- 根據幣別自動調整參數 ---
    is_tw = data["currency"] == "TWD"
    
    # 產業參數對應 (針對市場微調)
    ind_map = {
        '科技': {'pe': 25 if not is_tw else 18, 'growth': 0.04},
        '醫療': {'pe': 22 if not is_tw else 20, 'growth': 0.03},
        '金融': {'pe': 12 if not is_tw else 10, 'growth': 0.02},
        '能源': {'pe': 10 if not is_tw else 8, 'growth': 0.02},
        '消費': {'pe': 18 if not is_tw else 15, 'growth': 0.03},
        '工業': {'pe': 15 if not is_tw else 12, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    # 1. 計算 WACC (台股無風險利率較低)
    rf = 0.015 if is_tw else 0.042
    wacc_val = max(rf + data["beta"] * 0.055, 0.05)
    wacc_str = f"{round(wacc_val * 100, 2)}%"

    # 2. 估值模型
    v_pe = data["future_eps"] * ind['pe'] if data["future_eps"] > 0 else 0
    
    # EV/EBITDA 估值 (若數據缺失則顯示 0)
    if data["ebitda"] > 0 and data["shares"] > 0:
        v_ev = (data["ebitda"] * 12 - data["total_debt"] + data["total_cash"]) / data["shares"]
    else:
        v_ev = 0

    # DCF 估值
    v_dcf = (data["future_eps"] * (1 + ind['growth'])) / (max(wacc_val - ind['growth'], 0.015)) if data["future_eps"] > 0 else 0

    # 分析師目標價
    v_analyst = data["target_mean"]

    # 綜合目標價計算
    valid_vals = [v for v in [v_pe, v_ev, v_dcf, v_analyst] if v > 0]
    final_target = sum(valid_vals) / len(valid_vals) if valid_vals else data["current_price"]

    return {
        "symbol": data["symbol"],
        "name": data.get("name"),
        "current_price": round(data["current_price"], 2),
        "target": round(final_target, 2),
        "analyst_target": round(v_analyst, 2),
        "pe": round(v_pe, 2),
        "ev": round(v_ev, 2),
        "dcf": round(v_dcf, 2),
        "wacc": wacc_str,
        "currency": "NT$" if is_tw else "$",
        "data_source": "Yahoo Finance (Global)"
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")