import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import yfinance as yf
import pandas as pd
import pandas_datareader.data as web
import datetime
import uvicorn

app = FastAPI()

# 模擬您的原始計算邏輯
def get_risk_free_rate():
    try:
        end_date = datetime.datetime.now()
        start_date = end_date - datetime.timedelta(days=30)
        dgs10 = web.DataReader('DGS10', 'fred', start_date, end_date)
        return dgs10['DGS10'].iloc[-1] / 100
    except:
        return 0.03

@app.get("/api/evaluate")
async def evaluate(ticker: str, industry: str):
    try:
        stock = yf.Ticker(ticker.upper())
        info = stock.info
        
        # 基礎數據提取
        data = {
            'market_cap': info.get('marketCap', 0),
            'beta': info.get('beta', 1.0),
            'shares_outstanding': info.get('sharesOutstanding', 0),
            'current_price': info.get('currentPrice', 0),
            'target_mean_price': info.get('targetMeanPrice', 0),
            'forward_eps': info.get('forwardEps', 0),
        }

        # 財務報表 (加入 try-except 防止缺失資料導致崩潰)
        bs = stock.balance_sheet
        data['total_debt'] = bs.loc['Total Liabilities Net Minority Interest'].iloc[0] if 'Total Liabilities Net Minority Interest' in bs.index else 0
        
        fin = stock.financials
        data['ebitda'] = fin.loc['EBITDA'].iloc[0] if 'EBITDA' in fin.index else fin.loc['EBIT'].iloc[0] if 'EBIT' in fin.index else 0
        
        cf = stock.cashflow
        data['fcf'] = cf.loc['Free Cash Flow'].iloc[0] if 'Free Cash Flow' in cf.index else 0
        data['cash'] = bs.loc['Cash Cash Equivalents And Short Term Investments'].iloc[0] if 'Cash Cash Equivalents And Short Term Investments' in bs.index else 0

        # WACC 計算
        rf = get_risk_free_rate()
        Re = rf + data['beta'] * (0.10 - rf)
        V = data['market_cap'] + data['total_debt']
        wacc = (data['market_cap'] / V) * Re + (data['total_debt'] / V) * 0.052 * (1 - 0.21) if V > 0 else 0.08

        # 產業參數
        industry_data = {
            '科技': {'pe': 35, 'evebitda': 20, 'growth': 0.04},
            '醫療': {'pe': 30, 'evebitda': 15, 'growth': 0.03},
            '金融': {'pe': 14, 'evebitda': 10, 'growth': 0.02},
            '能源': {'pe': 12, 'evebitda': 8, 'growth': 0.02},
            '消費': {'pe': 20, 'evebitda': 12, 'growth': 0.03},
            '工業': {'pe': 22, 'evebitda': 10, 'growth': 0.025}
        }
        ind = industry_data.get(industry, industry_data['科技'])

        # 估值計算
        shares = data['shares_outstanding']
        if shares == 0: raise ValueError("無法獲取發行股數")

        v_pe = data['forward_eps'] * ind['pe']
        v_ev = ((data['ebitda'] * ind['evebitda']) - data['total_debt'] + data['cash']) / shares
        v_dcf = (data['fcf'] * (1 + ind['growth']) / (max(wacc - ind['growth'], 0.01))) / shares
        
        comprehensive = (v_pe + v_ev + v_dcf) / 3 * 1.15

        return {
            "symbol": ticker.upper(),
            "current_price": data['current_price'],
            "wacc": f"{wacc:.2%}",
            "dcf": round(v_dcf, 2),
            "pe": round(v_pe, 2),
            "ev": round(v_ev, 2),
            "target": round(comprehensive, 2),
            "analyst_target": data['target_mean_price']
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# 設置靜態檔案路徑
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    # Render 會自動提供 PORT 環境變數
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)