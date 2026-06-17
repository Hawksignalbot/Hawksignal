import os
import time
import requests
import threading
from flask import Flask, jsonify
from datetime import datetime
import yfinance as yf
import pandas as pd
import pandas_ta as ta

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# NASDAQ'tan en aktif 80 hisse
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "AVGO", "QCOM", "INTC", "MU", "AMAT", "KLAC", "LRCX", "MRVL",
    "ORCL", "CRM", "NOW", "SNOW", "PLTR", "DDOG", "NET", "CRWD",
    "ZS", "PANW", "OKTA", "MDB", "ABNB", "UBER", "LYFT", "DASH",
    "SHOP", "SPOT", "NFLX", "DIS", "ROKU", "TTD", "TRADE", "APPS",
    "PYPL", "SQ", "COIN", "HOOD", "SOFI", "AFRM", "UPST", "LC",
    "MRNA", "BNTX", "PFE", "GILD", "BIIB", "REGN", "VRTX", "ILMN",
    "ENPH", "FSLR", "SEDG", "RUN", "PLUG", "BE", "CHPT", "BLNK",
    "RIVN", "LCID", "NIO", "LI", "XPEV", "FSR", "WKHS", "GOEV",
    "RBLX", "U", "MTTR", "ASGN", "IONQ", "QUBT", "RGTI", "ARQQ"
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def analyze_stock(ticker):
    try:
        df = yf.download(ticker, period="60d", interval="1h", progress=False)
        if df is None or len(df) < 50:
            return None

        # Göstergeleri hesapla
        df['RSI'] = ta.rsi(df['Close'], length=14)
        macd = ta.macd(df['Close'], fast=12, slow=26, signal=9)
        df['MACD'] = macd['MACD_12_26_9']
        df['MACD_Signal'] = macd['MACDs_12_26_9']
        df['EMA20'] = ta.ema(df['Close'], length=20)
        df['Vol_MA'] = df['Volume'].rolling(20).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        rsi = float(last['RSI'])
        macd_val = float(last['MACD'])
        macd_sig = float(last['MACD_Signal'])
        macd_prev = float(prev['MACD'])
        macd_sig_prev = float(prev['MACD_Signal'])
        close = float(last['Close'])
        ema20 = float(last['EMA20'])
        volume = float(last['Volume'])
        avg_volume = float(last['Vol_MA'])

        score = 0
        reasons = []
        action = None

        # ALIM sinyali kriterleri
        buy_score = 0
        if 50 <= rsi <= 65:
            buy_score += 2
            reasons.append(f"✅ RSI: {rsi:.1f} (ideal)")
        elif 45 <= rsi < 50:
            buy_score += 1
            reasons.append(f"✅ RSI: {rsi:.1f} (toparlanıyor)")

        # MACD crossover (önceki bar altında, şimdi üstünde)
        if macd_prev < macd_sig_prev and macd_val > macd_sig:
            buy_score += 3
            reasons.append(f"✅ MACD: Taze crossover! 🔥")
        elif macd_val > macd_sig:
            buy_score += 1
            reasons.append(f"✅ MACD: Pozitif")

        if close > ema20:
            buy_score += 2
            reasons.append(f"✅ EMA20 üstünde")

        vol_ratio = volume / avg_volume if avg_volume > 0 else 0
        if vol_ratio >= 2.0:
            buy_score += 2
            reasons.append(f"✅ Hacim: {vol_ratio:.1f}x 🔥")
        elif vol_ratio >= 1.5:
            buy_score += 1
            reasons.append(f"✅ Hacim: {vol_ratio:.1f}x")

        if buy_score >= 6:
            action = "BUY"
            score = buy_score

        if action == "BUY":
            entry = close
            stop = round(entry * 0.98, 2)
            target1 = round(entry * 1.03, 2)
            target2 = round(entry * 1.05, 2)
            risk = round(entry - stop, 2)
            reward = round(target2 - entry, 2)
            rr = round(reward / risk, 1) if risk > 0 else 0

            if score >= 8:
                quality = "A+ 🏆"
            elif score >= 6:
                quality = "A ✨"
            else:
                quality = "B 👍"

            msg = f"""
🟢 <b>ALIM SİNYALİ — {ticker}</b>

📊 <b>Analiz:</b>
{chr(10).join(reasons)}

💰 <b>İşlem Planı:</b>
• Giriş: <b>${entry:.2f}</b>
• Stop: <b>${stop:.2f}</b> (%2)
• Hedef 1: <b>${target1:.2f}</b> (%3)
• Hedef 2: <b>${target2:.2f}</b> (%5)

⚖️ Risk/Ödül: <b>1:{rr}</b>
🎯 Setup: <b>{quality}</b> ({score}/9 puan)
⏰ {datetime.now().strftime('%H:%M')}
"""
            return msg

        return None

    except Exception as e:
        return None

def scan_loop():
    send_telegram("🦅 <b>Hawk Signal Bot başladı!</b>\n\nNASDAQ taraması başlıyor... Her 5 dakikada sinyal aranacak.")
    
    while True:
        try:
            signals_found = 0
            for ticker in WATCHLIST:
                signal = analyze_stock(ticker)
                if signal:
                    send_telegram(signal)
                    signals_found += 1
                    time.sleep(2)
                time.sleep(0.5)
            
            if signals_found == 0:
                # Her 30 dakikada bir "tarama devam ediyor" mesajı
                pass
                
        except Exception as e:
            pass
        
        time.sleep(300)  # 5 dakika bekle

@app.route("/")
def home():
    return jsonify({"status": "Hawk Signal Bot çalışıyor 🦅", "watchlist": len(WATCHLIST)})

