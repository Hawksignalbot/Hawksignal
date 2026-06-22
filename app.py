import os
import time
import requests
import threading
import random
import re
from flask import Flask, jsonify, request
from datetime import datetime
import pandas as pd
import numpy as np

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TD_API_KEY = os.environ.get("TD_API_KEY")

# tradingview-scraper kütüphanesi (opsiyonel - yoksa stockanalysis.com fallback kullanılır)
try:
    from tradingview_scraper.symbols.market_movers import MarketMovers
    from tradingview_scraper.symbols.news import NewsScraper
    from tradingview_scraper.symbols.overview import Overview
    TV_SCRAPER_AVAILABLE = True
except Exception:
    TV_SCRAPER_AVAILABLE = False

# =====================
# SEKTÖR HARİTASI (sinyal mesajında gösterilir)
# =====================

# =====================

# Sektör bilgisi artık DİNAMİK çekiliyor (sabit liste yok).
# Aynı gün içinde tekrar sorgulamamak için basit bir bellek cache kullanılır.
sector_cache = {}

def get_sector(symbol, exchange='NASDAQ'):
    """tradingview-scraper ile gerçek zamanlı sektör/endüstri bilgisi çeker."""
    cache_key = symbol.upper()
    if cache_key in sector_cache:
        return sector_cache[cache_key]

    if not TV_SCRAPER_AVAILABLE:
        return "Bilinmiyor"

    try:
        ov = Overview()
        # Önce NASDAQ dene, sonra NYSE
        for exch in ['NASDAQ', 'NYSE']:
            result = ov.get_profile(symbol=f'{exch}:{symbol.upper()}')
            if result and result.get('status') == 'success':
                data = result.get('data', {})
                sector = data.get('sector') or data.get('industry') or "Bilinmiyor"
                sector_cache[cache_key] = sector
                return sector
    except:
        pass

    sector_cache[cache_key] = "Bilinmiyor"
    return "Bilinmiyor"

# =====================
# BELLEK
# =====================

portfolio = {}
alerts = {}
tracked = {}
session_sector_signals = {}
news_archive = {}
news_counter = {}

def reset_sector_tracking():
    session_sector_signals.clear()

def register_sector_signal(symbol):
    sector = get_sector(symbol)
    if sector not in session_sector_signals:
        session_sector_signals[sector] = []
    session_sector_signals[sector].append(symbol)
    return sector, len(session_sector_signals[sector])

def get_news_id(date_str=None):
    if not date_str:
        date_str = datetime.now().strftime('%d%m%y')
    if date_str not in news_counter:
        news_counter[date_str] = 0
    news_counter[date_str] += 1
    return f"{date_str}/{news_counter[date_str]}"

# =====================
# TELEGRAM
# =====================

def send_telegram(message, chat_id=None):
    cid = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# =====================
# CANLI HAVUZ SİSTEMİ
# =====================
# Öncelik 1: tradingview-scraper (MarketMovers) - en güvenilir
# Öncelik 2: stockanalysis.com web scraping - yedek kaynak
# Öncelik 3: son başarılı canlı taramanın hafızası - acil durum

last_pool_source = {"source": None, "time": None, "count": 0}
last_successful_pool = []  # En son başarılı CANLI taramanın sonucu (sabit liste DEĞİL)

def fetch_pool_tradingview():
    """tradingview-scraper kütüphanesi ile gainers+losers+active çeker.
    Sadece NASDAQ ve NYSE hisseleri alınır, OTC/AMEX gibi düşük likiditeli borsalar elenir."""
    if not TV_SCRAPER_AVAILABLE:
        return None
    try:
        symbols = set()
        mm = MarketMovers()
        for category in ['gainers', 'losers', 'most-active']:
            try:
                result = mm.scrape(market='stocks-usa', category=category, limit=30)
                if result and result.get('data'):
                    for item in result['data']:
                        sym = item.get('symbol', '')
                        if not (sym.startswith('NASDAQ:') or sym.startswith('NYSE:')):
                            continue
                        clean = sym.split(':')[-1]
                        if clean and re.match(r'^[A-Z]{1,5}$', clean):
                            symbols.add(clean)
            except:
                continue
        return list(symbols) if len(symbols) >= 15 else None
    except:
        return None

