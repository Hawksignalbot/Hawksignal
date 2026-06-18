import os
import time
import requests
import threading
import random
from flask import Flask, jsonify, request
from datetime import datetime
import pandas as pd
import numpy as np
import re

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TD_API_KEY = os.environ.get("TD_API_KEY")

# =====================
# YEDEK HAVUZLAR
# =====================

NASDAQ_BACKUP = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO","QCOM",
    "INTC","MU","AMAT","KLAC","MRVL","ORCL","CRM","NOW","SNOW","PLTR",
    "DDOG","NET","CRWD","ZS","PANW","SHOP","NFLX","UBER","PYPL","COIN",
    "MRNA","BNTX","ENPH","FSLR","RIVN","RBLX","U","IONQ","TTD","SOFI",
    "AFRM","UPST","HOOD","SQ","MSTR","RIOT","MARA","ADBE","CSCO","PEP",
    "COST","SBUX","GILD","REGN","VRTX","BIIB","ISRG","IDXX","ALGN","DXCM",
    "ATVI","EA","TTWO","MTTR","QUBT","RGTI","ARQQ","SOUN","CLOV","WKHS",
    "LCID","NIO","LI","XPEV","RIVN","FSR","GOEV","BLNK","CHPT","PLUG"
]

BIST_BACKUP = [
    "THYAO","GARAN","ASELS","KCHOL","EREGL","BIMAS","AKBNK","YKBNK",
    "TUPRS","SISE","PGSUS","TAVHL","TOASO","FROTO","SAHOL","HALKB",
    "VAKBN","TCELL","TTKOM","EKGYO","PETKM","ARCLK","VESTL","LOGO",
    "NETAS","DOAS","OTKAR","ULKER","CCOLA","AEFES","BRISA","GUBRF",
    "KOZAL","KRDMD","ISCTR","ALBRK","TSKB","KLNMA","MPARK","MGROS"
]

ALMAN_BACKUP = [
    "SAP","SIE","ALV","MRK","BAYN","BASF","BMW","MBG","VOW3","DTE",
    "DBK","CBK","BAS","EOAN","RWE","ADS","LIN","MUV2","HEI","FRE",
    "HEN3","SHL","ZAL","DHER","QIA","MTX","HFG","NDX1","TUI1","LEG"
]

# Bellek
portfolio = {}
alerts = {}
tracked = {}
news_archive = {}
news_counter = {}

# =====================
# YARDIMCI
# =====================

def normalize_text(text):
    rep = {
        'İ':'i','I':'i','ı':'i','Ü':'u','ü':'u','Ö':'o','ö':'o',
        'Ş':'s','ş':'s','Ğ':'g','ğ':'g','Ç':'c','ç':'c','Â':'a','â':'a'
    }
    text = text.lower()
    for k,v in rep.items():
        text = text.replace(k.lower(),v).replace(k,v)
    return text

def detect_market(text):
    t = normalize_text(text)
    if any(w in t for w in ['nasdaq','amerika','abd','us','usa','america']):
        return 'nasdaq'
    if any(w in t for w in ['bist','turkiye','turkey','istanbul','ist']):
        return 'bist'
    if any(w in t for w in ['alman','almanya','dax','german','germany']):
        return 'alman'
    return None

def send_telegram(message, chat_id=None):
    cid = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# =====================
# 5. KADEME: DİNAMİK HACİM FİLTRESİ
# =====================

