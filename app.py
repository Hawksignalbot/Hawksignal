import os
import time
import requests
import threading
from flask import Flask, jsonify, request
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import numpy as np
import re

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# =====================
# BORSA LİSTELERİ
# =====================

NASDAQ_LIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "AVGO", "QCOM", "INTC", "MU", "AMAT", "KLAC", "MRVL", "ORCL",
    "CRM", "NOW", "SNOW", "PLTR", "DDOG", "NET", "CRWD", "ZS",
    "PANW", "SHOP", "NFLX", "UBER", "PYPL", "COIN", "MRNA", "BNTX",
    "ENPH", "FSLR", "RIVN", "RBLX", "U", "IONQ", "QUBT", "TTD"
]

BIST_LIST = [
    "THYAO.IS", "GARAN.IS", "ASELS.IS", "KCHOL.IS", "EREGL.IS",
    "BIMAS.IS", "AKBNK.IS", "YKBNK.IS", "TUPRS.IS", "SISE.IS",
    "PGSUS.IS", "TAVHL.IS", "TOASO.IS", "FROTO.IS", "SAHOL.IS",
    "HALKB.IS", "VAKBN.IS", "TCELL.IS", "TTKOM.IS", "EKGYO.IS"
]

ALMAN_LIST = [
    "SAP.DE", "SIE.DE", "ALV.DE", "MRK.DE", "BAYN.DE",
    "BASF.DE", "BMW.DE", "MBG.DE", "VOW3.DE", "DTE.DE",
    "DBK.DE", "CBK.DE", "BAS.DE", "EOAN.DE", "RWE.DE"
]

# Portföy ve alarm hafızası
portfolio = {}
alerts = {}
tracked = {}
news_archive = {}
news_counter = {}

# =====================
# YARDIMCI FONKSİYONLAR
# =====================

def normalize_text(text):
    """Türkçe karakter normalizasyonu ve küçük harf"""
    replacements = {
        'İ': 'i', 'I': 'i', 'ı': 'i',
        'Ü': 'u', 'ü': 'u',
        'Ö': 'o', 'ö': 'o',
        'Ş': 's', 'ş': 's',
        'Ğ': 'g', 'ğ': 'g',
        'Ç': 'c', 'ç': 'c',
        'Â': 'a', 'â': 'a'
    }
    text = text.lower()
    for k, v in replacements.items():
        text = text.replace(k.lower(), v).replace(k, v)
    return text

def detect_market(text):
    """Hangi borsa olduğunu tespit et"""
    t = normalize_text(text)
    if any(w in t for w in ['nasdaq', 'amerika', 'abd', 'us', 'usa', 'america']):
        return 'nasdaq'
    if any(w in t for w in ['bist', 'turkiye', 'turkey', 'istanbul', 'ist', 'borsa istanbul']):
        return 'bist'
    if any(w in t for w in ['alman', 'almanya', 'dax', 'german', 'germany', 'de', 'xetra']):
        return 'alman'
    return None

def get_market_list(market):
    if market == 'nasdaq':
        return NASDAQ_LIST
    elif market == 'bist':
        return BIST_LIST
    elif market == 'alman':
        return ALMAN_LIST
    return []

def format_ticker(ticker, market):
    if market == 'bist' and not ticker.endswith('.IS'):
        return ticker + '.IS'
    if market == 'alman' and not ticker.endswith('.DE'):
        return ticker + '.DE'
    return ticker

def send_telegram(message, chat_id=None):
    cid = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def get_news_id(date_str=None):
    """Haber numarası üret: DDMMYY/N"""
    if not date_str:
        date_str = datetime.now().strftime('%d%m%y')
    if date_str not in news_counter:
        news_counter[date_str] = 0
    news_counter[date_str] += 1
    return f"{date_str}/{news_counter[date_str]}"

def save_news(news_id, market, title, sentiment, hours_ago, content):
    news_archive[news_id] = {
        'market': market,
        'title': title,
        'sentiment': sentiment,
        'hours_ago': hours_ago,
        'content': content,
        'date': datetime.now().strftime('%d.%m.%Y %H:%M')
    }

# =====================
# GÖSTERGELERİ HESAPLA
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