def fetch_pool_stockanalysis():
    """stockanalysis.com web scraping ile gainers/active/losers çeker (yedek kaynak)."""
    symbols = set()
    pages = [
        "https://stockanalysis.com/markets/gainers/",
        "https://stockanalysis.com/markets/active/",
        "https://stockanalysis.com/markets/losers/"
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        for url in pages:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            found = re.findall(r'/stocks/([a-z0-9-]+)/', r.text)
            for s in found:
                clean = s.upper()
                if re.match(r'^[A-Z]{1,5}$', clean):
                    symbols.add(clean)
        return list(symbols) if len(symbols) >= 15 else None
    except:
        return None

def get_trading_pool(select_n=30):
    """
    Canlı havuzu üç kademeli öncelikle döndürür:
    1. tradingview-scraper (en güvenilir)
    2. stockanalysis.com (yedek kaynak)
    3. son başarılı canlı taramanın hafızası (acil durum - SABİT LİSTE DEĞİL)
    """
    global last_successful_pool

    pool = fetch_pool_tradingview()
    if pool:
        last_pool_source["source"] = "tradingview"
        last_pool_source["time"] = datetime.now()
        last_pool_source["count"] = len(pool)
        last_successful_pool = pool
        return random.sample(pool, min(select_n, len(pool)))

    pool = fetch_pool_stockanalysis()
    if pool:
        last_pool_source["source"] = "stockanalysis"
        last_pool_source["time"] = datetime.now()
        last_pool_source["count"] = len(pool)
        last_successful_pool = pool
        return random.sample(pool, min(select_n, len(pool)))

    if last_successful_pool:
        last_pool_source["source"] = "memory"
        last_pool_source["time"] = datetime.now()
        last_pool_source["count"] = len(last_successful_pool)
        return random.sample(last_successful_pool, min(select_n, len(last_successful_pool)))

    last_pool_source["source"] = "none"
    return []

def get_source_label():
    src = last_pool_source["source"]
    if src == "tradingview":
        return "📡 Kaynak: Canlı Piyasa Taraması (TradingView) ✅"
    elif src == "stockanalysis":
        return "📡 Kaynak: Canlı Piyasa Taraması (StockAnalysis) ✅"
    elif src == "memory":
        return f"📡 Kaynak: Son Başarılı Tarama Hafızası ⚠️ ({last_pool_source['time'].strftime('%H:%M') if last_pool_source['time'] else '?'})"
    else:
        return "📡 Kaynak: Veri alınamadı ❌"

# =====================
# TWELVE DATA - Teknik gösterge verisi
# =====================

def td_get_ohlcv(symbol, outputsize=210):
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol, "interval": "1day", "outputsize": outputsize,
            "apikey": TD_API_KEY, "format": "JSON"
        }
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error" or "values" not in data:
            return None
        df = pd.DataFrame(data["values"])
        df = df.rename(columns={"datetime":"Date","open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        for col in ["Open","High","Low","Close","Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except:
        return None

# =====================
# GÖSTERGELER
# =====================

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line

def calc_sma(series, period):
    return series.rolling(period).mean()

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_adx(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm - minus_dm) < 0] = 0
    minus_dm[(minus_dm - plus_dm) < 0] = 0
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(period).mean()

def detect_candle_pattern(df):
    """Basit mum formasyonu tespiti - hammer ve engulfing"""
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        body = abs(last['Close'] - last['Open'])
        full_range = last['High'] - last['Low']
        lower_wick = min(last['Open'], last['Close']) - last['Low']

        # Hammer: küçük gövde, uzun alt fitil
        is_hammer = full_range > 0 and lower_wick > body * 2 and body < full_range * 0.35

        # Bullish engulfing: önceki kırmızı mum, şimdiki yeşil mum onu tamamen kapsıyor
        prev_bearish = prev['Close'] < prev['Open']
        curr_bullish = last['Close'] > last['Open']
        is_engulfing = prev_bearish and curr_bullish and last['Close'] > prev['Open'] and last['Open'] < prev['Close']

        if is_hammer:
            return "🔨 Hammer (Dönüş formasyonu)"
        elif is_engulfing:
            return "📈 Bullish Engulfing (Güçlü dönüş)"
        return None
    except:
        return None

def get_weekly_rsi(symbol):
    """Haftalık RSI - çoklu zaman dilimi teyidi için"""
    try:
        df = td_get_ohlcv(symbol, outputsize=100)
        if df is None or len(df) < 30:
            return None
        weekly = df.set_index('Date').resample('W').agg({
            'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'
        }).dropna()
        if len(weekly) < 15:
            return None
        rsi = calc_rsi(weekly['Close'])
        return float(rsi.iloc[-1])
    except:
        return None

def get_qqq_performance(days=20):
    try:
        df = td_get_ohlcv('QQQ', outputsize=days+5)
        if df is None or len(df) < days:
            return None
        old_price = float(df['Close'].iloc[-days])
        new_price = float(df['Close'].iloc[-1])
        return (new_price - old_price) / old_price * 100
    except:
        return None

# =====================
# ANA ANALİZ (Trend Sinyali)
# =====================

def analyze_stock(symbol):
    try:
        df = td_get_ohlcv(symbol, outputsize=210)
        if df is None:
            return None

        data_days = len(df)
        if data_days < 20:
            return None
        low_data_warning = data_days < 60

        close = df['Close']
        volume = df['Volume']

        rsi = calc_rsi(close)
        macd, macd_signal = calc_macd(close)
        ema20 = calc_ema(close, 20)
        sma50 = calc_sma(close, min(50, data_days-1))
        sma200 = calc_sma(close, min(200, data_days-1))
        vol_ma20 = volume.rolling(min(20, data_days-1)).mean()
        atr = calc_atr(df, period=min(14, data_days-1))
        adx = calc_adx(df, period=min(14, data_days-1))

        price = float(close.iloc[-1])
        rsi_now = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2])
        macd_now = float(macd.iloc[-1])
        macd_sig_now = float(macd_signal.iloc[-1])
        macd_prev_val = float(macd.iloc[-2])
        macd_sig_prev = float(macd_signal.iloc[-2])
        ema20_now = float(ema20.iloc[-1])
        sma50_now = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else price
        sma200_now = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else price
        vol_now = float(volume.iloc[-1])
        vol_avg = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else vol_now
        atr_now = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else price * 0.02
        adx_now = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 20

        trend_sma200 = "✅ Yukarıda" if price > sma200_now else "⚠️ Aşağıda"
        trend_ema20 = "✅ Yukarıda" if price > ema20_now else "⚠️ Aşağıda"
        trend_sma50 = "✅ Yukarıda" if price > sma50_now else "⚠️ Aşağıda"
        resistance_risk = (
            (abs(price - sma50_now) / price < 0.02 and price < sma50_now) or
            (abs(price - sma200_now) / price < 0.02 and price < sma200_now)
        )

        rsi_signal = rsi_now > rsi_prev and 40 < rsi_now < 65
        macd_crossover = macd_prev_val < macd_sig_prev and macd_now > macd_sig_now
        macd_positive = macd_now > macd_sig_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0
        vol_ok = vol_ratio >= 1.2

        strong_trend = adx_now >= 25
        adx_text = f"✅ {adx_now:.0f} (Güçlü trend)" if strong_trend else f"⚠️ {adx_now:.0f} (Zayıf trend)"

        index_perf = get_qqq_performance(days=20)
        rs_text = "➖ Veri yok"
        rs_strong = False
        try:
            old_price = float(close.iloc[max(0,len(close)-20)])
            stock_perf = (price - old_price) / old_price * 100
            if index_perf is not None:
                rs_diff = stock_perf - index_perf
                rs_strong = rs_diff > 0
                rs_text = f"✅ QQQ'a göre +{rs_diff:.1f}% güçlü" if rs_strong else f"⚠️ QQQ'a göre {rs_diff:.1f}% zayıf"
        except:
            pass

        divergence_text = "➖ Tespit edilmedi"
        try:
            prices_10 = close.values[-10:]
            rsi_10 = rsi.values[-10:]
            p_low_idx = int(np.argmin(prices_10[:-1]))
            p_high_idx = int(np.argmax(prices_10[:-1]))
            bull_div = prices_10[-1] < prices_10[p_low_idx] and rsi_10[-1] > rsi_10[p_low_idx]
            bear_div = prices_10[-1] > prices_10[p_high_idx] and rsi_10[-1] < rsi_10[p_high_idx]
            if bull_div:
                divergence_text = "🟢 Boğa Uyumsuzluğu"
            elif bear_div:
                divergence_text = "🔴 Ayı Uyumsuzluğu"
        except:
            pass

        score = 0
        if price > sma200_now: score += 2
        if price > sma50_now: score += 1
        if price > ema20_now: score += 1
        if macd_crossover: score += 3
        elif macd_positive: score += 1
        if rsi_signal: score += 2
        if vol_ok: score += 2
        if strong_trend: score += 2
        if rs_strong: score += 1
        if price < sma200_now: score -= 2
        if resistance_risk: score -= 2
        if not strong_trend: score -= 1

        if score < 7:
            return None

        risk_score = max(10, min(90, 100 - (score * 8)))
        risk_label = "Düşük 🟢" if risk_score < 30 else "Orta 🟡" if risk_score < 60 else "Yüksek 🔴"

        entry = price
        take_profit = round(entry * 1.05, 2)
        atr_stop = round(entry - (atr_now * 1.8), 2)
        pct_stop = round(entry * 0.97, 2)
        stop_loss = round(max(atr_stop, pct_stop * 0.98), 2)
        if stop_loss >= entry:
            stop_loss = round(entry * 0.97, 2)
        risk_amt = round(entry - stop_loss, 2)
        reward_amt = round(take_profit - entry, 2)
        rr = round(reward_amt / risk_amt, 1) if risk_amt > 0 else 0

        vol_text = f"✅ {vol_ratio:.1f}x 🔥" if vol_ratio >= 2 else f"✅ {vol_ratio:.1f}x" if vol_ratio >= 1.2 else f"⚠️ {vol_ratio:.1f}x"
        macd_text = "✅ Taze Crossover 🔥" if macd_crossover else "✅ Pozitif" if macd_positive else "⚠️ Negatif"
        rsi_text = f"✅ {rsi_now:.1f}" if rsi_signal else f"⚠️ {rsi_now:.1f}"

        if score >= 9 and rr >= 1.5:
            karar = "💚 İŞLEME GİRİLEBİLİR"
        elif score >= 7:
            karar = "🟡 İZLEMEDE KALSIN"
        else:
            karar = "🔴 GEÇ"

        sector, sector_count = register_sector_signal(symbol)
        sector_text = f"🔥 {sector} sektöründen {sector_count}. sinyal" if sector_count >= 2 else f"📌 {sector} sektörü"
        low_data_note = f"\n⚠️ Not: Sınırlı veri ({data_days} gün).\n" if low_data_warning else ""

        msg = f"""
📊 <b>TREND SİNYALİ — Mevcut Güçlü Trend</b>
🚨 <b>{symbol} - POTANSİYEL SİNYAL</b>
──────────────────────────
📈 Giriş: <b>${entry:.2f}</b>
🎯 Kar Al (%5): <b>${take_profit:.2f}</b>
🛑 Zarar Kes (ATR): <b>${stop_loss:.2f}</b>
⚖️ Risk/Ödül: <b>1:{rr}</b>

📊 <b>Teknik Durum:</b>
• SMA200: {trend_sma200}
• EMA20/SMA50: {trend_ema20} / {trend_sma50}
• RSI: {rsi_text}
• MACD: {macd_text}
• Hacim: {vol_text}
• ADX: {adx_text}
• Relative Strength: {rs_text}
• Uyumsuzluk: {divergence_text}
• Sektör: {sector_text}

⚠️ Direnç: {"⚠️ Kritik seviye yakın" if resistance_risk else "✅ Temiz"}
⚠️ Risk Skoru: %{risk_score} - {risk_label}
{low_data_note}
{get_source_label()}
💡 <b>Karar:</b> {karar}
⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
        return msg
    except:
        return None

# =====================
# ERKEN UYARI SİSTEMİ (7 Maddelik Uyumsuzluk Teyidi)
# =====================

def early_warning_scan(symbol):
    """
    Sadece RSI/fiyat uyumsuzluğuna dayanan erken dönüş sinyali.
    7 kriterle teyit edilir, ana trend filtresinden bağımsız çalışır.
    """
    try:
        df = td_get_ohlcv(symbol, outputsize=60)
        if df is None or len(df) < 30:
            return None

        close = df['Close']
        volume = df['Volume']
        rsi = calc_rsi(close)
        macd, macd_signal = calc_macd(close)
        sma200 = calc_sma(close, min(200, len(close)-1))
        ema20 = calc_ema(close, 20)
        vol_ma20 = volume.rolling(20).mean()

        prices_10 = close.values[-10:]
        rsi_10 = rsi.values[-10:]
        p_low_idx = int(np.argmin(prices_10[:-1]))
        p_high_idx = int(np.argmax(prices_10[:-1]))

        bull_div = prices_10[-1] < prices_10[p_low_idx] and rsi_10[-1] > rsi_10[p_low_idx]
        bear_div = prices_10[-1] > prices_10[p_high_idx] and rsi_10[-1] < rsi_10[p_high_idx]

        if not (bull_div or bear_div):
            return None

        direction = "BOĞA" if bull_div else "AYI"
        price = float(close.iloc[-1])

        # KRİTER 1: Hacim teyidi
        vol_now = float(volume.iloc[-1])
        vol_avg = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else vol_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0
        crit_volume = vol_ratio >= 1.3

        # KRİTER 2: Çoklu zaman dilimi (haftalık RSI)
        weekly_rsi = get_weekly_rsi(symbol)
        if direction == "BOĞA":
            crit_timeframe = weekly_rsi is not None and weekly_rsi < 55
        else:
            crit_timeframe = weekly_rsi is not None and weekly_rsi > 45

        # KRİTER 3: Destek/Direnç seviyesi çakışması
        sma200_now = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else price
        ema20_now = float(ema20.iloc[-1])
        near_support = abs(price - sma200_now) / price < 0.03 or abs(price - ema20_now) / price < 0.02
        crit_support = near_support

        # KRİTER 4: Mum formasyonu
        candle_pattern = detect_candle_pattern(df)
        crit_candle = candle_pattern is not None

        # KRİTER 5: MACD histogram daralması
        macd_hist = (macd - macd_signal).values[-5:]
        if direction == "BOĞA":
            crit_macd_hist = macd_hist[-1] > macd_hist[-3]
        else:
            crit_macd_hist = macd_hist[-1] < macd_hist[-3]

        # KRİTER 6: Piyasa/sektör genel durumu (QQQ)
        qqq_perf = get_qqq_performance(days=5)
        if direction == "BOĞA":
            crit_market = qqq_perf is not None and qqq_perf > -3
        else:
            crit_market = qqq_perf is not None and qqq_perf < 3

        # KRİTER 7: Aşırı satım/alım derinliği
        rsi_now = float(rsi.iloc[-1])
        if direction == "BOĞA":
            crit_depth = rsi_now < 35
        else:
            crit_depth = rsi_now > 65

        criteria = [crit_volume, crit_timeframe, crit_support, crit_candle, crit_macd_hist, crit_market, crit_depth]
        confirmed = sum(criteria)

        if confirmed < 3:
            return None

        confidence = "Yüksek 🟢" if confirmed >= 5 else "Orta 🟡" if confirmed >= 4 else "Düşük 🟠"

        emoji = "🟢" if direction == "BOĞA" else "🔴"
        action = "Yukarı dönüş potansiyeli" if direction == "BOĞA" else "Aşağı dönüş riski"

        msg = f"""
