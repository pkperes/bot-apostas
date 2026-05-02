#!/usr/bin/env python3
import os, sys, json, asyncio, logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    os.system(f"{sys.executable} -m pip install httpx -q")
    import httpx

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()

async def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(msg), 4000):
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg[i:i+4000]})

async def diagnostico():
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    relatorio = ["🔍 DIAGNÓSTICO DA API\n\n"]

    for delta in [0, 1, 2]:
        data = (datetime.now() + timedelta(days=delta)).strftime("%Y-%m-%d")
        label = ["HOJE", "AMANHÃ", "DEPOIS DE AMANHÃ"][delta]
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"https://v3.football.api-sports.io/fixtures?date={data}", headers=headers)
                dados = r.json()

            total = len(dados.get("response", []))
            erros = dados.get("errors", {})
            restante = dados.get("results", 0)

            relatorio.append(f"📅 {label} ({data}): {total} partidas\n")

            if erros:
                relatorio.append(f"  ❌ Erros: {erros}\n")

            # Conta status
            status_count = {}
            ligas_count = {}
            for p in dados.get("response", []):
                s = p.get("fixture",{}).get("status",{}).get("short","?")
                status_count[s] = status_count.get(s, 0) + 1
                l = p.get("league",{}).get("name","?")
                ligas_count[l] = ligas_count.get(l, 0) + 1

            if status_count:
                relatorio.append(f"  Status: {status_count}\n")
            top5 = sorted(ligas_count.items(), key=lambda x: x[1], reverse=True)[:5]
            if top5:
                relatorio.append(f"  Top ligas: {top5}\n")

        except Exception as ex:
            relatorio.append(f"📅 {label} ({data}): ERRO — {ex}\n")

        relatorio.append("\n")

    # Verifica limite da API
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://v3.football.api-sports.io/status", headers=headers)
            status = r.json().get("response", {})
        plano = status.get("subscription", {})
        requests = status.get("requests", {})
        relatorio.append(f"📊 Plano: {plano.get('plan','?')}\n")
        relatorio.append(f"📊 Requests hoje: {requests.get('current','?')}/{requests.get('limit_day','?')}\n")
    except Exception as ex:
        relatorio.append(f"📊 Status API: ERRO — {ex}\n")

    msg = "".join(relatorio)
    log.info(msg)
    await enviar_telegram(msg)

asyncio.run(diagnostico())
