import os
import asyncio
import httpx
import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- 安全設定區 ---
# 從系統讀取 FMP_API_KEY，如果開發時沒設定，才會用到後面的備用值
# 部署到 Render 時，請在 Environment Variables 加入 FMP_API_KEY
FMP_API_KEY = os.environ.get("FMP_API_KEY", "ThjXlqf9hWEiYHU9Ccms51LqZuVBm1qj")
BASE_URL = "https://financialmodelingprep.com/api/v3"

# 全域計數器 (儲存在伺服器記憶體中)
usage_stats = {"count": 0, "date": datetime.date.today().isoformat()}

def update_usage():
    today = datetime.date.today().isoformat()
    if usage_stats["date"] != today:
        usage_stats["count"] = 0
        usage_stats["date"] = today
    usage_stats["count"] += 1

async def fetch_fmp(endpoint: str, symbol: str, params: dict = None):
    """標準化抓取函式，將 Key 隔離在網址之外"""
    query_params = {"apikey": FMP_API_KEY}
    if params:
        query_params.update(params)
    
    url = f"{BASE_URL}/{endpoint}/{symbol.upper()}"
    
    async with httpx.AsyncClient() as client:
        try:
            # 這裡就是程式自動組合網址的地方
            response = await client.get(url, params=query_params, timeout=10.0)
            data = response.json()
            
            if isinstance(data, dict) and "Error Message" in data:
                if "limit" in data["Error Message"].lower():
                    raise HTTPException(status_code=403, detail="API 次數已達今日上限")
                raise HTTPException(status_code=400, detail=data["Error Message"])
            return data
        except Exception as e:
            if isinstance(e, HTTPException): raise e
            return None

@app.get("/api/usage")
async def get_usage():
    return {"usage": usage_stats["count"]}

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    # 限制全站總量
    if usage_stats["count"] >= 250:
        raise HTTPException(status_code=403, detail="全站額度已用完，請明天再試")

    # 1. 抓取所需的所有前瞻與基礎數據
    # analyst-estimates: 您提供的未來預期數據
    # quote: 發行股數與當前股價
    # balance-sheet-statement: 負債與現金
    tasks = [
        fetch_fmp("analyst-estimates", ticker, {"period": "annual", "limit": 1}),
        fetch_fmp("quote", ticker),
        fetch_fmp("balance-sheet-statement", ticker, {"period": "annual", "limit": 1}),
        fetch_fmp("profile", ticker)
    ]
    
    results = await asyncio.gather(*tasks)
    est_list, quote_list, bal_list, prof_list = results

    if not est_list or not quote_list:
        raise HTTPException(status_code=404, detail="找不到該股票的分析師預測數據")

    est = est_list[0]
    q = quote_list[0]
    bal = bal_list[0] if bal_list else {}
    p = prof_list[0] if prof_list else {"beta": 1.0}

    # 2. 提取計算參數
    shares = q.get('sharesOutstanding', 1)
    current_price = q.get('price', 0)
    future_eps = est.get('epsAvg', 0)
    future_ebitda = est.get('ebitdaAvg', 0)
    future_net_income = est.get('netIncomeAvg', 0)
    debt = bal.get('totalDebt', 0)
    cash = bal.get('cashAndCashEquivalents', 0)

    # 3. WACC 計算
    rf = 0.04  # 假設 10 年期美債利率
    beta = p.get('beta', 1.0)
    re = rf + beta * 0.06  # 股權成本 (假設風險溢價 6%)
    market_cap = q.get('marketCap', 1)
    v = market_cap + debt
    wacc = (market_cap / v) * re + (debt / v) * 0.05 * 0.79 if v > 0 else 0.08

    # 4. 產業參數
    ind_map = {
        '科技': {'pe': 30, 'evebitda': 18, 'growth': 0.04},
        '醫療': {'pe': 25, 'evebitda': 15, 'growth': 0.03},
        '金融': {'pe': 12, 'evebitda': 10, 'growth': 0.02},
        '能源': {'pe': 10, 'evebitda': 8, 'growth': 0.02},
        '消費': {'pe': 20, 'evebitda': 12, 'growth': 0.03},
        '工業': {'pe': 18, 'evebitda': 11, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    # 5. 三大估值模型
    # (1) 前瞻 P/E 估值
    v_pe = future_eps * ind['pe']
    
    # (2) 前瞻 EV/EBITDA 估值
    v_ev = ((future_ebitda * ind['evebitda']) - debt + cash) / shares
    
    # (3) 前瞻 DCF 估值 (以預期淨利當作 FCF 近似值)
    v_dcf = ((future_net_income / shares) * (1 + ind['growth'])) / (max(wacc - ind['growth'], 0.01))

    # 綜合目標價
    target = (v_pe + v_ev + v_dcf) / 3 * 1.15

    update_usage() # 成功計算才計數
    
    return {
        "symbol": ticker.upper(),
        "current_price": current_price,
        "wacc": f"{wacc:.2%}",
        "pe": round(v_pe, 2),
        "ev": round(v_ev, 2),
        "dcf": round(v_dcf, 2),
        "target": round(target, 2),
        "analyst_target": q.get('priceAvg200', 0),
        "server_usage": usage_stats["count"]
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)