⚡ <b>ERKEN UYARI — Dönüş Potansiyeli</b>
🚨 <b>{symbol} - {direction} UYUMSUZLUĞU</b>
──────────────────────────
{emoji} {action}
💲 Fiyat: ${price:.2f}

📋 <b>Teyit Kriterleri ({confirmed}/7):</b>
• Hacim Teyidi: {"✅" if crit_volume else "❌"}
• Haftalık Teyit: {"✅" if crit_timeframe else "❌"}
• Destek/Direnç: {"✅" if crit_support else "❌"}
• Mum Formasyonu: {"✅ " + candle_pattern if crit_candle else "❌"}
• MACD Daralması: {"✅" if crit_macd_hist else "❌"}
• Piyasa Durumu: {"✅" if crit_market else "❌"}
• Aşırı Satım/Alım: {"✅" if crit_depth else "❌"}

🎯 Güven Seviyesi: <b>{confidence}</b>
{get_source_label()}
⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}

⚠️ Bu erken bir sinyal, ana trend filtresinden geçmemiştir. Dikkatli değerlendir.
"""
        return msg
    except:
        return None

# =====================
# HABERLER
# =====================

def fetch_news_for_symbol(symbol):
    """tradingview-scraper ile sembol bazlı haber çeker."""
    if not TV_SCRAPER_AVAILABLE:
        return []
    try:
        ns = NewsScraper()
        result = ns.scrape_headlines(symbol=symbol, exchange='NASDAQ', sort='latest')
        if result and result.get('data'):
            return result['data'][:5]
        return []
    except:
        return []

# =====================
# TÜRKÇE NORMALİZASYON
# =====================

def normalize_text(text):
    rep = {'İ':'i','I':'i','ı':'i','Ü':'u','ü':'u','Ö':'o','ö':'o','Ş':'s','ş':'s','Ğ':'g','ğ':'g','Ç':'c','ç':'c','Â':'a','â':'a'}
    text = text.lower()
    for k,v in rep.items():
        text = text.replace(k.lower(),v).replace(k,v)
    return text

# =====================
# KOMUT İŞLEYİCİ
# =====================

def handle_command(text, chat_id):
    t = normalize_text(text.strip())

    if any(t.startswith(x) for x in ['/start','/durum','/status']):
        send_telegram(f"""🦅 <b>HAWK SIGNAL BOT — NASDAQ</b>