@app.route("/test")
def test():
    send_telegram("🦅 <b>Hawk Signal Bot aktif!</b>\n\nNASDAQ'ta sinyal aranıyor...")
    return jsonify({"status": "Test mesajı gönderildi!"})

@app.route("/scan")
def manual_scan():
    send_telegram("🔍 Manuel tarama başlatıldı...")
    threading.Thread(target=lambda: [analyze_stock(t) for t in WATCHLIST[:10]]).start()
    return jsonify({"status": "Tarama başladı"})

if __name__ == "__main__":
    scanner = threading.Thread(target=scan_loop, daemon=True)
    scanner.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)        if 50 <= rsi <= 65:
            score += 2
            reasons.append(f"✅ RSI: {rsi:.1f} (ideal alım bölgesi)")
        elif 65 < rsi <= 75:
            score += 1
            reasons.append(f"⚠️ RSI: {rsi:.1f} (yüksek ama kabul edilebilir)")
        elif rsi > 75:
            warnings.append(f"🔴 RSI: {rsi:.1f} (aşırı alım - riskli)")
        else:
            score += 1
            reasons.append(f"✅ RSI: {rsi:.1f} (momentum başlıyor)")
    else:
        if rsi > 70:
            score += 2
            reasons.append(f"✅ RSI: {rsi:.1f} (aşırı alım - satış zamanı)")
        elif rsi > 60:
            score += 1
            reasons.append(f"⚠️ RSI: {rsi:.1f} (zayıflıyor)")

    # MACD Analizi
    macd_diff = macd - macd_signal
    if action == "BUY":
        if macd_diff > 0:
            score += 2
            reasons.append(f"✅ MACD: Pozitif crossover ({macd_diff:+.3f})")
        else:
            warnings.append(f"⚠️ MACD: Henüz crossover yok ({macd_diff:+.3f})")
    else:
        if macd_diff < 0:
            score += 2
            reasons.append(f"✅ MACD: Negatif crossover ({macd_diff:+.3f})")

    # Hacim Analizi
    vol_ratio = volume / avg_volume if avg_volume > 0 else 0
    if vol_ratio >= 2.0:
        score += 2
        reasons.append(f"✅ Hacim: Ortalamanın {vol_ratio:.1f}x üstünde 🔥")
    elif vol_ratio >= 1.5:
        score += 1
        reasons.append(f"✅ Hacim: Ortalamanın {vol_ratio:.1f}x üstünde")
    else:
        warnings.append(f"⚠️ Hacim: Zayıf ({vol_ratio:.1f}x ortalama)")

    # EMA20 Analizi
    if action == "BUY":
        if close > ema20:
            score += 2
            reasons.append(f"✅ Fiyat EMA20 üstünde (${close:.2f} > ${ema20:.2f})")
        else:
            warnings.append(f"🔴 Fiyat EMA20 altında (${close:.2f} < ${ema20:.2f})")
    else:
        if close < ema20:
            score += 2
            reasons.append(f"✅ Fiyat EMA20 altında (${close:.2f} < ${ema20:.2f})")

    # Giriş/Stop/Hedef Hesaplama
    if action == "BUY":
        entry = close
        stop = round(entry * 0.98, 2)       # %2 stop
        target1 = round(entry * 1.03, 2)    # %3 hedef
        target2 = round(entry * 1.05, 2)    # %5 hedef
        risk = round(entry - stop, 2)
        reward = round(target2 - entry, 2)
        rr_ratio = round(reward / risk, 1) if risk > 0 else 0

        emoji = "🟢"
        action_text = "ALIM SİNYALİ"
    else:
        entry = close
        stop = round(entry * 1.02, 2)
        target1 = round(entry * 0.97, 2)
        target2 = round(entry * 0.95, 2)
        risk = round(stop - entry, 2)
        reward = round(entry - target2, 2)
        rr_ratio = round(reward / risk, 1) if risk > 0 else 0

        emoji = "🔴"
        action_text = "SATIM SİNYALİ"

    # Setup kalitesi
    if score >= 7:
        quality = "A+ Setup 🏆"
    elif score >= 5:
        quality = "A Setup ✨"
    elif score >= 3:
        quality = "B Setup 👍"
    else:
        quality = "C Setup ⚠️ (Dikkatli ol)"

    message = f"""
{emoji} <b>{action_text} — {ticker}</b>

📊 <b>Teknik Analiz:</b>
{chr(10).join(reasons)}
{chr(10).join(warnings) if warnings else ""}

💰 <b>İşlem Planı:</b>
• Giriş: <b>${entry:.2f}</b>
• Stop-Loss: <b>${stop:.2f}</b> (%2 risk)
• Hedef 1: <b>${target1:.2f}</b> (%3)
• Hedef 2: <b>${target2:.2f}</b> (%5)

⚖️ Risk/Ödül: <b>1:{rr_ratio}</b>
🎯 Setup Kalitesi: <b>{quality}</b> ({score}/8 puan)

⏰ Sinyal zamanı: {data.get("time", "—")}
"""
    return message, score

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No data"}), 400

        message, score = analyze_signal(data)

        # Sadece 3+ puan alan setup'ları gönder
        if score >= 3:
            send_telegram(message)
            return jsonify({"status": "signal sent", "score": score}), 200
        else:
            send_telegram(f"⚫ {data.get('ticker','?')} — Zayıf setup ({score}/8), sinyal geçildi.")
            return jsonify({"status": "weak signal", "score": score}), 200

    except Exception as e:
        send_telegram(f"❌ Hata: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/test", methods=["GET"])
def test():
    send_telegram("🦅 <b>Hawk Signal Bot aktif!</b>\n\nTradingView sinyalleri bekleniyor...")
    return jsonify({"status": "Hawk Signal Bot çalışıyor!"}), 200

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Hawk Signal Bot is running 🦅"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
