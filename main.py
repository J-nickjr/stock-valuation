import os
import asyncio
import datetime
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()

# 建立 ThreadPool 處理同步的 yfinance 請求
executor = ThreadPoolExecutor(max_workers=5)

usage_stats = {"count": 0, "date": datetime.date.today().isoformat()}

def update_usage():
    today = datetime.date.today().isoformat()
    if usage_stats["date"] != today:
        usage_stats["count"] = 0
        usage_stats["date"] = today
    usage_stats["count"] += 1

def get_stock_data_sync(ticker_str: str):
    """同步爬取 Yahoo Finance 數據"""
    try:
        stock = yf.Ticker(ticker_str)
        info = stock.info
        
        # 抓取關鍵估值指標
        return {
            "symbol": info.get("symbol", ticker_str).upper(),
            "name": info.get("shortName", "未知公司"),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "future_eps": info.get("forwardEps") or info.get("trailingEps"),
            "target_price": info.get("targetMeanPrice"),
            "beta": info.get("beta", 1.0),
            "forward_pe": info.get("forwardPE")
        }
    except Exception as e:
        print(f"YFinance Error: {e}")
        return None

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    loop = asyncio.get_event_loop()
    # 將同步的 yfinance 丟進 ThreadPool 跑
    data = await loop.run_in_executor(executor, get_stock_data_sync, ticker)

    if not data or not data["current_price"]:
        raise HTTPException(status_code=404, detail="無法獲取數據，請確認股票代號是否正確 (例如 AAPL)")

    # 產業估值參數
    ind_map = {
        '科技': {'pe': 28, 'growth': 0.04},
        '醫療': {'pe': 22, 'growth': 0.03},
        '金融': {'pe': 12, 'growth': 0.02},
        '能源': {'pe': 10, 'growth': 0.02},
        '消費': {'pe': 18, 'growth': 0.03},
        '工業': {'pe': 16, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    price = data["current_price"]
    eps = data["future_eps"] or 0
    analyst_target = data["target_price"] or 0

    # 1. 產業本益比估值
    v_pe = eps * ind['pe'] if eps > 0 else 0
    
    # 2. 簡化版 DCF (根據 Beta 調整折現率)
    rf = 0.042
    wacc = max(rf + data["beta"] * 0.055, 0.06)
    v_dcf = (eps * (1 + ind['growth'])) / (max(wacc - ind['growth'], 0.02)) if eps > 0 else 0

    # 3. 綜合目標價 (分析師+PE+DCF 權重平均)
    valid_vals = [v for v in [v_pe, analyst_target, v_dcf] if v > 0]
    final_target = sum(valid_vals) / len(valid_vals) if valid_vals else price * 1.1

    update_usage()

    return {
        "symbol": data["symbol"],
        "name": data["name"],
        "current_price": round(price, 2),
        "pe_valuation": round(v_pe, 2),
        "analyst_target": round(analyst_target, 2),
        "dcf_valuation": round(v_dcf, 2),
        "target": round(final_target, 2),
        "data_source": "Yahoo Finance (爬蟲免Key版)",
        "server_usage": usage_stats["count"]
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)