──────────────────────────
/durum — /status
/yardim — /help
/liste — /list
──────────────────────────
📈 TARAMA | ⚡ ERKEN UYARI
📌 TAKİP | 📊 ANALİZ | 📰 HABER
🔔 ALARMLAR | 💼 PORTFÖY
──────────────────────────
{get_source_label()}
Tarayıcı kütüphanesi: {"Aktif ✅" if TV_SCRAPER_AVAILABLE else "Yedek modda ⚠️"}
──────────────────────────
Detay için /yardim veya /help""", chat_id)
        return

    if any(t.startswith(x) for x in ['/yardim','/yardım','/komutlar','/komut']):
        send_telegram("""📋 <b>KOMUTLAR — Hawk Signal (NASDAQ)</b>

/durum — Sistem durumu
/liste — Havuz bilgisi
──────────────────────────
📈 <b>TARAMA (Trend Sinyali)</b>
/nasdaq tara | /abd tara
/nasdaq [HİSSE]

⚡ <b>ERKEN UYARI (Dönüş Sinyali)</b>
/erkenuyari tara
/erkenuyari [HİSSE]

📌 <b>TAKİP</b>
/takip [hisse] [giris] [stop] [hedef]
/takiplerim

📊 <b>ANALİZ</b>
/analizet [hisse]

