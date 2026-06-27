import os
import time
import requests
import threading
import random
import re
from flask import Flask, jsonify, request
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

app = Flask(__name__)

# =====================
# TÜRKİYE SAATİ (UTC+3) — sunucu saati ne olursa olsun bu kullanılır
# =====================
TR_TZ = timezone(timedelta(hours=3))

def now_tr():
    """Sunucunun saat dilimi farklı olsa bile her zaman Türkiye saatini (UTC+3) döndürür."""
    return datetime.now(timezone.utc).astimezone(TR_TZ)

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
        date_str = now_tr().strftime('%d%m%y')
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
        last_pool_source["time"] = now_tr()
        last_pool_source["count"] = len(pool)
        last_successful_pool = pool
        return random.sample(pool, min(select_n, len(pool)))

    pool = fetch_pool_stockanalysis()
    if pool:
        last_pool_source["source"] = "stockanalysis"
        last_pool_source["time"] = now_tr()
        last_pool_source["count"] = len(pool)
        last_successful_pool = pool
        return random.sample(pool, min(select_n, len(pool)))

    if last_successful_pool:
        last_pool_source["source"] = "memory"
        last_pool_source["time"] = now_tr()
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
# GEÇMİŞ OHLCV VERİSİ — Fallback zinciri:
# 1. stockanalysis.com /history/ sayfası (ücretsiz, scraping)
# 2. Twelve Data API (kredi limitli, garanti yöntem)
# =====================

def scrape_stockanalysis_history(symbol, min_rows=150):
    """
    stockanalysis.com/stocks/{symbol}/history/ sayfasından OHLCV tablosunu
    scrape eder. Sayfa varsayılan olarak ~50 satır/sayfa gösteriyor ve
    sayfalama (?p=2, ?p=3...) destekliyor; yeterli satır birikene kadar
    art arda sayfa çekilir. Sayfa yapısı değişirse veya erişim engellenirse
    None döner, çağıran taraf Twelve Data'ya düşer.

    ÖNEMLİ - PERFORMANS NOTU: Sayfanın gerçek HTML'i, SvelteKit'in SSR
    yorum işaretleriyle (<!--[!--><!--]-->) çok yoğun serpiştirilmiş.
    Tek bir büyük regex'te art arda .*? + re.DOTALL kullanmak burada
    CATASTROPHIC BACKTRACKING'e yol açıyor (test edildi: küçük örnekte
    hızlı, gerçek yoğunlukta dakikalarca/sınırsız sürebiliyor). Bu, worker
    timeout/SIGKILL sorununun GERÇEK kök nedeniydi. Çözüm: önce TÜM yorum
    bloklarını tek seferde (basit, doğrusal regex ile) temizle, sonra
    temiz HTML üzerinde tekrar etmeyen (.*? zincirsiz) güvenli bir desenle
    eşleştir. Bu yöntem mikrosaniyeler içinde çalışır.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        all_rows = []
        for page in range(1, 5):
            url = f"https://stockanalysis.com/stocks/{symbol.lower()}/history/"
            params = {"p": page} if page > 1 else {}
            r = requests.get(url, headers=headers, params=params, timeout=8)
            if r.status_code != 200:
                break
            html = r.text

            # ADIM 1: SvelteKit SSR yorum bloklarını temizle — bu basit desen
            # (.*? ama TEK seferde, zincirsiz) backtracking riski taşımaz.
            clean_html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)

            # ADIM 2: Temiz HTML'de art arda gelen <td> hücrelerini güvenle eşleştir.
            # Artık aralarda yorum olmadığı için .*? zincirine gerek yok.
            row_pattern = re.compile(
                r'<td class="sym[^"]*">([A-Za-z]+ \d{1,2}, \d{4})</td>'
                r'<td[^>]*>([\d.]+)</td>'
                r'<td[^>]*>([\d.]+)</td>'
                r'<td[^>]*>([\d.]+)</td>'
                r'<td[^>]*>([\d.]+)</td>'
                r'<td[^>]*>[\d.]+</td>'  # Adj. Close (atlanır)
                r'<td[^>]*><span[^>]*>-?[\d.]+%</span></td>'  # Change (atlanır)
                r'<td[^>]*>([\d,]+)</td>'  # Volume
            )
            matches = row_pattern.findall(clean_html)
            if not matches:
                break
            all_rows.extend(matches)
            if len(all_rows) >= min_rows:
                break
            # Bir sonraki sayfada aynı satırları tekrar görmemek için kısa bekleme

        if len(all_rows) < 20:
            return None

        df = pd.DataFrame(all_rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        df["Date"] = pd.to_datetime(df["Date"], format="%b %d, %Y", errors="coerce")
        for col in ["Open", "High", "Low", "Close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["Volume"] = df["Volume"].str.replace(",", "", regex=False)
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)

        if len(df) < 20:
            return None
        return df
    except:
        return None

def td_get_ohlcv(symbol, outputsize=210):
    # 1. Önce ücretsiz kaynak: stockanalysis.com /history/
    df = scrape_stockanalysis_history(symbol, min_rows=min(outputsize, 150))
    if df is not None and len(df) >= 20:
        return df

    # 2. Fallback: Twelve Data (kredi limitli)
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

def get_weekly_rsi(symbol, df=None):
    """Haftalık RSI - çoklu zaman dilimi teyidi için"""
    try:
        if df is None:
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
# ELLİOTT DALGA SAYIMI
# =====================
# Not: Tam otomatik Elliott dalga etiketleme algoritmik olarak çok zor bir problemdir.
# Bu modül, ZigZag pivot tespiti üzerine basitleştirilmiş bir 5-dalga kontrolü kurar
# ve sonucu bir ÖNERİ olarak sunar; kesin/garanti bir dalga sayımı değildir.

def find_zigzag_pivots(df, pct_threshold=5.0):
    """
    Basit bir ZigZag pivot tespiti: fiyat önceki pivot noktasından
    en az pct_threshold% hareket etmedikçe yeni bir pivot kabul edilmez.
    Küçük gürültüyü eler, anlamlı yön değişimlerini (swing high/low) yakalar.
    """
    closes = df['Close'].values
    dates = df['Date'].values
    if len(closes) < 10:
        return []

    pivots = []
    last_pivot_idx = 0
    last_pivot_price = closes[0]
    direction = None  # 'up' veya 'down'

    for i in range(1, len(closes)):
        change_pct = (closes[i] - last_pivot_price) / last_pivot_price * 100

        if direction is None:
            if abs(change_pct) >= pct_threshold:
                direction = 'up' if change_pct > 0 else 'down'
                last_pivot_idx = i
                last_pivot_price = closes[i]
        elif direction == 'up':
            if closes[i] > last_pivot_price:
                last_pivot_idx = i
                last_pivot_price = closes[i]
            elif (closes[i] - last_pivot_price) / last_pivot_price * 100 <= -pct_threshold:
                pivots.append({'idx': last_pivot_idx, 'price': last_pivot_price, 'type': 'high', 'date': dates[last_pivot_idx]})
                direction = 'down'
                last_pivot_idx = i
                last_pivot_price = closes[i]
        elif direction == 'down':
            if closes[i] < last_pivot_price:
                last_pivot_idx = i
                last_pivot_price = closes[i]
            elif (closes[i] - last_pivot_price) / last_pivot_price * 100 >= pct_threshold:
                pivots.append({'idx': last_pivot_idx, 'price': last_pivot_price, 'type': 'low', 'date': dates[last_pivot_idx]})
                direction = 'up'
                last_pivot_idx = i
                last_pivot_price = closes[i]

    # Son (henüz tamamlanmamış) pivotu da ekle
    pivots.append({'idx': last_pivot_idx, 'price': last_pivot_price,
                    'type': 'high' if direction == 'up' else 'low', 'date': dates[last_pivot_idx]})
    return pivots

def label_elliott_waves(pivots):
    """
    Son 6 pivot noktasını (5 dalga + başlangıç) kullanarak klasik dürtü (impulse)
    yapısının temel kurallarını kontrol eder:
    - Dalga 2, Dalga 1'in başlangıcının altına (boğa) / üstüne (ayı) inmez
    - Dalga 3, Dalga 1'den daha uzundur (genelde en uzun dalga)
    - Dalga 4, Dalga 1'in zirvesiyle çakışmaz (overlap kuralı)
    Kurallara uyan en güncel 6 pivot bulunursa "5 dalga tamamlandı" önerisi yapılır.
    """
    if len(pivots) < 6:
        return None

    last6 = pivots[-6:]
    p0, p1, p2, p3, p4, p5 = [p['price'] for p in last6]
    bullish = p1 > p0  # ilk hareket yukarıysa boğa dürtüsü olabilir

    if bullish:
        wave1 = p1 - p0
        wave3 = p3 - p2
        wave5 = p5 - p4
        rule_wave2 = p2 > p0          # Dalga 2, başlangıcın altına inmemeli
        rule_wave3_longest = wave3 > wave1 and wave3 > wave5
        rule_wave4_no_overlap = p4 > p1   # Dalga 4, Dalga 1 zirvesiyle çakışmamalı
        valid = rule_wave2 and rule_wave3_longest and rule_wave4_no_overlap and p5 > p3
        direction_text = "YUKARI (Boğa Dürtüsü)"
        next_expectation = "5 dalga tamamlanmış görünüyor — ABC düzeltmesi (aşağı) beklenebilir"
    else:
        wave1 = p0 - p1
        wave3 = p2 - p3
        wave5 = p4 - p5
        rule_wave2 = p2 < p0
        rule_wave3_longest = wave3 > wave1 and wave3 > wave5
        rule_wave4_no_overlap = p4 < p1
        valid = rule_wave2 and rule_wave3_longest and rule_wave4_no_overlap and p5 < p3
        direction_text = "AŞAĞI (Ayı Dürtüsü)"
        next_expectation = "5 dalga tamamlanmış görünüyor — ABC düzeltmesi (yukarı) beklenebilir"

    return {
        'valid': valid,
        'bullish': bullish,
        'direction_text': direction_text,
        'next_expectation': next_expectation,
        'pivots': last6,
        'rule_wave2': rule_wave2,
        'rule_wave3_longest': rule_wave3_longest,
        'rule_wave4_no_overlap': rule_wave4_no_overlap,
    }

def elliott_wave_analysis(symbol):
    """
    /elliott (ve eş değerleri: /eliot /elliot /dalga) komutu için ana fonksiyon.
    Hem manuel tekil sorguda hem otomatik taramada kullanılır.
    Döner: {"signal": <5 dalga tamamlanmış öneri metni veya None>, "info": <her durumda gösterilebilecek özet>}
    """
    try:
        df = td_get_ohlcv(symbol, outputsize=150)
        if df is None or len(df) < 30:
            return None

        price = float(df['Close'].iloc[-1])
        pivots = find_zigzag_pivots(df, pct_threshold=5.0)

        if len(pivots) < 6:
            info = f"""
