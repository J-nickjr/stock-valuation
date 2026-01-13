import os
import asyncio
import httpx
import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- 安全設定區 ---
# 務必在 Render 的 Environment Variables 設定 FMP_API_KEY
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()

usage_stats = {"count": 0, "date": datetime.date.today().isoformat()}

def update_usage():
    today = datetime.date.today().isoformat()
    if usage_stats["date"] != today:
        usage_stats["count"] = 0
        usage_stats["date"] = today
    usage_stats["count"] += 1

async def fetch_fmp(endpoint: str, symbol: str, params: dict = None, version: str = "v3"):
    if not FMP_API_KEY:
        print("!!! 錯誤: 系統找不到 FMP_API_KEY")
        return None

    query_params = {"apikey": FMP_API_KEY}
    if params:
        query_params.update(params)
    
    url = f"https://financialmodelingprep.com/api/{version}/{endpoint}/{symbol.upper()}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(url, params=query_params, headers=headers, timeout=10.0)
            
            # 如果是 403 (權限問題)，直接回傳 None，交給後端降級邏輯處理
            if response.status_code == 403:
                print(f"!!! 權限限制 (403): 跳過端點 {endpoint}")
                return None
            
            if response.status_code != 200:
                return None
                
            return response.json()
        except Exception as e:
            print(f"Request Exception at {endpoint}: {e}")
            return None

@app.get("/api/usage")
async def get_usage():
    return {"usage": usage_stats["count"]}

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    if not FMP_API_KEY:
        raise HTTPException(status_code=500, detail="API Key 未設定")

    # 同時抓取所有可能需要的數據 (增加 income-statement 當作備案)
    tasks = [
        fetch_fmp("analyst-estimates", ticker, {"period": "annual", "limit": 1}, version="stable"),
        fetch_fmp("quote", ticker),
        fetch_fmp("balance-sheet-statement", ticker, {"period": "annual", "limit": 1}),
        fetch_fmp("income-statement", ticker, {"period": "annual", "limit": 1}),
        fetch_fmp("profile", ticker)
    ]
    
    results = await asyncio.gather(*tasks)
    est_list, quote_list, bal_list, inc_list, prof_list = results

    # 基礎檢查
    if not quote_list:
        raise HTTPException(status_code=404, detail="無法獲取股票基本數據，請檢查 API Key 是否正確設定於 Render")

    q = quote_list[0]
    bal = bal_list[0] if bal_list else {}
    inc = inc_list[0] if inc_list else {}
    p = prof_list[0] if prof_list else {"beta": 1.0}

    # --- 核心邏輯：數據降級策略 ---
    # 優先使用預測數據，若無則使用最近一期財報數據
    if est_list and isinstance(est_list, list) and len(est_list) > 0:
        est = est_list[0]
        future_eps = est.get('epsAvg', q.get('eps', 0))
        future_ebitda = est.get('ebitdaAvg', 0)
        future_net_income = est.get('netIncomeAvg', 0)
        data_source = "分析師前瞻預測"
    else:
        future_eps = inc.get('eps', q.get('eps', 0))
        future_ebitda = inc.get('ebitda', 0)
        future_net_income = inc.get('netIncome', 0)
        data_source = "最近年度財報 (前瞻 API 受限)"

    # 提取計算參數
    current_price = q.get('price', 0)
    shares = q.get('sharesOutstanding')
    if not shares or shares <= 0:
        shares = p.get('mktCap', 0) / current_price if current_price > 0 else 1

    debt = bal.get('totalDebt', 0)
    cash = bal.get('cashAndCashEquivalents', 0)

    # 估值計算
    ind_map = {
        '科技': {'pe': 28, 'evebitda': 16, 'growth': 0.04},
        '醫療': {'pe': 22, 'evebitda': 14, 'growth': 0.03},
        '金融': {'pe': 12, 'evebitda': 10, 'growth': 0.02},
        '能源': {'pe': 10, 'evebitda': 8, 'growth': 0.02},
        '消費': {'pe': 18, 'evebitda': 12, 'growth': 0.03},
        '工業': {'pe': 16, 'evebitda': 11, 'growth': 0.025}
    }
    ind = ind_map.get(industry, ind_map['科技'])

    # 三大模型計算 (增加安全除零檢查)
    v_pe = future_eps * ind['pe']
    v_ev = ((future_ebitda * ind['evebitda']) - debt + cash) / shares if (future_ebitda > 0 and shares > 0) else 0
    
    # 簡單 DCF 估算
    rf = 0.042
    beta = p.get('beta') if p.get('beta') else 1.0
    wacc = max(rf + beta * 0.055, 0.06) 
    v_dcf = ((future_net_income / shares) * (1 + ind['growth'])) / (max(wacc - ind['growth'], 0.02)) if (future_net_income > 0 and shares > 0) else 0

    valid_models = [v for v in [v_pe, v_ev, v_dcf] if v > 0]
    target = (sum(valid_models) / len(valid_models)) * 1.1 if valid_models else current_price * 1.05

    update_usage()
    
    return {
        "symbol": ticker.upper(),
        "current_price": current_price,
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