📰 <b>HABER</b>
/haber [hisse]

🔔 <b>ALARMLAR</b>
/alarm [hisse] [fiyat]
/alarmlarim | /alarm sil [hisse]

💼 <b>PORTFÖY</b>
/portfoy ekle [hisse] [fiyat] [adet]
/portfoy | /portfoy [hisse]""", chat_id)
        return

    if t.startswith('/help'):
        send_telegram("""📋 <b>COMMANDS — Hawk Signal (NASDAQ)</b>

/status | /list
──────────────────────────
📈 <b>SCAN (Trend Signal)</b>
/nasdaq scan | /nasdaq [TICKER]

⚡ <b>EARLY WARNING (Reversal Signal)</b>
/earlywarning scan | /earlywarning [TICKER]

📌 <b>TRACKING</b>
/track [ticker] [entry] [stop] [target] | /mytracks

📊 <b>ANALYSIS</b>
/analyze [ticker]

📰 <b>NEWS</b>
/news [ticker]

🔔 <b>ALERTS</b>
/alert [ticker] [price] | /alerts | /alert delete [ticker]

💼 <b>PORTFOLIO</b>
/portfolio add [ticker] [price] [qty] | /portfolio""", chat_id)
        return

    if any(t.startswith(x) for x in ['/liste','/list']):
        send_telegram(f"📋 <b>HAVUZ DURUMU</b>\n\n{get_source_label()}\nSon sayı: {last_pool_source['count']} hisse\nTarayıcı kütüphanesi: {'Aktif ✅' if TV_SCRAPER_AVAILABLE else 'Yok, yedek modda ⚠️'}", chat_id)
        return

    # TREND TARAMASI
    if t.startswith('/nasdaq') or t.startswith('/abd'):
        cmd_len = 7 if t.startswith('/nasdaq') else 4
        rest = t[cmd_len:].strip()
        if any(x in rest for x in ['tara','scan']):
            send_telegram("🔍 NASDAQ trend taraması başladı...", chat_id)
            def do_scan(cid=chat_id):
                reset_sector_tracking()
                batch = get_trading_pool(30)
                if last_pool_source["source"] == "memory":
                    send_telegram("⚠️ Canlı veri alınamadı, son başarılı tarama hafızasına geçildi.", cid)
                elif last_pool_source["source"] == "none":
                    send_telegram("❌ Hiçbir veri kaynağına erişilemedi, tarama yapılamadı.", cid)
                    return
                found = 0
                for ticker in batch:
                    signal = analyze_stock(ticker)
                    if signal:
                        send_telegram(signal, cid)
                        found += 1
                        time.sleep(2)
                    time.sleep(1.2)
                if found == 0:
                    send_telegram("Tarama tamamlandı. Uygun trend sinyali bulunamadı.", cid)
            threading.Thread(target=do_scan).start()
        elif rest:
            ticker = rest.upper().split()[0]
            send_telegram(f"🔍 {ticker} analiz ediliyor...", chat_id)
            def do_single(tk=ticker, cid=chat_id):
                signal = analyze_stock(tk)
                send_telegram(signal if signal else f"{tk} için uygun trend sinyali yok.", cid)
            threading.Thread(target=do_single).start()
        return

    # ERKEN UYARI
    if any(t.startswith(x) for x in ['/erkenuyari','/erken uyari','/earlywarning']):
        rest = re.sub(r'^/(erkenuyari|erken uyari|earlywarning)', '', t).strip()
        if any(x in rest for x in ['tara','scan']):
            send_telegram("⚡ Erken uyarı taraması başladı (7 kriterli dönüş teyidi)...", chat_id)
            def do_ew(cid=chat_id):
                batch = get_trading_pool(25)
                found = 0
                for ticker in batch:
                    result = early_warning_scan(ticker)
                    if result:
                        send_telegram(result, cid)
                        found += 1
                        time.sleep(2)
                    time.sleep(1.2)
                if found == 0:
                    send_telegram("Erken uyarı taraması tamamlandı. Teyitli dönüş sinyali bulunamadı.", cid)
            threading.Thread(target=do_ew).start()
        elif rest:
            ticker = rest.upper().split()[0]
            send_telegram(f"⚡ {ticker} erken uyarı analizi yapılıyor...", chat_id)
            def do_single_ew(tk=ticker, cid=chat_id):
                result = early_warning_scan(tk)
                send_telegram(result if result else f"{tk} için teyitli dönüş sinyali yok.", cid)
            threading.Thread(target=do_single_ew).start()
        return

    # ANALİZ
    if any(t.startswith(x) for x in ['/analizet','/analiz','/analyze']):
        parts = text.split(None,1)
        if len(parts) < 2:
            send_telegram("Kullanım: /analizet [HİSSE]", chat_id)
            return
        ticker = parts[1].strip().upper().split()[0]
        send_telegram(f"🔍 {ticker} analiz ediliyor (trend + erken uyarı)...", chat_id)
        def do_full_analyze(tk=ticker, cid=chat_id):
            trend_signal = analyze_stock(tk)
            ew_signal = early_warning_scan(tk)
            if trend_signal:
                send_telegram(trend_signal, cid)
            if ew_signal:
                send_telegram(ew_signal, cid)
            if not trend_signal and not ew_signal:
                send_telegram(f"{tk} için şu an aktif bir sinyal yok.", cid)
        threading.Thread(target=do_full_analyze).start()
        return

    # HABER
    if any(t.startswith(x) for x in ['/haber','/news']):
        parts = text.split(None,1)
        if len(parts) < 2:
            send_telegram("Kullanım: /haber [HİSSE]", chat_id)
            return
        ticker = parts[1].strip().upper().split()[0]
        send_telegram(f"📰 {ticker} haberleri aranıyor...", chat_id)
        def do_news(tk=ticker, cid=chat_id):
            if not TV_SCRAPER_AVAILABLE:
                send_telegram("Haber kütüphanesi şu an aktif değil.", cid)
                return
            news_items = fetch_news_for_symbol(tk)
            if not news_items:
                send_telegram(f"{tk} için güncel haber bulunamadı.", cid)
                return
            for item in news_items:
                nid = get_news_id()
                title = item.get('title', 'Başlık yok')
                provider = item.get('provider', '?')
                news_archive[nid] = {'title': title, 'symbol': tk, 'date': datetime.now().strftime('%d.%m.%Y %H:%M')}
                send_telegram(f"📰 <b>{nid} — {tk}</b>\nKaynak: {provider}\n{title}", cid)
                time.sleep(0.5)
        threading.Thread(target=do_news).start()
        return

    # ALARMLAR
    if any(t.startswith(x) for x in ['/alarm','/alert']):
        parts = text.split()
        if any(t.startswith(x) for x in ['/alarmlarim','/alerts']):
            if alerts:
                msg = "🔔 <b>ALARMLARIM</b>\n" + "\n".join(f"{k} → {v['price']}" for k,v in alerts.items())
                send_telegram(msg, chat_id)
            else:
                send_telegram("Aktif alarm yok.", chat_id)
            return
        if any(x in t for x in ['sil','delete']):
            if len(parts) >= 3:
                ticker = parts[-1].upper()
                if ticker in alerts:
                    del alerts[ticker]
                    send_telegram(f"✅ {ticker} alarmı silindi.", chat_id)
                else:
                    send_telegram(f"{ticker} için alarm bulunamadı.", chat_id)
            return
        if len(parts) >= 3:
            try:
                ticker = parts[1].upper()
                price = float(parts[2])
                alerts[ticker] = {'price': price, 'chat_id': chat_id}
                send_telegram(f"🔔 Alarm kuruldu: {ticker} → {price}", chat_id)
            except:
                send_telegram("Kullanım: /alarm [HİSSE] [FİYAT]", chat_id)
        return

    # PORTFÖY
    if any(t.startswith(x) for x in ['/portfoy','/portföy','/portfolio']):
        parts = text.split()
        if any(x in t for x in ['ekle','add']):
            if len(parts) >= 5:
                try:
                    ticker = parts[2].upper()
                    price = float(parts[3])
                    qty = float(parts[4])
                    portfolio[ticker] = {'price': price, 'qty': qty, 'chat_id': chat_id}
                    send_telegram(f"✅ {ticker} — {qty} adet @ {price} eklendi.", chat_id)
                except:
                    send_telegram("Kullanım: /portfoy ekle [HİSSE] [FİYAT] [ADET]", chat_id)
            return
        if len(parts) >= 2 and not any(x in t for x in ['ekle','add']):
            ticker = parts[1].upper()
            if ticker in portfolio:
                p = portfolio[ticker]
                send_telegram(f"💼 {ticker}\nAlış: {p['price']} x {p['qty']} adet", chat_id)
            else:
                send_telegram(f"{ticker} portföyde yok.", chat_id)
            return
        if portfolio:
            msg = "💼 <b>PORTFÖYÜM</b>\n" + "\n".join(f"{k}: {v['price']} x {v['qty']}" for k,v in portfolio.items())
            send_telegram(msg, chat_id)
        else:
            send_telegram("Portföy boş.", chat_id)
        return

    # TAKİP
    if any(t.startswith(x) for x in ['/takip','/track']):
        parts = text.split()
        if any(x in t for x in ['takiplerim','mytracks']):
            if tracked:
                msg = "📌 <b>TAKİP LİSTEM</b>\n" + "\n".join(f"{k} — Giriş:{v['entry']} Stop:{v['stop']} Hedef:{v['target']}" for k,v in tracked.items())
                send_telegram(msg, chat_id)
            else:
                send_telegram("Takip listesi boş.", chat_id)
            return
        if len(parts) >= 5:
            try:
                ticker = parts[1].upper()
                entry, stop, target = float(parts[2]), float(parts[3]), float(parts[4])
                tracked[ticker] = {'entry': entry, 'stop': stop, 'target': target, 'chat_id': chat_id}
                send_telegram(f"📌 {ticker} takibe alındı.\nGiriş:{entry} Stop:{stop} Hedef:{target}", chat_id)
            except:
                send_telegram("Kullanım: /takip [HİSSE] [GİRİŞ] [STOP] [HEDEF]", chat_id)
        return

    send_telegram("Komut tanınamadı. /yardim veya /help yazabilirsin.", chat_id)

# =====================
# WEBHOOK & ROUTES
# =====================

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True)
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        if text and chat_id:
            threading.Thread(target=handle_command, args=(text, chat_id)).start()
    except:
        pass
    return jsonify({"ok": True})

@app.route("/set_webhook")
def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return jsonify({"error": "RAILWAY_PUBLIC_DOMAIN not set"})
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", params={"url": f"https://{domain}/webhook"})
    return jsonify(r.json())

@app.route("/")
def home():
    return jsonify({"status": "Hawk Signal Bot - NASDAQ 🦅", "pool_source": last_pool_source["source"], "scraper_available": TV_SCRAPER_AVAILABLE})

@app.route("/test")
def test():
    send_telegram(f"🦅 <b>Hawk Signal Bot Aktif!</b>\n\n✅ Canlı piyasa taraması\n✅ Erken Uyarı Sistemi (7 kriter)\n✅ Haber entegrasyonu\n\nTarayıcı kütüphanesi: {'Aktif ✅' if TV_SCRAPER_AVAILABLE else 'Yedek modda ⚠️'}")
    return jsonify({"status": "Test mesajı gönderildi!"})

# =====================
# OTOMATİK TARAMA
# =====================

def is_nasdaq_hours():
    now = datetime.utcnow()
    return 12 <= now.hour <= 23

def auto_scan_loop():
    time.sleep(15)
    send_telegram(f"🦅 <b>Hawk Signal Bot — NASDAQ</b>\n\n✅ Canlı piyasa taraması\n✅ Trend + Erken Uyarı sistemleri aktif\n✅ 25 dakikada bir otomatik tarama\n\nTarayıcı kütüphanesi: {'Aktif ✅' if TV_SCRAPER_AVAILABLE else 'Yedek modda ⚠️'}")

    while True:
        try:
            if is_nasdaq_hours():
                reset_sector_tracking()
                batch = get_trading_pool(30)

                if last_pool_source["source"] == "memory":
                    send_telegram("⚠️ Otomatik tarama: canlı veri alınamadı, hafızadaki son başarılı taramaya geçildi.")
                elif last_pool_source["source"] == "none":
                    send_telegram("❌ Otomatik tarama: hiçbir veri kaynağına erişilemedi, bu döngü atlandı.")
                    time.sleep(1500)
                    continue

                for ticker in batch:
                    signal = analyze_stock(ticker)
                    if signal:
                        send_telegram(signal)
                        time.sleep(3)
                    time.sleep(1)

                ew_batch = random.sample(batch, min(15, len(batch)))
                for ticker in ew_batch:
                    result = early_warning_scan(ticker)
                    if result:
                        send_telegram(result)
                        time.sleep(3)
                    time.sleep(1)

            for ticker, data in list(tracked.items()):
                try:
                    df = td_get_ohlcv(ticker, 5)
                    if df is not None:
                        current = float(df['Close'].iloc[-1])
                        if current <= data['stop']:
                            send_telegram(f"🛑 <b>STOP — {ticker}</b>\n${current:.2f} → Stop ${data['stop']}", data['chat_id'])
                        elif current >= data['target']:
                            send_telegram(f"🎯 <b>HEDEF — {ticker}</b>\n${current:.2f} → Hedef ${data['target']}", data['chat_id'])
                except:
                    pass

            for ticker, data in list(alerts.items()):
                try:
                    df = td_get_ohlcv(ticker, 5)
                    if df is not None:
                        current = float(df['Close'].iloc[-1])
                        if current >= data['price']:
                            send_telegram(f"🔔 <b>ALARM — {ticker}</b>\n${current:.2f} → Hedef ${data['price']}", data['chat_id'])
                            del alerts[ticker]
                except:
                    pass
        except:
            pass

        time.sleep(1500)

threading.Thread(target=auto_scan_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