🌊 <b>ELLİOTT DALGA ANALİZİ — Bilgi Amaçlı</b>
🦅 <b>{symbol}</b>
──────────────────────────
💲 Fiyat: ${price:.2f}
📐 Tespit edilen pivot sayısı: {len(pivots)} (gerekli: 6+)

💬 <b>Özet:</b> Anlamlı bir dalga sayımı için yeterli sayıda belirgin
yön değişimi (pivot) bulunamadı. Bu genelde fiyatın yatay/düşük
volatiliteli bir bantta hareket ettiği anlamına gelir.

⚠️ Bu otomatik bir öneridir, kesin dalga sayımı değildir.
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
            return {"signal": None, "info": info}

        result = label_elliott_waves(pivots)
        if result is None:
            return None

        pivot_lines = "\n".join(
            [f"   {i}. {p['date']} — ${p['price']:.2f} ({'Tepe' if p['type']=='high' else 'Dip'})"
             for i, p in enumerate(result['pivots'])]
        )

        kural_text = (
            f"• Dalga 2 kuralı: {'✅' if result['rule_wave2'] else '❌'}\n"
            f"• Dalga 3 en uzun: {'✅' if result['rule_wave3_longest'] else '❌'}\n"
            f"• Dalga 4 çakışma yok: {'✅' if result['rule_wave4_no_overlap'] else '❌'}"
        )

        if result['valid']:
            msg = f"""
🌊 <b>ELLİOTT DALGA SAYIMI — 5 Dalga Tamamlanma Önerisi</b>
🚨 <b>{symbol} - {result['direction_text']}</b>
──────────────────────────
💲 Güncel Fiyat: ${price:.2f}

📍 <b>Tespit Edilen Pivot Noktaları (0-5):</b>
{pivot_lines}

✅ <b>Dürtü Dalgası Kuralları:</b>
{kural_text}

🎯 <b>Beklenti:</b> {result['next_expectation']}

⚠️ Bu otomatik bir öneridir, kesin dalga sayımı garantisi vermez.
Lütfen kendi teyidini de yap.
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
            return {"signal": msg, "info": None}
        else:
            info = f"""