def get_earnings_days(ticker):
    try:
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        if cal is not None and not cal.empty:
            earnings_date = cal.iloc[0, 0]
            if hasattr(earnings_date, 'date'):
                days_left = (earnings_date.date() - datetime.now().date()).days
                return max(0, days_left)
    except:
        pass
    return 999

def get_revenue_growth(ticker):
    try:
        stock = yf.Ticker(ticker)
        financials = stock.quarterly_financials
        if financials is not None and 'Total Revenue' in financials.index:
            revenues = financials.loc['Total Revenue'].dropna()
            if len(revenues) >= 2:
                growth = (revenues.iloc[0] - revenues.iloc[1]) / abs(revenues.iloc[1])
                return float(growth)
    except:
        pass
    return None

# =====================
# ANA ANALİZ FONKSİYONU
# =====================

def analyze_stock(ticker, market='nasdaq'):
    try:
        df = yf.download(ticker, period="200d", interval="1d", progress=False)
        if df is None or len(df) < 60:
            return None

        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()

        rsi = calc_rsi(close)
        macd, macd_signal = calc_macd(close)
        ema20 = calc_ema(close, 20)
        sma50 = calc_sma(close, 50)
        sma200 = calc_sma(close, 200)
        vol_ma20 = volume.rolling(20).mean()

        price = float(close.iloc[-1])
        rsi_now = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2])
        macd_now = float(macd.iloc[-1])
        macd_sig_now = float(macd_signal.iloc[-1])
        macd_prev_val = float(macd.iloc[-2])
        macd_sig_prev = float(macd_signal.iloc[-2])
        ema20_now = float(ema20.iloc[-1])
        sma50_now = float(sma50.iloc[-1])
        sma200_now = float(sma200.iloc[-1])
        vol_now = float(volume.iloc[-1])
        vol_avg = float(vol_ma20.iloc[-1])

        # Filtre 1: Trend
        trend_sma200 = "✅ Yukarıda" if price > sma200_now else "⚠️ Aşağıda"
        trend_ema20 = "✅ Yukarıda" if price > ema20_now else "⚠️ Aşağıda"
        trend_sma50 = "✅ Yukarıda" if price > sma50_now else "⚠️ Aşağıda"

        resistance_risk = (
            (abs(price - sma50_now) / price < 0.02 and price < sma50_now) or
            (abs(price - sma200_now) / price < 0.02 and price < sma200_now)
        )

        # Filtre 2: Teknik
        rsi_signal = rsi_now > rsi_prev and 40 < rsi_now < 65
        macd_crossover = macd_prev_val < macd_sig_prev and macd_now > macd_sig_now
        macd_positive = macd_now > macd_sig_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0

        # Filtre 3: Bilanço
        earnings_days = get_earnings_days(ticker)
        if earnings_days <= 7:
            return None
        revenue_growth = get_revenue_growth(ticker)

        # Skor
        score = 0
        if price > sma200_now: score += 2
        if price > sma50_now: score += 1
        if price > ema20_now: score += 1
        if macd_crossover: score += 3
        elif macd_positive: score += 1
        if rsi_signal: score += 2
        if vol_ratio >= 1.2: score += 2
        if price < sma200_now: score -= 2
        if resistance_risk: score -= 2
        if revenue_growth is not None and revenue_growth < 0: score -= 1

        if score < 6:
            return None

        # Risk
        risk_score = max(10, min(90, 100 - (score * 10)))
        risk_label = "Düşük 🟢" if risk_score < 30 else "Orta 🟡" if risk_score < 60 else "Yüksek 🔴"

        # Filtre 4: Çıkış
        entry = price
        take_profit = round(entry * 1.05, 2)
        support = max(ema20_now, sma50_now * 0.99)
        stop_loss = round(min(support, entry * 0.97), 2)
        risk_amt = round(entry - stop_loss, 2)
        reward_amt = round(take_profit - entry, 2)
        rr = round(reward_amt / risk_amt, 1) if risk_amt > 0 else 0

        vol_text = f"✅ {vol_ratio:.1f}x 🔥" if vol_ratio >= 2 else f"✅ {vol_ratio:.1f}x" if vol_ratio >= 1.2 else f"⚠️ {vol_ratio:.1f}x"
        macd_text = "✅ Taze Crossover 🔥" if macd_crossover else "✅ Pozitif" if macd_positive else "⚠️ Negatif"
        rsi_text = f"✅ {rsi_now:.1f}" if rsi_signal else f"⚠️ {rsi_now:.1f}"
        earnings_text = f"⚠️ {earnings_days} gün kaldı" if earnings_days < 30 else f"✅ Güvenli"
        rev_text = f"{'✅' if revenue_growth and revenue_growth > 0 else '⚠️'} {revenue_growth*100:.1f}%" if revenue_growth is not None else "— Veri yok"

        if score >= 8 and rr >= 2:
            karar = "💚 İŞLEME GİRİLEBİLİR. Güçlü setup."
        elif score >= 6:
            karar = "🟡 İZLEMEDE KALSIN. Onay bekleniyor."
        else:
            karar = "🔴 GEÇ. Kriterler yetersiz."

        clean_ticker = ticker.replace('.IS', '').replace('.DE', '')

        msg = f"""
🚨 <b>{clean_ticker} - POTANSİYEL SİNYAL</b>
──────────────────────────
📈 Giriş: <b>${entry:.2f}</b>
🎯 Kar Al (%5): <b>${take_profit:.2f}</b>
🛑 Zarar Kes: <b>${stop_loss:.2f}</b>
⚖️ Risk/Ödül: <b>1:{rr}</b>

📊 <b>Teknik Durum:</b>
• SMA200: {trend_sma200}
• EMA20 / SMA50: {trend_ema20} / {trend_sma50}
• RSI: {rsi_text}
• MACD: {macd_text}
• Hacim: {vol_text}

⚠️ <b>Risk:</b>
• Bilanço: {earnings_text}
• Gelir Büyümesi: {rev_text}
• Direnç: {"⚠️ Kritik seviye yakın" if resistance_risk else "✅ Temiz"}
• Risk Skoru: %{risk_score} - {risk_label}

💡 <b>Karar:</b> {karar}
⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
        return msg

    except:
        return None

# =====================
# UYUMSUZLUK ANALİZİ
# =====================

def check_divergence(ticker):
    try:
        df = yf.download(ticker, period="60d", interval="1d", progress=False)
        if df is None or len(df) < 30:
            return None

        close = df['Close'].squeeze()
        rsi = calc_rsi(close)
        macd, macd_signal = calc_macd(close)

        prices = close.values[-10:]
        rsi_vals = rsi.values[-10:]
        macd_vals = macd.values[-10:]

        price_low_idx = np.argmin(prices)
        price_high_idx = np.argmax(prices)

        bull_div = False
        bear_div = False
        div_time = datetime.now().strftime('%H:%M')

        # Boğa uyumsuzluğu
        if price_low_idx < len(prices) - 1:
            if prices[-1] < prices[price_low_idx] and rsi_vals[-1] > rsi_vals[price_low_idx]:
                bull_div = True

        # Ayı uyumsuzluğu
        if price_high_idx < len(prices) - 1:
            if prices[-1] > prices[price_high_idx] and rsi_vals[-1] < rsi_vals[price_high_idx]:
                bear_div = True

        clean_ticker = ticker.replace('.IS', '').replace('.DE', '')

        if bull_div:
            return f"⚡ <b>BOĞA UYUMSUZLUĞU — {clean_ticker}</b>\nTür: Al Sinyali\nBaşlangıç: {div_time}\nFiyat yeni dip yaparken RSI yükseliyor\nRSI: {rsi_vals[-1]:.1f}"
        if bear_div:
            return f"⚡ <b>AYI UYUMSUZLUĞU — {clean_ticker}</b>\nTür: Sat Sinyali\nBaşlangıç: {div_time}\nFiyat yeni zirve yaparken RSI düşüyor\nRSI: {rsi_vals[-1]:.1f}"

        return None
    except:
        return None

# =====================
# KOMUT İŞLEYİCİ
# =====================

def handle_command(text, chat_id):
    t = normalize_text(text.strip())
    original = text.strip()

    # /start veya /durum veya /status
    if any(t.startswith(x) for x in ['/start', '/durum', '/status']):
        send_telegram("""🦅 <b>HAWK SIGNAL BOT v2.0</b>
