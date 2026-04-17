import asyncio
import os
import time
import yfinance as yf
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()

executor = ThreadPoolExecutor(max_workers=5)

# 快取：避免短時間內重複打 Yahoo Finance 同一支股票
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 分鐘


def prewarm():
    """啟動時預先初始化 curl_cffi session，避免第一次請求超時。"""
    for attempt in range(3):
        try:
            yf.Ticker("AAPL").fast_info
            print("[YF] curl_cffi session 預熱成功")
            return
        except Exception as e:
            print(f"[YF] 預熱失敗第 {attempt+1} 次: {e}")
            if attempt < 2:
                time.sleep(5)
    print("[YF] 預熱全部失敗，服務仍會繼續，但首次查詢可能較慢")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, prewarm)
    yield


app = FastAPI(lifespan=lifespan)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

HELP_TEXT = (
    "📋 使用說明\n"
    "請直接輸入股票代碼\n\n"
    "範例：\n"
    "  AAPL\n"
    "  TSLA\n"
    "  2330\n"
    "  2884"
)


# ── 取得股票原始數據 ────────────────────────────────────────────

def get_stock_data_sync(ticker_str: str):
    try:
        raw_ticker = ticker_str.strip().upper()
        search_tickers = []

        if raw_ticker.isdigit():
            search_tickers = [f"{raw_ticker}.TW", f"{raw_ticker}.TWO"]
        else:
            search_tickers = [raw_ticker]

        for t in search_tickers:
            if t in _cache:
                cached_data, cached_at = _cache[t]
                if time.time() - cached_at < CACHE_TTL:
                    print(f"[Cache] {t} 命中快取")
                    return cached_data

            try:
                stock = yf.Ticker(t)

                # fast_info 比 info 更穩定，優先使用
                fi = stock.fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
                currency = getattr(fi, "currency", "USD") or "USD"

                print(f"[{t}] fast_info price={price} currency={currency}")

                if not price:
                    print(f"[{t}] 無法取得股價，略過")
                    continue

                # info 用來取補充資料，失敗不影響主流程
                info = {}
                try:
                    info = stock.info or {}
                except Exception as e:
                    print(f"[{t}] info 失敗（略過）: {e}")

                result = {
                    "symbol": (info.get("symbol") or t).upper(),
                    "name": info.get("shortName", ""),
                    "current_price": price,
                    "future_eps": info.get("forwardEps") or info.get("trailingEps", 0),
                    "peg": info.get("pegRatio", 0),
                    "eps_growth": info.get("earningsGrowth", 0),
                    "target_mean": info.get("targetMeanPrice", 0),
                    "beta": info.get("beta", 1.0),
                    "ebitda": info.get("ebitda", 0),
                    "total_debt": info.get("totalDebt", 0),
                    "total_cash": info.get("totalCash", 0),
                    "shares": info.get("sharesOutstanding") or getattr(fi, "shares", 1) or 1,
                    "currency": currency,
                    "sector": info.get("sector", ""),
                }
                _cache[t] = (result, time.time())
                return result
            except Exception as e:
                print(f"[{t}] 例外: {e}")
                continue

        print(f"所有代碼均無法取得資料: {search_tickers}")
        return None
    except Exception as e:
        print(f"YFinance Error: {e}")
        return None


# ── 估值計算 ────────────────────────────────────────────────────

SECTOR_MAP = {
    # yfinance 英文 sector → (US PE, TW PE, growth)
    "Technology":             (25, 18, 0.04),
    "Healthcare":             (22, 20, 0.03),
    "Financial Services":     (12, 10, 0.02),
    "Energy":                 (10,  8, 0.02),
    "Consumer Cyclical":      (18, 15, 0.03),
    "Consumer Defensive":     (18, 15, 0.03),
    "Industrials":            (15, 12, 0.025),
    "Basic Materials":        (12, 10, 0.02),
    "Real Estate":            (20, 15, 0.025),
    "Utilities":              (14, 12, 0.02),
    "Communication Services": (20, 16, 0.03),
}


