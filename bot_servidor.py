#!/usr/bin/env python3
import os, sys, json, asyncio, logging
from datetime import datetime, timezone, time as dtime
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

try:
    import httpx
    from openai import OpenAI
except ImportError:
    os.system(f"{sys.executable} -m pip install httpx openai -q")
    import httpx
    from openai import OpenAI

# ── Diagnostico de ambiente ──────────────────────────────────────────────────
log.info("=== DIAGNOSTICO DE AMBIENTE ===")
variaveis_presentes = [k for k in os.environ if not k.startswith("_")]
log.info(f"Total de variaveis de ambiente: {len(variaveis_presentes)}")
for nome_esperado in ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "API_FOOTBALL_KEY"]:
    valor = os.environ.get(nome_esperado, "")
    if valor:
        log.info(f"  OK: {nome_esperado} = {valor[:8]}...")
    else:
        log.error(f"  AUSENTE: {nome_esperado}")
log.info("=== FIM DO DIAGNOSTICO ===")

# ── Leitura das variaveis ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()

ausentes = [n for n, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "API_FOOTBALL_KEY": API_FOOTBALL_KEY,
}.items() if not v]

if ausentes:
    for nome in ausentes:
        log.error(f"VARIAVEL AUSENTE: {nome} — adicione no painel Variables do Railway")
    log.error("Bot encerrado por falta de variaveis.")
    sys.exit(1)

log.info("Todas as variaveis carregadas com sucesso.")

# ── Configuracoes ────────────────────────────────────────────────────────────
HORA_DISPARO = dtime(14, 30)
MIN_ODD   = 1.5
MIN_CONF  = 75
N_APOSTAS = 10

LIGAS_MAP = {
    39: "Premier League", 140: "La Liga", 78: "Bundesliga",
    135: "Serie A", 61: "Ligue 1", 2: "Champions League",
    3: "Europa League", 71: "Brasileirao Serie A", 13: "Libertadores"
}
SUPERBET_URLS = {
    "Premier League":      "https://superbet.bet.br/apostas-esportivas/futebol/inglaterra/premier-league",
    "La Liga":             "https://superbet.bet.br/apostas-esportivas/futebol/espanha/la-liga",
    "Bundesliga":          "https://superbet.bet.br/apostas-esportivas/futebol/alemanha/bundesliga",
    "Serie A":             "https://superbet.bet.br/apostas-esportivas/futebol/italia/serie-a",
    "Ligue 1":             "https://superbet.bet.br/apostas-esportivas/futebol/franca/ligue-1",
    "Champions League":    "https://superbet.bet.br/apostas-esportivas/futebol/europa/champions-league",
    "Europa League":       "https://superbet.bet.br/apostas-esportivas/futebol/europa/europa-league",
    "Brasileirao Serie A": "https://superbet.bet.br/apostas-esportivas/futebol/brasil/campeonato-brasileiro-serie-a",
    "Libertadores":        "https://superbet.bet.br/apostas-esportivas/futebol/america-do-sul/copa-libertadores",
}
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=futebol+hoje+jogo&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "https://news.google.com/rss/search?q=football+match+today+premier+league&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "https://news.google.com/rss/search?q=lesao+suspensao+futebol+hoje&hl=pt-BR&gl=BR&ceid=BR:pt-419",
]

# ── Funcoes auxiliares ───────────────────────────────────────────────────────
async def buscar_forma(tid, headers):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://v3.football.api-sports.io/fixtures?team={tid}&last=5", headers=headers)
            jogos = r.json().get("response", [])
        res = []
        for j in jogos:
            gols = j.get("goals", {}); times = j.get("teams", {})
            casa = 