──────────────────────────
/durum — /status
/yardim — /help
/liste — /list
──────────────────────────
📈 TARAMA
📊 ANALİZ
⚡ UYUMSUZLUK
📌 TAKİP
📰 HABERLER
🔔 ALARMLAR
💼 PORTFÖY
──────────────────────────
Detay için /yardim veya /help""", chat_id)
        return

    # /yardim (Türkçe)
    if any(t.startswith(x) for x in ['/yardim', '/yardım', '/komutlar', '/komut']):
        send_telegram("""📋 <b>HAWK SIGNAL BOT — KOMUTLAR</b>

/durum — Sistem durumu
/liste — İzleme listesi
/yardim — Bu menü (Türkçe)
/help — This menu (English)
──────────────────────────
📈 <b>TARAMA</b>
/nasdaq tara — NASDAQ tara
/nasdaq [HİSSE] — Tek hisse
/bist tara — BIST tara
/bist [HİSSE] — Tek hisse
/alman tara — Alman tara
/alman [HİSSE] — Tek hisse

⚡ <b>UYUMSUZLUK</b>
/uyumsuzluk [borsa]

📌 <b>TAKİP</b>
/takip [hisse] giris[f] stop[f] hedef[f]
/takiplerim

📊 <b>ANALİZ</b>
/analizet [hisse veya haber no]
/analiz et [hisse veya haber no]
/ozet [borsa] [tarih]

