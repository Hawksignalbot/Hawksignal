import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    requests.post(url, json=payload)

def analyze_signal(data):
    ticker = data.get("ticker", "UNKNOWN")
    close = float(data.get("close", 0))
    rsi = float(data.get("rsi", 0))
    macd = float(data.get("macd", 0))
    macd_signal = float(data.get("macd_signal", 0))
    volume = float(data.get("volume", 0))
    avg_volume = float(data.get("avg_volume", 1))
    ema20 = float(data.get("ema20", 0))
    action = data.get("action", "").upper()

    score = 0
    reasons = []
    warnings = []

    # RSI Analizi
    if action == "BUY":
        if 50 <= rsi <= 65:
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