def get_top_volume_stocks(market, top_n=100, select_n=30):
    """
    Twelve Data'dan o günün en hacimli hisselerini çek,
    bunlardan rastgele select_n tane seç.
    Başarısız olursa yedek havuzdan rastgele seç.
    """
    try:
        if market == 'nasdaq':
            exchange = 'NASDAQ'
        elif market == 'bist':
            exchange = 'BIST'
        elif market == 'alman':
            exchange = 'XETRA'
        else:
            exchange = 'NASDAQ'

        url = "https://api.twelvedata.com/stocks"
        params = {
            "exchange": exchange,
            "apikey": TD_API_KEY,
            "format": "JSON"
        }
        r = requests.get(url, params=params, timeout=15)
        data = r.json()

        if "data" not in data or not data["data"]:
            raise Exception("No data")

        symbols = [item["symbol"] for item in data["data"] if item.get("symbol")]

        if len(symbols) > top_n:
            symbols = symbols[:top_n]

        if len(symbols) < select_n:
            select_n = len(symbols)

        selected = random.sample(symbols, select_n)
        return selected

    except:
        # Yedek havuzdan rastgele seç
        backup = {
            'nasdaq': NASDAQ_BACKUP,
            'bist': BIST_BACKUP,
            'alman': ALMAN_BACKUP
        }.get(market, NASDAQ_BACKUP)
        return random.sample(backup, min(select_n, len(backup)))

# =====================
# TWELVE DATA VERİ
# =====================

