#!/usr/bin/env python3
import os, sys, json, asyncio, logging, xml.etree.ElementTree as ET, re, unicodedata
from datetime import datetime, time as dtime, timedelta

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

try:
    import httpx
    from openai import OpenAI
except ImportError:
    os.system(f"{sys.executable} -m pip install httpx openai -q")
    import httpx
    from openai import OpenAI

log.info("=== DIAGNOSTICO DE AMBIENTE ===")
for nome in ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "API_FOOTBALL_KEY"]:
    v = os.environ.get(nome, "")
    log.info(f"  {'OK' if v else 'AUSENTE'}: {nome}{' = ' + v[:8] + '...' if v else ''}")
log.info("=== FIM DO DIAGNOSTICO ===")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()

ausentes = [n for n, v in {"TELEGRAM_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "OPENAI_API_KEY": OPENAI_API_KEY, "API_FOOTBALL_KEY": API_FOOTBALL_KEY}.items() if not v]
if ausentes:
    for n in ausentes: log.error(f"VARIAVEL AUSENTE: {n}")
    sys.exit(1)

log.info("Todas as variaveis carregadas com sucesso.")

MODO_TESTE     = True
HORA_APOSTAS   = dtime(11, 0)
HORA_RESULTADO = dtime(23, 50)
N_APOSTAS      = 10
SUPERBET_BASE  = "https://superbet.bet.br/apostas-esportivas/futebol"
MODELO_IA      = "gpt-5.4-mini"

apostas_do_dia = []

LIGAS_BOAS = {
    "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1",
    "UEFA Champions League", "UEFA Europa League", "UEFA Conference League",
    "Brasileirao Serie A", "Brasileirao Serie B", "Copa Libertadores",
    "Copa Sudamericana", "Primeira Liga", "Eredivisie", "Pro League",
    "Scottish Premiership", "Super Lig", "Saudi Pro League", "MLS", "Liga MX",
    "Champions