📰 <b>HABERLER</b>
/haberler [borsa]
/haberlerhepsi [tarih]
/habertekrar [tarih]

🔔 <b>ALARMLAR</b>
/alarm [hisse] [fiyat]
/alarmlarim
/alarm sil [hisse]

💼 <b>PORTFÖY</b>
/portfoy ekle [hisse] [fiyat] [adet]
/portfoy
/portfoy [hisse]""", chat_id)
        return

    # /help (İngilizce)
    if t.startswith('/help'):
        send_telegram("""📋 <b>HAWK SIGNAL BOT — COMMANDS</b>

/status — System status
/list — Watchlist
/help — This menu (English)
/yardim — Bu menü (Türkçe)
──────────────────────────
📈 <b>SCAN</b>
/nasdaq scan — Scan NASDAQ
/nasdaq [TICKER] — Single stock
/bist scan — Scan BIST
/bist [TICKER] — Single stock
/german scan — Scan German
/german [TICKER] — Single stock

⚡ <b>DIVERGENCE</b>
/divergence [market]

📌 <b>TRACKING</b>
/track [ticker] entry[p] stop[p] target[p]
/mytracks

📊 <b>ANALYSIS</b>
/analyze [ticker or news id]
/analysis [ticker or news id]
/summary [market] [date]

📰 <b>NEWS</b>
/news [market]
/newsall [date]
/newsrepeat [date]

🔔 <b>ALERTS</b>
/alert [ticker] [price]
/alerts
/alert delete [ticker]

💼 <b>PORTFOLIO</b>
/portfolio add [ticker] [price] [qty]
/portfolio
/portfolio [ticker]""", chat_id)
        return

    # /liste veya /list
    if any(t.startswith(x) for x in ['/liste', '/list']):
        msg = f"""📋 <b>İZLEME LİSTESİ</b>

🇺🇸 <b>NASDAQ</b> ({len(NASDAQ_LIST)} hisse)
{', '.join(NASDAQ_LIST)}

🇹🇷 <b>BIST</b> ({len(BIST_LIST)} hisse)
{', '.join([x.replace('.IS','') for x in BIST_LIST])}