🌊 <b>ELLİOTT DALGA ANALİZİ — Bilgi Amaçlı (Kurallar Tam Karşılanmadı)</b>
🦅 <b>{symbol}</b>
──────────────────────────
💲 Fiyat: ${price:.2f}

📍 <b>Tespit Edilen Pivot Noktaları (0-5):</b>
{pivot_lines}

📋 <b>Dürtü Dalgası Kuralları:</b>
{kural_text}

💬 <b>Özet:</b> Son 6 pivot, klasik 5 dalga dürtü yapısının kurallarını
tam karşılamıyor. Bu, ya dalga sayımının farklı bir noktadan
başlaması gerektiği ya da şu an düzeltme (ABC) aşamasında
olunduğu anlamına gelebilir.

⚠️ Bu otomatik bir öneridir, kesin dalga sayımı değildir.
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
            return {"signal": None, "info": info}
    except:
        return None

# =====================
# FORMASYON SİNYALİ (Dönüş + Devam Formasyonları)
# =====================
# Bu kategori, klasik grafik formasyonlarını tespit eder ve her formasyon
# için beklenen yönü (yukarı/aşağı/devam) belirtir. TREND SİNYALİ ve ERKEN
# UYARI'dan tamamen ayrı, kendi başına çalışan üçüncü bir sinyal türüdür.
#
# DÖNÜŞ FORMASYONLARI: Wolfe Wave, Rising/Falling Wedge, Head&Shoulders
#                       (+ ters), Diamond Top/Bottom, Three Drives
# DEVAM FORMASYONLARI:  Triangle (simetrik), Flag, Rectangle

FORMASYON_YON_BILGISI = {
    'wolfe_wave': {
        'isim': 'Wolfe Wave',
        'kategori': 'Dönüş',
    },
    'rising_wedge': {
        'isim': 'Rising Wedge (Yükselen Takoz)',
        'kategori': 'Dönüş',
        'yon': 'AŞAĞI ⬇️',
        'not': 'Yükselen takozlar genelde aşağı kırılımla sonuçlanır',
    },
    'falling_wedge': {
        'isim': 'Falling Wedge (Düşen Takoz)',
        'kategori': 'Dönüş',
        'yon': 'YUKARI ⬆️',
        'not': 'Düşen takozlar genelde yukarı kırılımla sonuçlanır',
    },
    'head_shoulders': {
        'isim': 'Head & Shoulders (Omuz Baş Omuz)',
        'kategori': 'Dönüş',
        'yon': 'AŞAĞI ⬇️',
        'not': 'Yükseliş trendinin sonunda görülen klasik dönüş formasyonu',
    },
    'inverse_head_shoulders': {
        'isim': 'Ters Omuz Baş Omuz',
        'kategori': 'Dönüş',
        'yon': 'YUKARI ⬆️',
        'not': 'Düşüş trendinin sonunda görülen klasik dönüş formasyonu',
    },
    'diamond_top': {
        'isim': 'Diamond Top',
        'kategori': 'Dönüş',
        'yon': 'AŞAĞI ⬇️',
        'not': 'Yükseliş trendinin tepesinde genişleyip daralan bir yapı',
    },
    'diamond_bottom': {
        'isim': 'Diamond Bottom',
        'kategori': 'Dönüş',
        'yon': 'YUKARI ⬆️',
        'not': 'Düşüş trendinin dibinde genişleyip daralan bir yapı',
    },
    'three_drives_bear': {
        'isim': 'Three Drives (Tükenme - Ayı)',
        'kategori': 'Dönüş',
        'yon': 'AŞAĞI ⬇️',
        'not': '3 itme dalgalı yukarı tükenme, genelde sert düşüşle sonuçlanır',
    },
    'three_drives_bull': {
        'isim': 'Three Drives (Tükenme - Boğa)',
        'kategori': 'Dönüş',
        'yon': 'YUKARI ⬆️',
        'not': '3 itme dalgalı aşağı tükenme, genelde sert yükselişle sonuçlanır',
    },
    'triangle': {
        'isim': 'Symmetrical Triangle (Simetrik Üçgen)',
        'kategori': 'Devam',
        'yon': 'MEVCUT TREND YÖNÜNDE ➡️',
        'not': 'Daralan üçgen genelde mevcut trendin devamı ile kırılır',
    },
    'flag': {
        'isim': 'Flag / Pennant (Bayrak/Flama)',
        'kategori': 'Devam',
        'yon': 'MEVCUT TREND YÖNÜNDE ➡️',
        'not': 'Kısa bir konsolidasyon sonrası trend genelde devam eder',
    },
    'rectangle': {
        'isim': 'Rectangle (Dikdörtgen)',
        'kategori': 'Devam',
        'yon': 'MEVCUT TREND YÖNÜNDE ➡️',
        'not': 'Yatay bant sonrası fiyat genelde geldiği yöne devam eder',
    },
}

def _pct_diff(a, b):
    return abs(a - b) / ((a + b) / 2) * 100 if (a + b) != 0 else 999

