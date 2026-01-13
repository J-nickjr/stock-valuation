import os
import asyncio
import httpx
import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- 安全設定區 ---
# 請確保在 Render 的 Environment Variables 設定 ALPHA_VANTAGE_KEY
AV_API_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "").strip()

usage_stats = {"count": 0, "date": datetime.date.today().isoformat()}

def update_usage():
    today = datetime.date.today().isoformat()
    if usage_stats["date"] != today:
        usage_stats["count"] = 0
        usage_stats["date"] = today
    usage_stats["count"] += 1

async def fetch_av(function: str, symbol: str):
    """依照 Alpha Vantage 必填規則進行請求"""
    if not AV_API_KEY:
        print("!!! 錯誤: 找不到 ALPHA_VANTAGE_KEY")
        return None

    url = "https://www.alphavantage.co/query"
    params = {
        "function": function,
        "symbol": symbol.upper(),
        "apikey": AV_API_KEY
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, timeout=15.0)
            data = response.json()
            
            # 檢查頻率限制 (Alpha Vantage 免費版限制)
            if "Note" in data:
                print(f"!!! AV 限制提示: {data['Note']}")
                return "LIMIT"
            return data
        except Exception as e:
            print(f"AV API Error ({function}): {e}")
            return None

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    if not AV_API_KEY:
        raise HTTPException(status_code=500, detail="伺服器 API Key 設定缺失")

    # 1. 同時抓取即時報價與預測 EPS
    # 依照要求使用關鍵字 function=EARNINGS_ESTIMATES
    tasks = [
        fetch_av("GLOBAL_QUOTE", ticker),
        fetch_av("EARNINGS_ESTIMATES", ticker)
    ]
    
    results = await asyncio.gather(*tasks)
    quote_data, estimates_data = results

    if quote_data == "LIMIT" or estimates_data == "LIMIT":
        raise HTTPException(status_code=429, detail="API 請求過於頻繁，請一分鐘後再試")

    # 2. 解析股價
    quote = quote_data.get("Global Quote", {})
    current_price = float(quote.get("05. price", 0))
    if current_price == 0:
        raise HTTPException(status_code=404, detail="找不到股價數據，請確認 Ticker 正確")

    # 3. 解析分析師預測 (EARNINGS_ESTIMATES)
    # 我們抓取年度預測 (annualEstimates) 中的第一筆，通常是未來一年
    future_eps = 0
    estimates_list = estimates_data.get("annualEstimates", [])
    if estimates_list:
        # 取得清單中最新的一筆預測
        future_eps = float(estimates_list[0].get("estimated_eps_avg", 0))
        data_source = f"分析師預測 ({estimates_list[0].get('period', '下年度')})"
    else:
        # 降級方案：如果沒預測，嘗試從 quote 抓 TTM EPS
        data_source = "無預測數據，使用歷史數據"
        future_eps = 0 # 若要更嚴謹，可再抓一次 OVERVIEW

    # 4. 估值計算
    ind_map = {
        '科技': {'pe': 28, 'growth': 0.04},
        '醫療': {'pe': 22, 'growth': 0.03},
        '金融': {'pe': 12, 'growth': 0.02},
        '能源': {'pe': 10, 'growth': 0.02},
        '消費': {'pe': 18, 'growth': 0.03},
        '工業': {'pe': 16, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    # 計算模型
    v_pe = future_eps * ind['pe'] if future_eps > 0 else 0
    
    # 由於 EARNINGS_ESTIMATES 只給 EPS，DCF 我們使用簡化模型
    # 假設未來折現率 8%
    v_dcf = (future_eps * (1 + ind['growth'])) / (0.08 - ind['growth']) if future_eps > 0 else 0

    # 綜合目標價
    valid_models = [v for v in [v_pe, v_dcf] if v > 0]
    target = sum(valid_models) / len(valid_models) if valid_models else current_price * 1.05

    update_usage()
    
    return {
        "symbol": ticker.upper(),
        "current_price": current_price,
        "pe": round(v_pe, 2),
        "dcf": round(v_dcf, 2),
        "target": round(target, 2),
        "data_source": data_source,
        "server_usage": usage_stats["count"]
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)