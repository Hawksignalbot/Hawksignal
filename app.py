import os
import time
import requests
import threading
from flask import Flask, jsonify
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import numpy as np

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "AVGO", "QCOM", "INTC", "MU", "AMAT", "KLAC", "MRVL", "ORCL",
    "CRM", "NOW", "SNOW", "PLTR", "DDOG", "NET", "CRWD", "ZS",
    "PANW", "SHOP", "NFLX", "UBER", "PYPL", "COIN", "MRNA", "BNTX",
    "ENPH", "FSLR", "RIVN", "RBLX", "U", "IONQ", "QUBT", "TTD"
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# --- Gösterge Hesaplamaları (kütüphane yok, sıfırdan) ---

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
                return days_left
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

def analyze_stock(ticker):
    try:
        df = yf.download(ticker, period="200d", interval="1d", progress=False)
        if df is None or len(df) < 60:
            return None

        close = df['Close'].squeeze()
        volume = df['Volume'].squeeze()

        # Göstergeler
        rsi = calc_rsi(close)
        macd, macd_signal = calc_macd(close)
        ema20 = calc_ema(close, 20)
        sma50 = calc_sma(close, 50)
        sma200 = calc_sma(close, 200)
        vol_ma20 = volume.rolling(20).mean()

        last = close.iloc[-1]
        prev_close = close.iloc[-2]
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
        price = float(last)

        # --- FİLTRE 1: TREND ---
        trend_sma200 = "✅ Yukarıda" if price > sma200_now else "⚠️ Aşağıda"
        trend_ema20 = "✅ Yukarıda" if price > ema20_now else "⚠️ Aşağıda"
        trend_sma50 = "✅ Yukarıda" if price > sma50_now else "⚠️ Aşağıda"

        # Direnç kontrolü - SMA50 veya SMA200'e %1-2 yakın mı?
        resistance_risk = False
        if abs(price - sma50_now) / price < 0.02 and price < sma50_now:
            resistance_risk = True
        if abs(price - sma200_now) / price < 0.02 and price < sma200_now:
            resistance_risk = True

        # --- FİLTRE 2: TEKNİK SİNYAL ---
        rsi_signal = rsi_now > rsi_prev and rsi_now < 65 and rsi_now > 40
        macd_crossover = macd_prev_val < macd_sig_prev and macd_now > macd_sig_now
        macd_positive = macd_now > macd_sig_now
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0
        vol_ok = vol_ratio >= 1.2

        # --- FİLTRE 3: BİLANÇO ---
        earnings_days = get_earnings_days(ticker)
        earnings_risk = earnings_days <= 7
        revenue_growth = get_revenue_growth(ticker)

        # --- KARAR MANTIĞI ---
        score = 0

        # Trend puanı
        if price > sma200_now: score += 2
        if price > sma50_now: score += 1
        if price > ema20_now: score += 1

        # Teknik sinyal puanı
        if macd_crossover: score += 3
        elif macd_positive: score += 1
        if rsi_signal: score += 2
        if vol_ok: score += 2

        # Risk düşürme
        if price < sma200_now: score -= 2
        if resistance_risk: score -= 2
        if earnings_risk: return None  # Bilanço yakın, işlem yok
        if revenue_growth is not None and revenue_growth < 0: score -= 1

        if score < 6: return None

        # --- RİSK SKORU ---
        risk_score = 100 - (score * 10)
        risk_score = max(10, min(90, risk_score))
        if risk_score < 30:
            risk_label = "Düşük 🟢"
        elif risk_score < 60:
            risk_label = "Orta 🟡"
        else:
            risk_label = "Yüksek 🔴"

        # --- ÇIKIŞ STRATEJİSİ ---
        entry = price
        take_profit = round(entry * 1.05, 2)

        # Stop loss: en yakın destek (EMA20 veya SMA50'nin altı)
        support = max(ema20_now, sma50_now * 0.99)
        stop_loss = round(min(support, entry * 0.97), 2)

        risk_amt = round(entry - stop_loss, 2)
        reward_amt = round(take_profit - entry, 2)
        rr = round(reward_amt / risk_amt, 1) if risk_amt > 0 else 0

        # Hacim metni
        if vol_ratio >= 2.0:
            vol_text = f"✅ {vol_ratio:.1f}x Ortalama 🔥"
        elif vol_ratio >= 1.2:
            vol_text = f"✅ {vol_ratio:.1f}x Ortalama"
        else:
            vol_text = f"⚠️ {vol_ratio:.1f}x (Zayıf)"

        # MACD metni
        if macd_crossover:
            macd_text = "✅ Taze Yukarı Crossover! 🔥"
        elif macd_positive:
            macd_text = "✅ Pozitif"
        else:
            macd_text = "⚠️ Negatif"

        # RSI metni
        rsi_text = f"✅ {rsi_now:.1f} (Momentum var)" if rsi_signal else f"⚠️ {rsi_now:.1f}"

        # Bilanço metni
        if earnings_days < 30:
            earnings_text = f"⚠️ {earnings_days} gün kaldı"
        else:
            earnings_text = f"✅ Güvenli ({earnings_days} gün)"

        # Gelir büyümesi
        if revenue_growth is not None:
            rev_text = f"{'✅' if revenue_growth > 0 else '⚠️'} {revenue_growth*100:.1f}%"
        else:
            rev_text = "— Veri yok"

        # Karar notu
        if score >= 8 and rr >= 2:
            karar = "💚 İŞLEME GİRİLEBİLİR. Güçlü setup, risk/ödül oranı uygun."
        elif score >= 6:
            karar = "🟡 İZLEMEDE KALSIN. Setup oluşuyor, onay bekleniyor."
        else:
            karar = "🔴 GEÇ. Kriterler yeterli değil."

        msg = f"""
🚨 <b>{ticker} - POTANSİYEL SİNYAL ANALİZİ</b>
──────────────────────────
📈 Önerilen Giriş: <b>${entry:.2f}</b>
🎯 Kar Al (%5 TP): <b>${take_profit:.2f}</b>
🛑 Zarar Kes (SL): <b>${stop_loss:.2f}</b>
⚖️ Risk/Ödül: <b>1:{rr}</b>

📊 <b>Teknik ve Trend Durumu:</b>
• Ana Trend (SMA200): {trend_sma200}
• EMA20 / SMA50: {trend_ema20} / {trend_sma50}
• RSI: {rsi_text}
• MACD: {macd_text}
• Hacim: {vol_text}

⚠️ <b>Risk Değerlendirmesi:</b>
• Bilanço Yakınlığı: {earnings_text}
• Gelir Büyümesi: {rev_text}
• Direnç Riski: {"⚠️ Kritik direnç yakın!" if resistance_risk else "✅ Temiz"}
• Genel Risk Skoru: %{risk_score} - {risk_label}

💡 <b>Karar:</b> {karar}
⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
        return msg

    except Exception as e:
        return None

def scan_loop():
    send_telegram("🦅 <b>Hawk Signal Bot v2.0 Başladı!</b>\n\n4 Katmanlı Filtre Sistemi Aktif\nNASDAQ Taraması Başlıyor...\n\n• Trend Analizi ✅\n• Teknik Sinyal ✅\n• Bilanço Riski ✅\n• Çıkış Stratejisi ✅")

    while True:
        try:
            for ticker in WATCHLIST:
                signal = analyze_stock(ticker)
                if signal:
                    send_telegram(signal)
                    time.sleep(3)
                time.sleep(1)
        except:
            pass
        time.sleep(300)

@app.route("/")
def home():
    return jsonify({"status": "Hawk Signal Bot v2.0 🦅", "watchlist": len(WATCHLIST), "filters": 4})

@app.route("/test")
def test():
    send_telegram("🦅 <b>Hawk Signal Bot v2.0 Aktif!</b>\n\n4 Katmanlı filtre sistemi çalışıyor.\nNASDAQ taraması devam ediyor...")
    return jsonify({"status": "Test mesajı gönderildi!"})

if __name__ == "__main__":
    scanner = threading.Thread(target=scan_loop, daemon=True)
    scanner.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