def detect_formations(pivots, trend_direction=None):
    """
    Son pivot noktalarını geometrik olarak inceleyip bilinen formasyonlara
    uyup uymadığını kontrol eder. Birden fazla formasyon eşleşebilir,
    hepsi liste olarak döner (boş liste = formasyon tespit edilmedi).
    trend_direction: 'up' / 'down' / None — devam formasyonları için mevcut
    trendin yönünü belirtir (örn. Üçgen kırılımının hangi yöne olası olduğu).
    """
    found = []
    if len(pivots) < 5:
        return found

    last5 = pivots[-5:]
    prices = [p['price'] for p in last5]
    p0, p1, p2, p3, p4 = prices

    # --- DÖNÜŞ: Head & Shoulders (Tepe-Omuz-Baş-Omuz-Tepe: yüksek-düşük-yüksek-düşük-yüksek) ---
    types = [p['type'] for p in last5]
    if types == ['low','high','low','high','low']:
        # p1 (sol omuz), p3 (sağ omuz) tepeleri; ortadaki p2 dip, ama "baş" aranan asıl tepe ayrı pivot setinde olur
        pass

    if len(pivots) >= 5:
        # Tepe noktalarını (high) ve dip noktalarını (low) ayır, son 3 tepe ile H&S dene
        highs = [p for p in pivots[-7:] if p['type'] == 'high']
        lows = [p for p in pivots[-7:] if p['type'] == 'low']

        if len(highs) >= 3:
            h1, h2, h3 = highs[-3]['price'], highs[-2]['price'], highs[-1]['price']
            # Baş (h2) belirgin şekilde diğer ikisinden yüksek, omuzlar (h1, h3) birbirine yakın
            if h2 > h1 * 1.03 and h2 > h3 * 1.03 and _pct_diff(h1, h3) < 4:
                found.append('head_shoulders')

        if len(lows) >= 3:
            l1, l2, l3 = lows[-3]['price'], lows[-2]['price'], lows[-1]['price']
            if l2 < l1 * 0.97 and l2 < l3 * 0.97 and _pct_diff(l1, l3) < 4:
                found.append('inverse_head_shoulders')

        # --- Wedge (Takoz): art arda gelen tepeler VE dipler aynı yönde daralarak hareket ediyor ---
        if len(highs) >= 2 and len(lows) >= 2:
            highs_rising = highs[-1]['price'] > highs[-2]['price']
            lows_rising = lows[-1]['price'] > lows[-2]['price']
            # Rising Wedge: hem tepeler hem dipler yükseliyor ama aralık daralıyor (momentum azalıyor)
            if highs_rising and lows_rising:
                range_old = highs[-2]['price'] - lows[-2]['price']
                range_new = highs[-1]['price'] - lows[-1]['price']
                if range_new < range_old * 0.85:
                    found.append('rising_wedge')
            # Falling Wedge: hem tepeler hem dipler düşüyor ama aralık daralıyor
            elif not highs_rising and not lows_rising:
                range_old = highs[-2]['price'] - lows[-2]['price']
                range_new = highs[-1]['price'] - lows[-1]['price']
                if range_new < range_old * 0.85:
                    found.append('falling_wedge')

        # --- Diamond Top/Bottom: aralık önce genişler sonra daralır ---
        if len(highs) >= 2 and len(lows) >= 2:
            range1 = highs[-2]['price'] - lows[-2]['price']
            range2 = highs[-1]['price'] - lows[-1]['price']
            # Genişleyip daralma paterni (basitleştirilmiş): son aralık ortadaki genişlemeden belirgin küçük
            if len(highs) >= 3 and len(lows) >= 3:
                range0 = highs[-3]['price'] - lows[-3]['price']
                if range1 > range0 * 1.2 and range2 < range1 * 0.7:
                    if highs[-1]['idx'] > lows[-1]['idx']:
                        found.append('diamond_top' if trend_direction == 'up' else 'diamond_bottom')

        # --- Three Drives: 3 ardışık tepe (veya dip) her biri öncekinden daha yüksek/düşük ---
        if len(highs) >= 3:
            h1, h2, h3 = highs[-3]['price'], highs[-2]['price'], highs[-1]['price']
            if h1 < h2 < h3:
                found.append('three_drives_bear')
        if len(lows) >= 3:
            l1, l2, l3 = lows[-3]['price'], lows[-2]['price'], lows[-1]['price']
            if l1 > l2 > l3:
                found.append('three_drives_bull')

        # --- Triangle (devam): tepeler düşüyor, dipler yükseliyor (daralan yatay bant) ---
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1]['price'] < highs[-2]['price'] and lows[-1]['price'] > lows[-2]['price']:
                found.append('triangle')

        # --- Rectangle (devam): son tepeler birbirine yakın VE son dipler birbirine yakın ---
        if len(highs) >= 2 and len(lows) >= 2:
            if _pct_diff(highs[-1]['price'], highs[-2]['price']) < 2 and _pct_diff(lows[-1]['price'], lows[-2]['price']) < 2:
                found.append('rectangle')

    return list(set(found))

def formation_analysis(symbol):
    """
    /formasyon (ve eş değerleri: /formation /pattern) komutu için ana fonksiyon.
    Hem manuel tekil sorguda hem otomatik taramada kullanılır.
    """
    try:
        df = td_get_ohlcv(symbol, outputsize=150)
        if df is None or len(df) < 30:
            return None

        price = float(df['Close'].iloc[-1])
        close = df['Close']
        sma50 = calc_sma(close, min(50, len(close)-1))
        sma50_now = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else price
        trend_direction = 'up' if price > sma50_now else 'down'

        pivots = find_zigzag_pivots(df, pct_threshold=4.0)
        formations = detect_formations(pivots, trend_direction)

        if not formations:
            info = f"""
📐 <b>FORMASYON ANALİZİ — Bilgi Amaçlı</b>
🦅 <b>{symbol}</b>
──────────────────────────
💲 Fiyat: ${price:.2f}
📊 Genel Trend: {"Yukarı" if trend_direction=='up' else "Aşağı"} (SMA50'ye göre)

💬 <b>Özet:</b> Şu an bilinen dönüş veya devam formasyonlarından
(Wolfe Wave, Wedge, H&S, Diamond, Three Drives, Triangle, Flag,
Rectangle) hiçbiri net şekilde tespit edilemedi.

⚠️ Bu otomatik bir öneridir, kesin formasyon teyidi değildir.
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
            return {"signal": None, "info": info}

        bloklar = []
        for key in formations:
            bilgi = FORMASYON_YON_BILGISI.get(key)
            if not bilgi:
                continue
            yon = bilgi.get('yon', 'Belirsiz')
            not_text = bilgi.get('not', '')
            bloklar.append(
                f"🔍 <b>{bilgi['isim']}</b>\n"
                f"   📊 Kategori: {bilgi['kategori']} Formasyonu\n"
                f"   🎯 Beklenen Yön: {yon}\n"
                f"   📝 {not_text}"
            )

        formasyon_text = "\n\n".join(bloklar)

        msg = f"""