def td_get_ohlcv(symbol, market, outputsize=100):
    try:
        clean_symbol = symbol.replace('.IS','').replace('.XETRA','').replace('.DE','')

        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": clean_symbol,
            "interval": "1day",
            "outputsize": outputsize,
            "apikey": TD_API_KEY,
            "format": "JSON"
        }
        if market == 'bist':
            params["exchange"] = "BIST"
        elif market == 'alman':
            params["exchange"] = "XETRA"

        r = requests.get(url, params=params, timeout=15)
        data = r.json()

        if data.get("status") == "error" or "values" not in data:
            return None

        values = data["values"]
        df = pd.DataFrame(values)
        df = df.rename(columns={
            "datetime":"Date","open":"Open","high":"High",
            "low":"Low","close":"Close","volume":"Volume"
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        for col in ["Open","High","Low","Close","Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df, clean_symbol

    except:
        return None

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

# =====================
# ANA ANALİZ (4 KADEME)
# =====================

def analyze_stock(symbol, market):
    try:
        result = td_get_ohlcv(symbol, market, outputsize=210)
        if result is None:
            return None
        df, clean_symbol = result

        if len(df) < 60:
            return None

        close = df['Close']
        volume = df['Volume']

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

        # KADEME 1: TREND
        trend_sma200 = "✅ Yukarıda" if price > sma200_now else "⚠️ Aşağıda"
        trend_ema20 = "✅ Yukarıda" if price > ema20_now else "⚠️ Aşağıda"
        trend_sma50 = "✅ Yukarıda" if price > sma50_now else "⚠️ Aşağıda"
        resistance_risk = (
            (abs(price - sma50_now) / price < 0.02 and price < sma50_now) or
            (abs(price - sma200_now) / price < 0.02 and price < sma200_now)
        )

        # KADEME 2: TEKNİK
        rsi_signal = rsi_now > rsi_prev and 40 < rsi_now < 65
        macd_crossover = macd_prev_val < macd_sig_prev and macd_now > macd_sig_now
        macd_positive = macd_now > macd_sig_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0

        # KADEME 3: HACİM (5. kademe zaten hisse seçiminde uygulandı)
        vol_ok = vol_ratio >= 1.2

        # KADEME 4: SKOR
        score = 0
        if price > sma200_now: score += 2
        if price > sma50_now: score += 1
        if price > ema20_now: score += 1
        if macd_crossover: score += 3
        elif macd_positive: score += 1
        if rsi_signal: score += 2
        if vol_ok: score += 2
        if price < sma200_now: score -= 2
        if resistance_risk: score -= 2

        if score < 6:
            return None

        # RİSK
        risk_score = max(10, min(90, 100 - (score * 10)))
        risk_label = "Düşük 🟢" if risk_score < 30 else "Orta 🟡" if risk_score < 60 else "Yüksek 🔴"

        # ÇIKIŞ STRATEJİSİ
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

        if score >= 8 and rr >= 2:
            karar = "💚 İŞLEME GİRİLEBİLİR"
        elif score >= 6:
            karar = "🟡 İZLEMEDE KALSIN"
        else:
            karar = "🔴 GEÇ"

        currency = "₺" if market == 'bist' else "€" if market == 'alman' else "$"

        msg = f"""
🚨 <b>{clean_symbol} - POTANSİYEL SİNYAL</b>
──────────────────────────
📈 Giriş: <b>{currency}{entry:.2f}</b>
🎯 Kar Al (%5): <b>{currency}{take_profit:.2f}</b>
🛑 Zarar Kes: <b>{currency}{stop_loss:.2f}</b>
⚖️ Risk/Ödül: <b>1:{rr}</b>

📊 <b>Teknik Durum:</b>
• SMA200: {trend_sma200}
• EMA20 / SMA50: {trend_ema20} / {trend_sma50}
• RSI: {rsi_text}
• MACD: {macd_text}
• Hacim: {vol_text}

⚠️ <b>Risk:</b>
• Direnç: {"⚠️ Kritik seviye yakın" if resistance_risk else "✅ Temiz"}
• Risk Skoru: %{risk_score} - {risk_label}

💡 <b>Karar:</b> {karar}
⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
        return msg

    except:
        return None

# =====================
# UYUMSUZLUK
# =====================

def check_divergence(symbol, market):
    try:
        result = td_get_ohlcv(symbol, market, outputsize=30)
        if result is None:
            return None
        df, clean_symbol = result
        if len(df) < 15:
            return None

        close = df['Close']
        rsi = calc_rsi(close)
        prices = close.values[-10:]
        rsi_vals = rsi.values[-10:]
        div_time = datetime.now().strftime('%H:%M')

        price_low_idx = int(np.argmin(prices[:-1]))
        price_high_idx = int(np.argmax(prices[:-1]))

        bull_div = prices[-1] < prices[price_low_idx] and rsi_vals[-1] > rsi_vals[price_low_idx]
        bear_div = prices[-1] > prices[price_high_idx] and rsi_vals[-1] < rsi_vals[price_high_idx]

        if bull_div:
            return f"⚡ <b>BOĞA UYUMSUZLUĞU — {clean_symbol}</b>\nTür: Al Sinyali\nBaşlangıç: {div_time}\nFiyat yeni dip yaparken RSI yükseliyor\nRSI: {rsi_vals[-1]:.1f}"
        if bear_div:
            return f"⚡ <b>AYI UYUMSUZLUĞU — {clean_symbol}</b>\nTür: Sat Sinyali\nBaşlangıç: {div_time}\nFiyat yeni zirve yaparken RSI düşüyor\nRSI: {rsi_vals[-1]:.1f}"
        return None
    except:
        return None

# =====================
# KOMUT İŞLEYİCİ
# =====================

def handle_command(text, chat_id):
    t = normalize_text(text.strip())

    if any(t.startswith(x) for x in ['/start','/durum','/status']):
        send_telegram("""🦅 <b>HAWK SIGNAL BOT v3.0</b>
──────────────────────────
/durum — /status
/yardim — /help
/liste — /list
──────────────────────────
📈 TARAMA | ⚡ UYUMSUZLUK
📌 TAKİP | 📊 ANALİZ
📰 HABERLER | 🔔 ALARMLAR | 💼 PORTFÖY
──────────────────────────
Detay için /yardim veya /help""", chat_id)
        return

    if any(t.startswith(x) for x in ['/yardim','/yardım','/komutlar','/komut']):
        send_telegram("""📋 <b>KOMUTLAR</b>

/durum — Sistem durumu
/liste — Hisse havuzu bilgisi
──────────────────────────
📈 <b>TARAMA</b>
/nasdaq tara | /bist tara | /alman tara
/abd tara | /turkiye tara | /almanya tara
/nasdaq [HİSSE] | /bist [HİSSE] | /alman [HİSSE]

⚡ <b>UYUMSUZLUK</b>
/uyumsuzluk nasdaq|bist|alman

📌 <b>TAKİP</b>
/takip [hisse] [giris] [stop] [hedef]
/takiplerim

📊 <b>ANALİZ</b>
/analizet [hisse veya haber no]
/analiz et [hisse veya haber no]

📰 <b>HABERLER</b>
/haberler nasdaq|bist|alman
/haberlerhepsi [tarih]
/ozet [borsa] [tarih]

🔔 <b>ALARMLAR</b>
/alarm [hisse] [fiyat]
/alarmlarim | /alarm sil [hisse]

💼 <b>PORTFÖY</b>
/portfoy ekle [hisse] [fiyat] [adet]
/portfoy | /portfoy [hisse]""", chat_id)
        return

    if t.startswith('/help'):
        send_telegram("""📋 <b>COMMANDS</b>

/status — System status
/list — Watchlist info
──────────────────────────
📈 <b>SCAN</b>
/nasdaq scan | /bist scan | /german scan
/nasdaq [TICKER] | /bist [TICKER] | /german [TICKER]

⚡ <b>DIVERGENCE</b>
/divergence nasdaq|bist|german

📌 <b>TRACKING</b>
/track [ticker] [entry] [stop] [target]
/mytracks

📊 <b>ANALYSIS</b>
/analyze [ticker or news id]
/analysis [ticker or news id]

📰 <b>NEWS</b>
/news nasdaq|bist|german
/newsall [date] | /summary [market] [date]

🔔 <b>ALERTS</b>
/alert [ticker] [price]
/alerts | /alert delete [ticker]

💼 <b>PORTFOLIO</b>
/portfolio add [ticker] [price] [qty]
/portfolio | /portfolio [ticker]""", chat_id)
        return

    if any(t.startswith(x) for x in ['/liste','/list']):
        send_telegram("""📋 <b>HİSSE HAVUZU</b>

🇺🇸 NASDAQ: 70+ hisse havuzu
🇹🇷 BIST: 40 hisse havuzu
🇩🇪 Alman: 30 hisse havuzu

Her taramada:
1️⃣ O günün en hacimli hisseleri belirlenir
2️⃣ Bunlardan rastgele 30 hisse seçilir
3️⃣ 5 katmanlı analizden geçirilir

Her tarama farklı hisselerle yapılır.""", chat_id)
        return

    # TARAMA KOMUTLARI
    market_map = {
        '/nasdaq':'nasdaq','/abd':'nasdaq','/amerika':'nasdaq','/us':'nasdaq','/america':'nasdaq',
        '/bist':'bist','/turkiye':'bist','/istanbul':'bist','/turkey':'bist',
        '/alman':'alman','/almanya':'alman','/dax':'alman','/german':'alman','/germany':'alman'
    }

    for cmd, market in market_map.items():
        if t.startswith(cmd):
            rest = t[len(cmd):].strip()

            if any(x in rest for x in ['tara','scan','tarat']):
                send_telegram(f"🔍 {market.upper()} taraması başladı...\n5. kademe: Hacim filtresi uygulanıyor", chat_id)
                def do_scan(m=market, cid=chat_id):
                    batch = get_top_volume_stocks(m, top_n=100, select_n=30)
                    found = 0
                    for ticker in batch:
                        signal = analyze_stock(ticker, m)
                        if signal:
                            send_telegram(signal, cid)
                            found += 1
                            time.sleep(2)
                        time.sleep(1.2)
                    if found == 0:
                        send_telegram(f"{m.upper()} taraması tamamlandı. Şu an uygun setup bulunamadı.", cid)
                threading.Thread(target=do_scan).start()

            elif rest:
                ticker = rest.upper().split()[0]
                send_telegram(f"🔍 {ticker} analiz ediliyor...", chat_id)
                def do_single(tk=ticker, m=market, cid=chat_id):
                    signal = analyze_stock(tk, m)
                    if signal:
                        send_telegram(signal, cid)
                    else:
                        send_telegram(f"{tk} için şu an uygun setup yok.", cid)
                threading.Thread(target=do_single).start()
            return

    # UYUMSUZLUK
    if any(t.startswith(x) for x in ['/uyumsuzluk','/divergence']):
        parts = t.split(None,1)
        query = parts[1] if len(parts) > 1 else ''
        market = detect_market(query) or 'nasdaq'
        send_telegram(f"⚡ {market.upper()} uyumsuzluk taraması başladı...", chat_id)
        def do_div(m=market, cid=chat_id):
            batch = get_top_volume_stocks(m, top_n=50, select_n=20)
            found = 0
            for ticker in batch:
                result = check_divergence(ticker, m)
                if result:
                    send_telegram(result, cid)
                    found += 1
                    time.sleep(1)
                time.sleep(1)
            if found == 0:
                send_telegram(f"{m.upper()} için uyumsuzluk tespit edilemedi.", cid)
        threading.Thread(target=do_div).start()
        return

    # ANALİZ
    if any(t.startswith(x) for x in ['/analizet','/analiz','/analyze','/analysis']):
        parts = text.split(None,1)
        if len(parts) < 2:
            send_telegram("Kullanım: /analizet [HİSSE]", chat_id)
            return
        query = parts[1].strip()

        if re.match(r'\d{6}/\d+', query):
            if query in news_archive:
                n = news_archive[query]
                send_telegram(f"📊 <b>{query}</b>\n{n['title']}\n{n['content']}", chat_id)
            else:
                send_telegram(f"{query} numaralı haber bulunamadı.", chat_id)
            return

        ticker = query.upper().split()[0]
        market = detect_market(query) or 'nasdaq'
        send_telegram(f"🔍 {ticker} analiz ediliyor...", chat_id)
        def do_analyze(tk=ticker, m=market, cid=chat_id):
            signal = analyze_stock(tk, m)
            if signal:
                send_telegram(signal, cid)
            else:
                send_telegram(f"{tk} için şu an uygun setup yok.", cid)
        threading.Thread(target=do_analyze).start()
        return

    # HABERLER
    if any(t.startswith(x) for x in ['/haberler','/haber','/news']):
        parts = t.split(None,1)
        query = parts[1] if len(parts) > 1 else ''
        market = detect_market(query) or 'nasdaq'

        if any(x in t for x in ['hepsi','all','tekrar','repeat']):
            date_match = re.search(r'\d{6}', query)
            date_str = date_match.group() if date_match else datetime.now().strftime('%d%m%y')
            matching = {k:v for k,v in news_archive.items() if k.startswith(date_str)}
            if matching:
                msg = f"📰 <b>{date_str} TÜM HABERLERİ</b>\n"
                for nid, n in matching.items():
                    msg += f"\n{nid} — {n['title']}\n{n['sentiment']} — {n['hours_ago']} saat önce\n"
                send_telegram(msg, chat_id)
            else:
                send_telegram(f"{date_str} tarihine ait haber bulunamadı.", chat_id)
            return

        send_telegram(f"📰 {market.upper()} haber entegrasyonu yakında aktif olacak.", chat_id)
        return

    # ÖZET / SUMMARY
    if any(t.startswith(x) for x in ['/ozet','/özet','/summary']):
        send_telegram("📊 Özet özelliği yakında aktif olacak.", chat_id)
        return

    # ALARMLAR
    if any(t.startswith(x) for x in ['/alarm','/alert']):
        parts = text.split()

        if any(t.startswith(x) for x in ['/alarmlarim','/alerts']):
            if alerts:
                msg = "🔔 <b>ALARMLARIM</b>\n"
                for ticker, data in alerts.items():
                    msg += f"{ticker} → {data['price']}\n"
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
                    send_telegram(f"✅ {ticker} — {qty} adet @ {price} portföye eklendi.", chat_id)
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
            msg = "💼 <b>PORTFÖYÜM</b>\n"
            for ticker, p in portfolio.items():
                msg += f"{ticker}: {p['price']} x {p['qty']}\n"
            send_telegram(msg, chat_id)
        else:
            send_telegram("Portföy boş.", chat_id)
        return

    # TAKİP
    if any(t.startswith(x) for x in ['/takip','/track']):
        parts = text.split()

        if any(x in t for x in ['takiplerim','mytracks']):
            if tracked:
                msg = "📌 <b>TAKİP LİSTEM</b>\n"
                for ticker, data in tracked.items():
                    msg += f"{ticker} — Giriş:{data['entry']} Stop:{data['stop']} Hedef:{data['target']}\n"
                send_telegram(msg, chat_id)
            else:
                send_telegram("Takip listesi boş.", chat_id)
            return

        if len(parts) >= 5:
            try:
                ticker = parts[1].upper()
                entry = float(parts[2])
                stop = float(parts[3])
                target = float(parts[4])
                tracked[ticker] = {'entry': entry, 'stop': stop, 'target': target, 'chat_id': chat_id}
                send_telegram(f"📌 {ticker} takibe alındı.\nGiriş:{entry} Stop:{stop} Hedef:{target}", chat_id)
            except:
                send_telegram("Kullanım: /takip [HİSSE] [GİRİŞ] [STOP] [HEDEF]", chat_id)
        return

    send_telegram("Komut tanınamadı. /yardim veya /help yazabilirsin.", chat_id)

# =====================
# WEBHOOK
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
    webhook_url = f"https://{domain}/webhook"
    r = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        params={"url": webhook_url}
    )
    return jsonify(r.json())

@app.route("/")
def home():
    return jsonify({"status": "Hawk Signal Bot v3.0 🦅", "version": "5-kademe-filtre"})

@app.route("/test")
def test():
    send_telegram("🦅 <b>Hawk Signal Bot v3.0 Aktif!</b>\n\n✅ 5 Katmanlı Filtre\n✅ Dinamik Hacim Seçimi\n✅ Twelve Data entegrasyonu\n✅ NASDAQ, BIST, Alman")
    return jsonify({"status": "Test mesajı gönderildi!"})

# =====================
# OTOMATİK TARAMA
# =====================

def is_nasdaq_hours():
    now = datetime.utcnow()
    return 12 <= now.hour <= 23

def is_bist_hours():
    now = datetime.utcnow()
    h, m = now.hour, now.minute
    if 7 <= h < 15:
        if (h == 9 and m >= 30) or h == 10 or (h == 11 and m == 0):
            return False
        return True
    return False

def auto_scan_loop():
    time.sleep(15)
    send_telegram("🦅 <b>Hawk Signal Bot v3.0 Başladı!</b>\n\n✅ 5 Kademe Filtre Aktif\n✅ Dinamik hacim tabanlı hisse seçimi\n✅ 25 dakikada bir otomatik tarama")

    while True:
        try:
            if is_nasdaq_hours():
                batch = get_top_volume_stocks('nasdaq', 100, 30)
                for ticker in batch:
                    signal = analyze_stock(ticker, 'nasdaq')
                    if signal:
                        send_telegram(signal)
                        time.sleep(3)
                    time.sleep(1.2)

            if is_bist_hours():
                batch = get_top_volume_stocks('bist', 40, 15)
                for ticker in batch:
                    signal = analyze_stock(ticker, 'bist')
                    if signal:
                        send_telegram(signal)
                        time.sleep(3)
                    time.sleep(1.2)

            for ticker, data in list(tracked.items()):
                try:
                    result = td_get_ohlcv(ticker, 'nasdaq', 5)
                    if result:
                        df, _ = result
                        current = float(df['Close'].iloc[-1])
                        if current <= data['stop']:
                            send_telegram(f"🛑 <b>STOP — {ticker}</b>\n${current:.2f} → Stop ${data['stop']}", data['chat_id'])
                        elif current >= data['target']:
                            send_telegram(f"🎯 <b>HEDEF — {ticker}</b>\n${current:.2f} → Hedef ${data['target']}", data['chat_id'])
                except:
                    pass

        except:
            pass

        time.sleep(1500)

threading.Thread(target=auto_scan_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