🇩🇪 <b>ALMAN</b> ({len(ALMAN_LIST)} hisse)
{', '.join([x.replace('.DE','') for x in ALMAN_LIST])}"""
        send_telegram(msg, chat_id)
        return

    # TARAMA KOMUTLARI
    for cmd, market, market_list in [
        ('/nasdaq', 'nasdaq', NASDAQ_LIST),
        ('/bist', 'bist', BIST_LIST),
        ('/alman', 'alman', ALMAN_LIST),
        ('/german', 'alman', ALMAN_LIST),
        ('/amerika', 'nasdaq', NASDAQ_LIST),
        ('/abd', 'nasdaq', NASDAQ_LIST),
        ('/turkiye', 'bist', BIST_LIST),
        ('/istanbul', 'bist', BIST_LIST),
        ('/almanya', 'alman', ALMAN_LIST),
        ('/dax', 'alman', ALMAN_LIST),
    ]:
        if t.startswith(cmd):
            rest = t[len(cmd):].strip()
            if any(x in rest for x in ['tara', 'scan', 'tarat']):
                send_telegram(f"🔍 {market.upper()} taraması başladı...", chat_id)
                def do_scan(ml=market_list, m=market, cid=chat_id):
                    found = 0
                    for ticker in ml:
                        signal = analyze_stock(ticker, m)
                        if signal:
                            send_telegram(signal, cid)
                            found += 1
                            time.sleep(2)
                    if found == 0:
                        send_telegram(f"Şu an {m.upper()} için uygun setup bulunamadı.", cid)
                threading.Thread(target=do_scan).start()
            elif rest:
                ticker_raw = rest.upper().split()[0]
                ticker = format_ticker(ticker_raw, market)
                send_telegram(f"🔍 {ticker_raw} analiz ediliyor...", chat_id)
                def do_single(t=ticker, m=market, cid=chat_id, tr=ticker_raw):
                    signal = analyze_stock(t, m)
                    if signal:
                        send_telegram(signal, cid)
                    else:
                        send_telegram(f"{tr} için şu an uygun setup yok veya veri alınamadı.", cid)
                threading.Thread(target=do_single).start()
            return

    # UYUMSUZLUK
    if any(t.startswith(x) for x in ['/uyumsuzluk', '/divergence']):
        rest = t.split(None, 1)[1] if len(t.split()) > 1 else ''
        market = detect_market(rest) or 'nasdaq'
        market_list = get_market_list(market)
        send_telegram(f"⚡ {market.upper()} uyumsuzluk taraması başladı...", chat_id)
        def do_div(ml=market_list, cid=chat_id, m=market):
            found = 0
            for ticker in ml:
                result = check_divergence(ticker)
                if result:
                    send_telegram(result, cid)
                    found += 1
                    time.sleep(1)
            if found == 0:
                send_telegram(f"{m.upper()} için uyumsuzluk tespit edilemedi.", cid)
        threading.Thread(target=do_div).start()
        return

    # ANALİZ ET
    if any(t.startswith(x) for x in ['/analizet', '/analiz et', '/analyze', '/analysis']):
        parts = original.split(None, 1)
        if len(parts) < 2:
            send_telegram("Kullanım: /analizet [HİSSE veya HABER_NO]", chat_id)
            return
        query = parts[1].strip()

        # Haber no mu?
        if '/' in query and re.match(r'\d{6}/\d+', query):
            if query in news_archive:
                n = news_archive[query]
                send_telegram(f"""📊 <b>HABER ANALİZİ — {query}</b>
Tarih: {n['date']}
Borsa: {n['market'].upper()}
Başlık: {n['title']}
Duygu: {n['sentiment']}