📐 <b>FORMASYON SİNYALİ</b>
🚨 <b>{symbol} - Formasyon(lar) Tespit Edildi</b>
──────────────────────────
💲 Fiyat: ${price:.2f}
📊 Genel Trend: {"Yukarı" if trend_direction=='up' else "Aşağı"} (SMA50'ye göre)

{formasyon_text}

⚠️ Bu otomatik bir öneridir, kesin formasyon teyidi değildir.
Lütfen grafiği kendin de görsel olarak teyit et.
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
        return {"signal": msg, "info": None}
    except:
        return None

# =====================
# ANA ANALİZ (Trend Sinyali)
# =====================

def analyze_stock(symbol, df=None):
    try:
        if df is None:
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

        trend_sma200 = f"✅ Fiyat ${price:.2f} / Ortalama ${sma200_now:.2f} → Üstünde (İyi)" if price > sma200_now else f"⚠️ Fiyat ${price:.2f} / Ortalama ${sma200_now:.2f} → Altında"
        trend_ema20 = f"✅ Fiyat ${price:.2f} / Ortalama ${ema20_now:.2f} → Üstünde (İyi)" if price > ema20_now else f"⚠️ Fiyat ${price:.2f} / Ortalama ${ema20_now:.2f} → Altında"
        trend_sma50 = f"✅ Fiyat ${price:.2f} / Ortalama ${sma50_now:.2f} → Üstünde (İyi)" if price > sma50_now else f"⚠️ Fiyat ${price:.2f} / Ortalama ${sma50_now:.2f} → Altında"
        resistance_risk = (
            (abs(price - sma50_now) / price < 0.02 and price < sma50_now) or
            (abs(price - sma200_now) / price < 0.02 and price < sma200_now)
        )

        rsi_signal = rsi_now > rsi_prev and 40 < rsi_now < 65
        macd_crossover = macd_prev_val < macd_sig_prev and macd_now > macd_sig_now
        macd_positive = macd_now > macd_sig_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0
        vol_ok = vol_ratio >= 1.2

        # 💵 Dolar bazlı işlem hacmi (Adet × Fiyat) — SADECE BİLGİ AMAÇLI, skora katkısı YOK
        dollar_volume = vol_now * price
        if dollar_volume >= 1_000_000_000:
            dollar_vol_str = f"${dollar_volume/1_000_000_000:.2f} Milyar"
        elif dollar_volume >= 1_000_000:
            dollar_vol_str = f"${dollar_volume/1_000_000:.2f} Milyon"
        else:
            dollar_vol_str = f"${dollar_volume:,.0f}"
        dollar_vol_ok = dollar_volume >= 100_000
        dollar_vol_line = (
            f"• 💵 İşlem Hacmi (Para Büyüklüğü): {vol_now:,.0f} adet × ${price:.2f} ≈ {dollar_vol_str} "
            f"/ Referans $100.000 üstü → {'✅ Yüksek' if dollar_vol_ok else '⚠️ Düşük'} (bilgi amaçlı, skora katkısı yok)"
        )

        strong_trend = adx_now >= 25
        adx_text = (
            f"✅ {adx_now:.0f} / Referans 25 üstü → Güçlü Trend" if strong_trend
            else f"⚠️ {adx_now:.0f} / Referans 25 üstü → Zayıf Trend"
        )

        index_perf = get_qqq_performance(days=20)
        rs_text = "➖ Veri yok"
        rs_strong = False
        try:
            old_price = float(close.iloc[max(0,len(close)-20)])
            stock_perf = (price - old_price) / old_price * 100
            if index_perf is not None:
                rs_diff = stock_perf - index_perf
                rs_strong = rs_diff > 0
                rs_text = (
                    f"✅ +{rs_diff:.1f}% / Referans 0% üstü → Piyasayı Geride Bıraktı" if rs_strong
                    else f"⚠️ {rs_diff:.1f}% / Referans 0% üstü → Piyasanın Altında Kaldı"
                )
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
            risk_score_i = max(10, min(90, 100 - (score * 8)))
            risk_label_i = "Düşük 🟢" if risk_score_i < 30 else "Orta 🟡" if risk_score_i < 60 else "Yüksek 🔴"
            vol_text_i = (
                f"✅ {vol_ratio:.1f}x / Referans 1.2x üstü → Yeterli (İyi)" if vol_ok
                else f"⚠️ {vol_ratio:.1f}x / Referans 1.2x üstü → Yetersiz"
            )
            macd_text_i = (
                "✅ Taze Crossover 🔥 → Pozitif (İyi)" if macd_crossover
                else "✅ Pozitif (İyi)" if macd_positive
                else "⚠️ Negatif"
            )
            rsi_text_i = (
                f"✅ {rsi_now:.1f} / Referans 40-65 arası → Sağlıklı Bölgede" if rsi_signal
                else f"⚠️ {rsi_now:.1f} / Referans 40-65 arası → Bölge Dışı"
            )
            low_data_note_i = f"\n⚠️ Not: Sınırlı veri ({data_days} gün).\n" if low_data_warning else ""

            yorum_parcalari = []
            yorum_parcalari.append("fiyat SMA200'ün üzerinde" if price > sma200_now else "fiyat SMA200'ün altında")
            yorum_parcalari.append("trend gücü yeterli (ADX≥25)" if strong_trend else "trend gücü zayıf (ADX<25)")
            yorum_parcalari.append("MACD pozitif" if macd_positive else "MACD negatif")
            yorum_parcalari.append("hacim teyidi var" if vol_ok else "hacim teyidi yok")
            ozet_i = f"Toplam skor {score}/7 eşiğinin altında kaldı. " + ", ".join(yorum_parcalari) + ". Net bir TREND SİNYALİ için daha fazla kriterin aynı yönde hizalanması gerekiyor."

            msg_info = f"""
📊 <b>TREND SİNYALİ — Bilgi Amaçlı (Eşik Karşılanmadı)</b>
🦅 <b>{symbol}</b>
──────────────────────────
💲 Fiyat: ${price:.2f}

📊 <b>Teknik Durum:</b>
• SMA200 (Uzun Vadeli Ortalama): {trend_sma200}
• EMA20 (Kısa Vadeli Ortalama): {trend_ema20}
• SMA50 (Orta Vadeli Ortalama): {trend_sma50}
• RSI (Momentum, 0-100): {rsi_text_i}
• MACD (Yön Sinyali): {macd_text_i}
• Hacim (Adet Oranı): {vol_text_i}
{dollar_vol_line}
• ADX (Trend Gücü Endeksi): {adx_text}
• Piyasaya Göre Güç (QQQ'a Kıyasla): {rs_text}
• Uyumsuzluk (Fiyat-RSI Çelişkisi): {divergence_text}

📐 <b>Skor: {score}/7</b> (Sinyal eşiği: 7+)
⚠️ Risk Skoru: %{risk_score_i} - {risk_label_i}
{low_data_note_i}
💬 <b>Özet:</b> {ozet_i}

{get_source_label()}
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
            return {"signal": None, "info": msg_info}

        score_for_signal = score

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

        vol_text = (
            f"✅ {vol_ratio:.1f}x 🔥 / Referans 1.2x üstü → Çok Yüksek (İyi)" if vol_ratio >= 2
            else f"✅ {vol_ratio:.1f}x / Referans 1.2x üstü → Yeterli (İyi)" if vol_ratio >= 1.2
            else f"⚠️ {vol_ratio:.1f}x / Referans 1.2x üstü → Yetersiz"
        )
        macd_text = (
            "✅ Taze Crossover 🔥 → Pozitif (İyi)" if macd_crossover
            else "✅ Pozitif (İyi)" if macd_positive
            else "⚠️ Negatif"
        )
        rsi_text = (
            f"✅ {rsi_now:.1f} / Referans 40-65 arası → Sağlıklı Bölgede" if rsi_signal
            else f"⚠️ {rsi_now:.1f} / Referans 40-65 arası → Bölge Dışı"
        )

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
• SMA200 (Uzun Vadeli Ortalama): {trend_sma200}
• EMA20 (Kısa Vadeli Ortalama): {trend_ema20}
• SMA50 (Orta Vadeli Ortalama): {trend_sma50}
• RSI (Momentum, 0-100): {rsi_text}
• MACD (Yön Sinyali): {macd_text}
• Hacim (Adet Oranı): {vol_text}
{dollar_vol_line}
• ADX (Trend Gücü Endeksi): {adx_text}
• Piyasaya Göre Güç (QQQ'a Kıyasla): {rs_text}
• Uyumsuzluk (Fiyat-RSI Çelişkisi): {divergence_text}
• Sektör: {sector_text}

⚠️ Direnç (Yakın Engel Kontrolü): {"⚠️ Kritik seviye yakın → Dikkat" if resistance_risk else "✅ Temiz → Önünde Açık Alan Var"}
⚠️ Risk Skoru: %{risk_score} - {risk_label}
{low_data_note}
{get_source_label()}
💡 <b>Karar:</b> {karar}
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
        return {"signal": msg, "info": None}
    except:
        return None

# =====================
# ERKEN UYARI SİSTEMİ (7 Maddelik Uyumsuzluk Teyidi)
# =====================

def early_warning_scan(symbol, df=None):
    """
    Sadece RSI/fiyat uyumsuzluğuna dayanan erken dönüş sinyali.
    7 kriterle teyit edilir, ana trend filtresinden bağımsız çalışır.
    """
    try:
        if df is None:
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
            price_i = float(close.iloc[-1])
            rsi_i = float(rsi.iloc[-1])
            macd_i = float(macd.iloc[-1])
            macd_sig_i = float(macd_signal.iloc[-1])
            msg_info = f"""
⚡ <b>ERKEN UYARI — Bilgi Amaçlı (Uyumsuzluk Yok)</b>
🦅 <b>{symbol}</b>
──────────────────────────
💲 Fiyat: ${price_i:.2f}
📉 RSI(14): {rsi_i:.1f}
📊 MACD: {macd_i:.3f} (Sinyal: {macd_sig_i:.3f}) — {"✅ Pozitif" if macd_i > macd_sig_i else "⚠️ Negatif"}

💬 <b>Özet:</b> Son 10 günde fiyat ile RSI arasında bir uyumsuzluk (divergence) tespit edilmedi. Erken uyarı sistemi yalnızca uyumsuzluk durumunda kriter taraması başlatır, bu yüzden teyit kriterleri hesaplanmadı.

{get_source_label()}
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}
"""
            return {"signal": None, "info": msg_info}

        direction = "BOĞA" if bull_div else "AYI"
        price = float(close.iloc[-1])

        # KRİTER 1: Hacim teyidi (adet bazlı oran — skora/sayaca katkı sağlar)
        vol_now = float(volume.iloc[-1])
        vol_avg = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else vol_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0
        crit_volume = vol_ratio >= 1.3
        vol_kriter_text = (
            f"{'✅' if crit_volume else '❌'} {vol_ratio:.1f}x / Referans 1.3x üstü → {'Yeterli' if crit_volume else 'Yetersiz'}"
        )

        # 💵 Dolar bazlı işlem hacmi (Adet × Fiyat) — SADECE BİLGİ AMAÇLI, kriter sayacına katkısı YOK
        dollar_volume = vol_now * price
        if dollar_volume >= 1_000_000_000:
            dollar_vol_str = f"${dollar_volume/1_000_000_000:.2f} Milyar"
        elif dollar_volume >= 1_000_000:
            dollar_vol_str = f"${dollar_volume/1_000_000:.2f} Milyon"
        else:
            dollar_vol_str = f"${dollar_volume:,.0f}"
        dollar_vol_ok = dollar_volume >= 100_000
        dollar_vol_line = (
            f"• 💵 İşlem Hacmi (Para Büyüklüğü): {vol_now:,.0f} adet × ${price:.2f} ≈ {dollar_vol_str} "
            f"/ Referans $100.000 üstü → {'✅ Yüksek' if dollar_vol_ok else '⚠️ Düşük'} (bilgi amaçlı, kriter sayacına katkısı yok)"
        )

        # KRİTER 2: Çoklu zaman dilimi (haftalık RSI)
        weekly_rsi = get_weekly_rsi(symbol, df=df)
        if direction == "BOĞA":
            crit_timeframe = weekly_rsi is not None and weekly_rsi < 55
            timeframe_text = (
                f"{'✅' if crit_timeframe else '❌'} {weekly_rsi:.1f} / Referans <55 (Boğa) → {'Destekliyor' if crit_timeframe else 'Desteklemiyor'}"
                if weekly_rsi is not None else "❌ Veri yok"
            )
        else:
            crit_timeframe = weekly_rsi is not None and weekly_rsi > 45
            timeframe_text = (
                f"{'✅' if crit_timeframe else '❌'} {weekly_rsi:.1f} / Referans >45 (Ayı) → {'Destekliyor' if crit_timeframe else 'Desteklemiyor'}"
                if weekly_rsi is not None else "❌ Veri yok"
            )

        # KRİTER 3: Destek/Direnç seviyesi çakışması
        sma200_now = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else price
        ema20_now = float(ema20.iloc[-1])
        dist_sma200 = abs(price - sma200_now) / price * 100
        dist_ema20 = abs(price - ema20_now) / price * 100
        near_support = dist_sma200 < 3 or dist_ema20 < 2
        crit_support = near_support
        support_text = (
            f"{'✅' if crit_support else '❌'} SMA200'e %{dist_sma200:.1f} / EMA20'ye %{dist_ema20:.1f} "
            f"/ Referans %3 ve %2 içi → {'Yakın (İyi)' if crit_support else 'Uzak'}"
        )

        # KRİTER 4: Mum formasyonu
        candle_pattern = detect_candle_pattern(df)
        crit_candle = candle_pattern is not None
        candle_text = f"✅ {candle_pattern} tespit edildi" if crit_candle else "❌ Tespit edilmedi"

        # KRİTER 5: MACD histogram daralması
        macd_hist = (macd - macd_signal).values[-5:]
        if direction == "BOĞA":
            crit_macd_hist = macd_hist[-1] > macd_hist[-3]
        else:
            crit_macd_hist = macd_hist[-1] < macd_hist[-3]
        macd_hist_text = (
            f"{'✅' if crit_macd_hist else '❌'} Son {macd_hist[-1]:.3f} / Önceki {macd_hist[-3]:.3f} "
            f"→ {'Momentum Yönünde (İyi)' if crit_macd_hist else 'Henüz Değişmedi'}"
        )

        # KRİTER 6: Piyasa/sektör genel durumu (QQQ)
        qqq_perf = get_qqq_performance(days=5)
        if direction == "BOĞA":
            crit_market = qqq_perf is not None and qqq_perf > -3
            market_text = (
                f"{'✅' if crit_market else '❌'} QQQ %{qqq_perf:.1f} / Referans >-3% (Boğa) → {'Uygun' if crit_market else 'Uygun Değil'}"
                if qqq_perf is not None else "❌ Veri yok"
            )
        else:
            crit_market = qqq_perf is not None and qqq_perf < 3
            market_text = (
                f"{'✅' if crit_market else '❌'} QQQ %{qqq_perf:.1f} / Referans <3% (Ayı) → {'Uygun' if crit_market else 'Uygun Değil'}"
                if qqq_perf is not None else "❌ Veri yok"
            )

        # KRİTER 7: Aşırı satım/alım derinliği
        rsi_now = float(rsi.iloc[-1])
        if direction == "BOĞA":
            crit_depth = rsi_now < 35
            depth_text = f"{'✅' if crit_depth else '❌'} {rsi_now:.1f} / Referans <35 (Boğa) → {'Yeterince Düşük' if crit_depth else 'Henüz Yeterince Düşük Değil'}"
        else:
            crit_depth = rsi_now > 65
            depth_text = f"{'✅' if crit_depth else '❌'} {rsi_now:.1f} / Referans >65 (Ayı) → {'Yeterince Yüksek' if crit_depth else 'Henüz Yeterince Yüksek Değil'}"

        criteria = [crit_volume, crit_timeframe, crit_support, crit_candle, crit_macd_hist, crit_market, crit_depth]
        confirmed = sum(criteria)

        if confirmed < 3:
            confidence_i = "Düşük 🟠" if confirmed == 2 else "Çok Düşük 🔴"
            yorum_parcalari = []
            yorum_parcalari.append("hacim teyidi var" if crit_volume else "hacim teyidi yok")
            yorum_parcalari.append("haftalık zaman dilimi destekliyor" if crit_timeframe else "haftalık zaman dilimi desteklemiyor")
            yorum_parcalari.append("RSI yeterince aşırı bölgede" if crit_depth else "RSI henüz aşırı bölgede değil")
            ozet_i = f"{direction} uyumsuzluğu tespit edildi ama yalnızca {confirmed}/7 kriter karşılandı (gerekli: 3+). " + ", ".join(yorum_parcalari) + "."

            msg_info = f"""