def compute_evaluation(data: dict) -> dict:
    is_tw = data["currency"] == "TWD"

    # 自動依 sector 對應 PE 與成長率，找不到則用通用預設值
    sector = data.get("sector", "")
    if sector in SECTOR_MAP:
        pe_us, pe_tw, growth = SECTOR_MAP[sector]
        ind = {"pe": pe_tw if is_tw else pe_us, "growth": growth}
    else:
        ind = {"pe": 15 if is_tw else 20, "growth": 0.03}

    # WACC（台股無風險利率較低）
    rf = 0.015 if is_tw else 0.042
    wacc_val = max(rf + data["beta"] * 0.055, 0.05)
    wacc_str = f"{round(wacc_val * 100, 2)}%"

    # 各估值模型
    # P/E 估值 = 產業平均 PE × 明年預估 EPS
    v_pe = ind["pe"] * data["future_eps"] if data["future_eps"] > 0 else 0

    if data["ebitda"] > 0 and data["shares"] > 0:
        v_ev = (data["ebitda"] * 12 - data["total_debt"] + data["total_cash"]) / data["shares"]
    else:
        v_ev = 0

    v_dcf = (
        (data["future_eps"] * (1 + ind["growth"])) / max(wacc_val - ind["growth"], 0.015)
        if data["future_eps"] > 0
        else 0
    )

    v_analyst = data["target_mean"]

    valid_vals = [v for v in [v_pe, v_ev, v_dcf, v_analyst] if v > 0]
    final_target = sum(valid_vals) / len(valid_vals) if valid_vals else data["current_price"]

    return {
        "symbol": data["symbol"],
        "name": data.get("name", ""),
        "sector": sector,
        "current_price": round(data["current_price"], 2),
        "target": round(final_target, 2),
        "analyst_target": round(v_analyst, 2),
        "pe": round(v_pe, 2),
        "ev": round(v_ev, 2),
        "dcf": round(v_dcf, 2),
        "wacc": wacc_str,
        "currency": "NT$" if is_tw else "$",
    }


# ── LINE 訊息格式化 ─────────────────────────────────────────────

def format_result_message(result: dict) -> str:
    c = result["currency"]
    current = result["current_price"]
    target = result["target"]

    if current > 0:
        upside = round((target - current) / current * 100, 1)
        upside_str = f"+{upside}%" if upside >= 0 else f"{upside}%"
        trend = "📈" if upside >= 0 else "📉"
    else:
        upside_str = "N/A"
        trend = "➖"

    display_symbol = result["symbol"].replace(".TWO", "").replace(".TW", "")
    sector_str = f" ｜ {result['sector']}" if result.get("sector") else ""
    lines = [
        f"📊 {display_symbol} — {result['name']}{sector_str}",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 目前股價    {c}{current}",
        f"🎯 綜合目標價  {c}{target}",
        f"{trend} 潛在漲幅    {upside_str}",
        "",
        "📐 估值明細",
        f"  • DCF 估值     {c}{result['dcf']}",
        f"  • P/E 估值     {c}{result['pe']}",
        f"  • EV/EBITDA   {c}{result['ev']}",
        f"  • 分析師目標   {c}{result['analyst_target']}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📌 數據來源：Yahoo Finance",
        "⚠️  投資有風險，請自行評估",
    ]
    return "\n".join(lines)


# ── LINE Webhook 處理 ───────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    try:
        ticker_input = event.message.text.strip().split()[0] if event.message.text.strip() else ""

        if not ticker_input:
            reply = HELP_TEXT
        else:
            data = get_stock_data_sync(ticker_input)
            if not data or data["current_price"] == 0:
                reply = (
                    f"❌ 找不到「{ticker_input}」的數據\n\n"
                    "請確認：\n"
                    "  • 台股：輸入數字代碼（如 2330）\n"
                    "  • 美股：輸入英文代號（如 AAPL）"
                )
            else:
                result = compute_evaluation(data)
                reply = format_result_message(result)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply)],
                )
            )
    except Exception as e:
        print(f"handle_message error: {e}")
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="⚠️ 系統發生錯誤，請稍後再試")],
                    )
                )
        except Exception:
            pass


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(executor, handler.handle, body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"