{n['content']}""", chat_id)
            else:
                send_telegram(f"{query} numaralı haber bulunamadı.", chat_id)
            return

        # Hisse analizi
        ticker_raw = query.upper().split()[0]
        market = detect_market(query) or 'nasdaq'
        ticker = format_ticker(ticker_raw, market)
        send_telegram(f"🔍 {ticker_raw} analiz ediliyor...", chat_id)
        def do_analyze(t=ticker, m=market, cid=chat_id, tr=ticker_raw):
            signal = analyze_stock(t, m)
            if signal:
                send_telegram(signal, cid)
            else:
                send_telegram(f"{tr} için şu an uygun setup yok.", cid)
        threading.Thread(target=do_analyze).start()
        return

    # ÖZET / SUMMARY
    if any(t.startswith(x) for x in ['/ozet', '/özet', '/summary']):
        parts = original.split(None, 1)
        query = parts[1].strip() if len(parts) > 1 else ''
        market = detect_market(query) or 'tum'
        send_telegram(f"📊 {market.upper()} özet hazırlanıyor... (yakında)", chat_id)
        return

    # HABERLER
    if any(t.startswith(x) for x in ['/haberler', '/haber', '/news']):
        rest = t.split(None, 1)[1] if len(t.split()) > 1 else ''

        # Tüm haberler (haberlerhepsi / newsall)
        if any(x in t for x in ['hepsi', 'all', 'tekrar', 'repeat']):
            date_match = re.search(r'\d{6}', rest)
            date_str = date_match.group() if date_match else datetime.now().strftime('%d%m%y')
            matching = {k: v for k, v in news_archive.items() if k.startswith(date_str)}
            if matching:
                msg = f"📰 <b>{date_str} TÜM HABERLERİ</b>\n──────────────────────────\n"
                for nid, n in matching.items():
                    msg += f"\n{nid} — {n['title']}\n{n['sentiment']} — {n['hours_ago']} saat önce\n"
                send_telegram(msg, chat_id)
            else:
                send_telegram(f"{date_str} tarihine ait haber bulunamadı.", chat_id)
            return

        market = detect_market(rest) or 'nasdaq'
        send_telegram(f"📰 {market.upper()} haberleri aranıyor... (gerçek zamanlı haber entegrasyonu yakında eklenecek)", chat_id)
        return

    # ALARMLAR
    if t.startswith('/alarm') or t.startswith('/alert'):
        parts = original.split()

        # Alarm sil
        if any(x in t for x in ['sil', 'delete', 'kaldir', 'remove']):
            if len(parts) >= 3:
                ticker = parts[-1].upper()
                if ticker in alerts:
                    del alerts[ticker]
                    send_telegram(f"✅ {ticker} alarmı silindi.", chat_id)
                else:
                    send_telegram(f"{ticker} için alarm bulunamadı.", chat_id)
            return

        # Alarmlarım
        if any(t.startswith(x) for x in ['/alarmlarim', '/alerts']):
            if alerts:
                msg = "🔔 <b>ALARMLARIM</b>\n──────────────────────────\n"
                for ticker, data in alerts.items():
                    msg += f"{ticker} → ${data['price']}\n"
                send_telegram(msg, chat_id)
            else:
                send_telegram("Aktif alarm yok.", chat_id)
            return

        # Alarm ekle
        if len(parts) >= 3:
            ticker = parts[1].upper()
            try:
                price = float(parts[2])
                alerts[ticker] = {'price': price, 'chat_id': chat_id}
                send_telegram(f"🔔 Alarm kuruldu: {ticker} → ${price}", chat_id)
            except:
                send_telegram("Kullanım: /alarm [HİSSE] [FİYAT]", chat_id)
        return

    # PORTFÖY
    if any(t.startswith(x) for x in ['/portfoy', '/portföy', '/portfolio']):
        parts = original.split()

        # Ekle
        if any(x in t for x in ['ekle', 'add']):
            if len(parts) >= 5:
                try:
                    ticker = parts[2].upper()
                    price = float(parts[3])
                    qty = float(parts[4])
                    portfolio[ticker] = {'price': price, 'qty': qty, 'chat_id': chat_id}
                    total = price * qty
                    send_telegram(f"✅ Portföye eklendi:\n{ticker} — {qty} adet @ ${price}\nToplam: ${total:.2f}", chat_id)
                except:
                    send_telegram("Kullanım: /portfoy ekle [HİSSE] [FİYAT] [ADET]", chat_id)
            return

        # Tek hisse
        if len(parts) >= 2 and not any(x in t for x in ['ekle', 'add']):
            ticker = parts[1].upper()
            if ticker in portfolio:
                p = portfolio[ticker]
                try:
                    current = yf.Ticker(ticker).fast_info['last_price']
                    pnl = (current - p['price']) * p['qty']
                    pnl_pct = ((current - p['price']) / p['price']) * 100
                    send_telegram(f"""💼 <b>PORTFÖY — {ticker}</b>