⚡ <b>ERKEN UYARI — Bilgi Amaçlı (Eşik Karşılanmadı)</b>
🦅 <b>{symbol} - {direction} UYUMSUZLUĞU (zayıf)</b>
──────────────────────────
💲 Fiyat: ${price:.2f}
{dollar_vol_line}

📋 <b>Teyit Kriterleri ({confirmed}/7):</b>
• Hacim Teyidi (Adet Oranı): {vol_kriter_text}
• Haftalık Teyit (Büyük Resim RSI): {timeframe_text}
• Destek/Direnç (Ortalamaya Yakınlık): {support_text}
• Mum Formasyonu: {candle_text}
• MACD Daralması (Momentum Değişimi): {macd_hist_text}
• Piyasa Durumu (Genel Piyasa QQQ): {market_text}
• Aşırı Satım/Alım (RSI Derinliği): {depth_text}

🎯 Güven Seviyesi: <b>{confidence_i}</b> (eşik altı)
💬 <b>Özet:</b> {ozet_i}

{get_source_label()}
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}

⚠️ Bu bilgi amaçlıdır, sinyal eşiği (3/7) karşılanmamıştır.
"""
            return {"signal": None, "info": msg_info}

        confidence = "Yüksek 🟢" if confirmed >= 5 else "Orta 🟡" if confirmed >= 4 else "Düşük 🟠"

        emoji = "🟢" if direction == "BOĞA" else "🔴"
        action = "Yukarı dönüş potansiyeli" if direction == "BOĞA" else "Aşağı dönüş riski"

        msg = f"""
