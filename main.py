import os
import asyncio
import httpx
import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- 安全設定區 ---
FMP_API_KEY = os.environ.get("FMP_API_KEY", "ThjXlqf9hWEiYHU9Ccms51LqZuVBm1qj")
BASE_URL = "https://financialmodelingprep.com/api/v3"

usage_stats = {"count": 0, "date": datetime.date.today().isoformat()}

def update_usage():
    today = datetime.date.today().isoformat()
    if usage_stats["date"] != today:
        usage_stats["count"] = 0
        usage_stats["date"] = today
    usage_stats["count"] += 1

async def fetch_fmp(endpoint: str, symbol: str, params: dict = None):
    query_params = {"apikey": FMP_API_KEY}
    if params:
        query_params.update(params)
    
    url = f"{BASE_URL}/{endpoint}/{symbol.upper()}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=query_params, timeout=15.0)
            if response.status_code != 200:
                print(f"Error: {endpoint} returned status {response.status_code}")
                return None
                
            data = response.json()
            
            if isinstance(data, dict) and "Error Message" in data:
                print(f"FMP Error: {data['Error Message']}")
                return None
            return data
        except Exception as e:
            print(f"Request Exception for {endpoint}: {e}")
            return None

@app.get("/api/usage")
async def get_usage():
    return {"usage": usage_stats["count"]}

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    if usage_stats["count"] >= 250:
        raise HTTPException(status_code=403, detail="全站額度已用完，請明天再試")

    # 同時抓取所有數據
    tasks = [
        fetch_fmp("analyst-estimates", ticker, {"period": "annual", "limit": 1}),
        fetch_fmp("quote", ticker),
        fetch_fmp("balance-sheet-statement", ticker, {"period": "annual", "limit": 1}),
        fetch_fmp("profile", ticker)
    ]
    
    results = await asyncio.gather(*tasks)
    est_list, quote_list, bal_list, prof_list = results

    # 基礎檢查：一定要有 quote (報價) 才能進行任何計算
    if not quote_list or len(quote_list) == 0:
        raise HTTPException(status_code=404, detail=f"無法獲取 {ticker} 的基本報價數據")

    q = quote_list[0]
    bal = bal_list[0] if bal_list else {}
    p = prof_list[0] if prof_list else {"beta": 1.0}
    
    # --- 關鍵修正：容錯機制 ---
    # 如果沒有分析師預測，則使用現有的 EPS/EBITDA 資料替代
    if est_list and len(est_list) > 0:
        est = est_list[0]
        future_eps = est.get('epsAvg', q.get('eps', 0))
        future_ebitda = est.get('ebitdaAvg', 0)
        future_net_income = est.get('netIncomeAvg', 0)
        data_source = "分析師預測數據"
    else:
        # 降級方案：使用當前 TTM 數據
        future_eps = q.get('eps', 0)
        future_ebitda = 0 # 若無預估，EBITDA 估值通常無法運算
        future_net_income = 0
        data_source = "歷史數據 (無預測資料)"

    shares = q.get('sharesOutstanding')
    if not shares or shares <= 0:
        # 如果 quote 沒抓到股數，嘗試從 profile 抓
        shares = p.get('mktCap', 0) / q.get('price', 1) if q.get('price', 0) > 0 else 1

    current_price = q.get('price', 0)
    debt = bal.get('totalDebt', 0)
    cash = bal.get('cashAndCashEquivalents', 0)

    # 3. WACC 計算 (CAPM 模型)
    rf = 0.042 
    beta = p.get('beta') if p.get('beta') else 1.0
    re = rf + beta * 0.055 
    market_cap = q.get('marketCap', current_price * shares)
    v = market_cap + debt
    wacc = (market_cap / v) * re + (debt / v) * 0.05 * 0.79 if v > 0 else 0.08

    # 4. 產業參數
    ind_map = {
        '科技': {'pe': 28, 'evebitda': 16, 'growth': 0.04},
        '醫療': {'pe': 22, 'evebitda': 14, 'growth': 0.03},
        '金融': {'pe': 12, 'evebitda': 10, 'growth': 0.02},
        '能源': {'pe': 10, 'evebitda': 8, 'growth': 0.02},
        '消費': {'pe': 18, 'evebitda': 12, 'growth': 0.03},
        '工業': {'pe': 16, 'evebitda': 11, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    # 5. 三大估值模型 (增加 0 值檢查)
    v_pe = future_eps * ind['pe']
    v_ev = ((future_ebitda * ind['evebitda']) - debt + cash) / shares if future_ebitda > 0 else 0
    v_dcf = ((future_net_income / shares) * (1 + ind['growth'])) / (max(wacc - ind['growth'], 0.01)) if future_net_income > 0 else 0

    # 綜合目標價計算邏輯
    valid_models = [v for v in [v_pe, v_ev, v_dcf] if v > 0]
    if not valid_models:
        target = current_price * 1.1 # 若無預估數據，給予一個簡單溢價當作示範
    else:
        target = (sum(valid_models) / len(valid_models)) * 1.1

    update_usage()
    
    return {
        "symbol": ticker.upper(),
        "current_price": current_price,
        "wacc": f"{wacc:.2%}",
        "pe": round(v_pe, 2),
        "ev": round(v_ev, 2),
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