Alış: ${p['price']} x {p['qty']} adet
Güncel: ${current:.2f}
Kar/Zarar: ${pnl:.2f} (%{pnl_pct:.2f})""", chat_id)
                except:
                    send_telegram(f"{ticker} için güncel fiyat alınamadı.", chat_id)
            else:
                send_telegram(f"{ticker} portföyde bulunamadı.", chat_id)
            return

        # Tüm portföy
        if portfolio:
            msg = "💼 <b>PORTFÖYÜM</b>\n──────────────────────────\n"
            total_pnl = 0
            for ticker, p in portfolio.items():
                try:
                    current = yf.Ticker(ticker).fast_info['last_price']
                    pnl = (current - p['price']) * p['qty']
                    total_pnl += pnl
                    msg += f"{ticker}: ${current:.2f} | K/Z: ${pnl:.2f}\n"
                except:
                    msg += f"{ticker}: Fiyat alınamadı\n"
            msg += f"\nToplam K/Z: ${total_pnl:.2f}"
            send_telegram(msg, chat_id)
        else:
            send_telegram("Portföy boş.", chat_id)
        return

    # TAKİP
    if any(t.startswith(x) for x in ['/takip', '/track']):
        parts = original.split()

        if any(x in t for x in ['takiplerim', 'mytracks']):
            if tracked:
                msg = "📌 <b>TAKİP LİSTEM</b>\n──────────────────────────\n"
                for ticker, data in tracked.items():
                    msg += f"{ticker} — Giriş: ${data['entry']} Stop: ${data['stop']} Hedef: ${data['target']}\n"
                send_telegram(msg, chat_id)
            else:
                send_telegram("Takip listesi boş.", chat_id)
            return

        if len(parts) >= 5:
            try:
                ticker = parts[1].upper()
                entry = float(parts[2].replace('giris', '').replace('entry', ''))
                stop = float(parts[3].replace('stop', ''))
                target = float(parts[4].replace('hedef', '').replace('target', ''))
                tracked[ticker] = {'entry': entry, 'stop': stop, 'target': target, 'chat_id': chat_id}
                send_telegram(f"📌 Takibe alındı: {ticker}\nGiriş: ${entry} | Stop: ${stop} | Hedef: ${target}", chat_id)
            except:
                send_telegram("Kullanım: /takip [HİSSE] [GİRİŞ] [STOP] [HEDEF]", chat_id)
        return

    # Tanınmayan komut
    send_telegram("Komut tanınamadı. /yardim veya /help yazabilirsin.", chat_id)

# =====================
# TELEGRAM POLLING
# =====================

def telegram_polling():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 30, "offset": offset}
            response = requests.get(url, params=params, timeout=35)
            data = response.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    text = message.get("text", "")
                    chat_id = message.get("chat", {}).get("id")

                    if text and chat_id and text.startswith("/"):
                        threading.Thread(
                            target=handle_command,
                            args=(text, str(chat_id))
                        ).start()
        except:
            pass
        time.sleep(1)

# =====================
# SEANS SİSTEMİ
# =====================

def is_nasdaq_session():
    now = datetime.utcnow()
    hour = now.hour
    # Pre-market: 12:00-13:30 UTC (15:00-16:30 TR)
    # Regular: 13:30-20:00 UTC (16:30-23:00 TR)
    # After-hours: 20:00-23:00 UTC (23:00-02:00 TR)
    return 12 <= hour <= 23

def is_bist_session():
    now = datetime.utcnow()
    hour = now.hour
    minute = now.minute
    # BIST: 07:00-15:00 UTC (10:00-18:00 TR)
    # Öğle arası: 09:30-11:00 UTC (12:30-14:00 TR)
    if 7 <= hour < 15:
        if hour == 9 and minute >= 30:
            return False
        if hour == 10:
            return False
        if hour == 11 and minute == 0:
            return False
        return True
    return False

def get_session_type():
    now = datetime.utcnow()
    hour = now.hour
    if 12 <= hour < 13:
        return 'nasdaq_premarket'
    elif 13 <= hour < 20:
        return 'nasdaq_regular'
    elif 20 <= hour <= 23:
        return 'nasdaq_afterhours'
    return None

def auto_scan_loop():
    send_telegram("🦅 <b>Hawk Signal Bot v2.0 Başladı!</b>\n\n✅ 4 Katmanlı Filtre\n✅ 3 Borsa\n✅ Seans Sistemi\n✅ Manuel Komutlar Aktif")

    last_bist_premarket = None
    last_bist_lunch = None

    while True:
        try:
            now = datetime.utcnow()
            hour = now.hour

            # BIST açılış öncesi (09:00 TR = 06:00 UTC)
            if hour == 6 and last_bist_premarket != now.date():
                last_bist_premarket = now.date()
                send_telegram("🇹🇷 <b>BIST Açılış Öncesi Tarama</b>\nPiyasa 1 saat sonra açılıyor...")
                for ticker in BIST_LIST[:10]:
                    signal = analyze_stock(ticker, 'bist')
                    if signal:
                        send_telegram(signal)
                        time.sleep(2)

            # BIST öğle arası (12:30 TR = 09:30 UTC)
            if hour == 9 and now.minute == 30 and last_bist_lunch != now.date():
                last_bist_lunch = now.date()
                send_telegram("🇹🇷 <b>BIST Öğle Arası Özeti</b>\nSabah seansı tamamlandı. Öğleden sonra için izleme listesi hazırlanıyor...")

            # NASDAQ regular session taraması (her 5 dakika)
            if is_nasdaq_session():
                session = get_session_type()
                for ticker in NASDAQ_LIST:
                    signal = analyze_stock(ticker, 'nasdaq')
                    if signal:
                        if session == 'nasdaq_afterhours':
                            signal = "⚠️ <b>AFTER-HOURS SİNYALİ</b> (Ertesi gün için)\n" + signal
                        send_telegram(signal)
                        time.sleep(2)
                    time.sleep(0.5)

            # BIST regular session taraması
            if is_bist_session():
                for ticker in BIST_LIST:
                    signal = analyze_stock(ticker, 'bist')
                    if signal:
                        send_telegram(signal)
                        time.sleep(2)
                    time.sleep(0.5)

            # Takip edilen hisseleri kontrol et
            for ticker, data in tracked.items():
                try:
                    current = yf.Ticker(ticker).fast_info['last_price']
                    if current <= data['stop']:
                        send_telegram(f"🛑 <b>STOP UYARISI — {ticker}</b>\nFiyat ${current:.2f} — Stop seviyesi ${data['stop']} kırıldı!", data['chat_id'])
                    elif current >= data['target']:
                        send_telegram(f"🎯 <b>HEDEF UYARISI — {ticker}</b>\nFiyat ${current:.2f} — Hedef ${data['target']} seviyesine ulaştı!", data['chat_id'])
                except:
                    pass

            # Alarm kontrolü
            for ticker, data in list(alerts.items()):
                try:
                    current = yf.Ticker(ticker).fast_info['last_price']
                    if current >= data['price']:
                        send_telegram(f"🔔 <b>ALARM — {ticker}</b>\nFiyat ${current:.2f} → Hedef ${data['price']} seviyesine ulaştı!", data['chat_id'])
                        del alerts[ticker]
                except:
                    pass

        except:
            pass

        time.sleep(300)

# =====================
# FLASK ROUTES
# =====================

@app.route("/")
def home():
    return jsonify({"status": "Hawk Signal Bot v2.0 🦅", "nasdaq": len(NASDAQ_LIST), "bist": len(BIST_LIST), "alman": len(ALMAN_LIST)})

@app.route("/test")
def test():
    send_telegram("🦅 <b>Hawk Signal Bot v2.0 Aktif!</b>\n\n✅ NASDAQ, BIST, Alman Borsası\n✅ 3 Seans Sistemi\n✅ Manuel Komutlar\n✅ Portföy ve Alarm Takibi")
    return jsonify({"status": "Test mesajı gönderildi!"})

# =====================
# WEBHOOK ROUTE
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
    url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not url:
        return jsonify({"error": "RAILWAY_PUBLIC_DOMAIN not set"})
    webhook_url = f"https://{url}/webhook"
    r = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        params={"url": webhook_url}
    )
    return jsonify(r.json())

def start_background():
    threading.Thread(target=auto_scan_loop, daemon=True).start()

# Başlat
start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