⚡ <b>ERKEN UYARI — Dönüş Potansiyeli</b>
🚨 <b>{symbol} - {direction} UYUMSUZLUĞU</b>
──────────────────────────
{emoji} {action}
💲 Fiyat: ${price:.2f}
{dollar_vol_line}

📋 <b>Teyit Kriterleri ({confirmed}/7):</b>
• Hacim Teyidi (Adet Oranı): {vol_kriter_text}
• Haftalık Teyit (Büyük Resim RSI): {timeframe_text}
• Destek/Direnç (Ortalamaya Yakınlık): {support_text}
• Mum Formasyonu: {candle_text}
• MACD Daralması (Momentum Değişimi): {macd_hist_text}
• Piyasa Durumu (Genel Piyasa QQQ): {market_text}
• Aşırı Satım/Alım (RSI Derinliği): {depth_text}

🎯 Güven Seviyesi: <b>{confidence}</b>
{get_source_label()}
⏰ {now_tr().strftime('%d.%m.%Y %H:%M')}

⚠️ Bu erken bir sinyal, ana trend filtresinden geçmemiştir. Dikkatli değerlendir.
"""
        return {"signal": msg, "info": None}
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
🌊 ELLİOTT DALGA | 📐 FORMASYON
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

🌊 <b>ELLİOTT DALGA SAYIMI</b>
/dalga [HİSSE] — tekil analiz
/dalga tara — genel tarama
(eş değer: /elliott, /eliot)

📐 <b>FORMASYON SİNYALİ</b>
/formasyon [HİSSE] — tekil analiz
/formasyon tara — genel tarama
(eş değer: /formation, /pattern)
Dönüş: Wolfe Wave, Wedge, H&S, Diamond, Three Drives
Devam: Triangle, Flag, Rectangle

📌 <b>TAKİP</b>
/takip [hisse] [giris] [stop] [hedef]
/takiplerim

📊 <b>ANALİZ</b> (eşik karşılanmasa da güncel durumu gösterir)
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

🌊 <b>ELLIOTT WAVE COUNT</b>
/elliott [TICKER] — single analysis
/elliott scan — general scan
(aliases: /eliot, /dalga)

📐 <b>FORMATION SIGNAL</b>
/formation [TICKER] — single analysis
/formation scan — general scan
(aliases: /formasyon, /pattern)
Reversal: Wolfe Wave, Wedge, H&S, Diamond, Three Drives
Continuation: Triangle, Flag, Rectangle

📌 <b>TRACKING</b>
/track [ticker] [entry] [stop] [target] | /mytracks

📊 <b>ANALYSIS</b> (shows current status even below threshold)
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
                    result = analyze_stock(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"], cid)
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
                result = analyze_stock(tk)
                if result and result.get("signal"):
                    send_telegram(result["signal"], cid)
                elif result and result.get("info"):
                    send_telegram(result["info"], cid)
                else:
                    send_telegram(f"{tk} için veri alınamadı.", cid)
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
                    if result and result.get("signal"):
                        send_telegram(result["signal"], cid)
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
                if result and result.get("signal"):
                    send_telegram(result["signal"], cid)
                elif result and result.get("info"):
                    send_telegram(result["info"], cid)
                else:
                    send_telegram(f"{tk} için veri alınamadı.", cid)
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
            shared_df = td_get_ohlcv(tk, outputsize=210)
            if shared_df is None:
                send_telegram(f"{tk} için veri alınamadı. Sembolü kontrol et.", cid)
                return

            trend_result = analyze_stock(tk, df=shared_df)
            # Erken Uyarı 60 günlük veri istiyor; paylaşılan veri zaten en az bu kadar
            ew_result = early_warning_scan(tk, df=shared_df)

            trend_signal = trend_result.get("signal") if trend_result else None
            trend_info = trend_result.get("info") if trend_result else None
            ew_signal = ew_result.get("signal") if ew_result else None
            ew_info = ew_result.get("info") if ew_result else None

            if trend_signal:
                send_telegram(trend_signal, cid)
            if ew_signal:
                send_telegram(ew_signal, cid)

            # Hiçbir gerçek sinyal yoksa, bilgi amaçlı durumu göster
            if not trend_signal and not ew_signal:
                if trend_info:
                    send_telegram(trend_info, cid)
                if ew_info:
                    send_telegram(ew_info, cid)
                if not trend_info and not ew_info:
                    send_telegram(f"{tk} için veri alınamadı. Sembolü kontrol et.", cid)
        threading.Thread(target=do_full_analyze).start()
        return

    # ELLİOTT DALGA SAYIMI (eş değer: elliott, eliot, elliot, dalga)
    if any(t.startswith(x) for x in ['/elliott','/eliot','/elliot','/dalga']):
        rest = re.sub(r'^/(elliott|eliot|dalga)', '', t).strip()
        if any(x in rest for x in ['tara','scan']):
            send_telegram("🌊 Elliott Dalga taraması başladı...", chat_id)
            def do_elliott_scan(cid=chat_id):
                batch = get_trading_pool(20)
                found = 0
                for ticker in batch:
                    result = elliott_wave_analysis(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"], cid)
                        found += 1
                        time.sleep(2)
                    time.sleep(1.2)
                if found == 0:
                    send_telegram("Elliott Dalga taraması tamamlandı. Tamamlanmış 5 dalga yapısı bulunamadı.", cid)
            threading.Thread(target=do_elliott_scan).start()
        elif rest:
            ticker = rest.upper().split()[0]
            send_telegram(f"🌊 {ticker} Elliott Dalga analizi yapılıyor...", chat_id)
            def do_single_elliott(tk=ticker, cid=chat_id):
                result = elliott_wave_analysis(tk)
                if result and result.get("signal"):
                    send_telegram(result["signal"], cid)
                elif result and result.get("info"):
                    send_telegram(result["info"], cid)
                else:
                    send_telegram(f"{tk} için veri alınamadı.", cid)
            threading.Thread(target=do_single_elliott).start()
        else:
            send_telegram("Kullanım: /dalga [HİSSE] veya /dalga tara", chat_id)
        return

    # FORMASYON SİNYALİ (eş değer: formation, formasyon, pattern)
    if any(t.startswith(x) for x in ['/formation','/formasyon','/pattern']):
        rest = re.sub(r'^/(formation|formasyon|pattern)', '', t).strip()
        if any(x in rest for x in ['tara','scan']):
            send_telegram("📐 Formasyon taraması başladı...", chat_id)
            def do_formation_scan(cid=chat_id):
                batch = get_trading_pool(20)
                found = 0
                for ticker in batch:
                    result = formation_analysis(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"], cid)
                        found += 1
                        time.sleep(2)
                    time.sleep(1.2)
                if found == 0:
                    send_telegram("Formasyon taraması tamamlandı. Net formasyon bulunamadı.", cid)
            threading.Thread(target=do_formation_scan).start()
        elif rest:
            ticker = rest.upper().split()[0]
            send_telegram(f"📐 {ticker} formasyon analizi yapılıyor...", chat_id)
            def do_single_formation(tk=ticker, cid=chat_id):
                result = formation_analysis(tk)
                if result and result.get("signal"):
                    send_telegram(result["signal"], cid)
                elif result and result.get("info"):
                    send_telegram(result["info"], cid)
                else:
                    send_telegram(f"{tk} için veri alınamadı.", cid)
            threading.Thread(target=do_single_formation).start()
        else:
            send_telegram("Kullanım: /formasyon [HİSSE] veya /formasyon tara", chat_id)
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
                news_archive[nid] = {'title': title, 'symbol': tk, 'date': now_tr().strftime('%d.%m.%Y %H:%M')}
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
                    result = analyze_stock(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"])
                        time.sleep(2)
                    time.sleep(0.5)

                ew_batch = random.sample(batch, min(10, len(batch)))
                for ticker in ew_batch:
                    result = early_warning_scan(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"])
                        time.sleep(2)
                    time.sleep(0.5)

                # Elliott Dalga ve Formasyon taraması: kredi/worker yükünü
                # korumak için daha küçük bir alt-küme üzerinde çalışır.
                pattern_batch = random.sample(batch, min(5, len(batch)))
                for ticker in pattern_batch:
                    result = elliott_wave_analysis(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"])
                        time.sleep(2)
                    time.sleep(0.5)

                for ticker in pattern_batch:
                    result = formation_analysis(ticker)
                    if result and result.get("signal"):
                        send_telegram(result["signal"])
                        time.sleep(2)
                    time.sleep(0.5)

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
