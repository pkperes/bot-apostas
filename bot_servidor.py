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

# Leitura segura das variaveis — mostra erro claro se faltar alguma
def ler_var(nome):
    val = os.getenv(nome, "").strip()
    if not val:
        log.error(f"VARIAVEL AUSENTE: {nome} — adicione no painel Variables do Railway")
    else:
        log.info(f"OK: {nome} carregada ({val[:6]}...)")
    return val

TELEGRAM_TOKEN   = ler_var("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = ler_var("TELEGRAM_CHAT_ID")
OPENAI_API_KEY   = ler_var("OPENAI_API_KEY")
API_FOOTBALL_KEY = ler_var("API_FOOTBALL_KEY")

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY, API_FOOTBALL_KEY]):
    log.error("Uma ou mais variaveis estao faltando. Bot encerrado.")
    sys.exit(1)

HORA_DISPARO = dtime(14, 30)
MIN_ODD   = 1.5
MIN_CONF  = 75
N_APOSTAS = 10

LIGAS_MAP = {
    39:"Premier League",140:"La Liga",78:"Bundesliga",
    135:"Serie A",61:"Ligue 1",2:"Champions League",
    3:"Europa League",71:"Brasileirao Serie A",13:"Libertadores"
}
SUPERBET_URLS = {
    "Premier League":       "https://superbet.bet.br/apostas-esportivas/futebol/inglaterra/premier-league",
    "La Liga":              "https://superbet.bet.br/apostas-esportivas/futebol/espanha/la-liga",
    "Bundesliga":           "https://superbet.bet.br/apostas-esportivas/futebol/alemanha/bundesliga",
    "Serie A":              "https://superbet.bet.br/apostas-esportivas/futebol/italia/serie-a",
    "Ligue 1":              "https://superbet.bet.br/apostas-esportivas/futebol/franca/ligue-1",
    "Champions League":     "https://superbet.bet.br/apostas-esportivas/futebol/europa/champions-league",
    "Europa League":        "https://superbet.bet.br/apostas-esportivas/futebol/europa/europa-league",
    "Brasileirao Serie A":  "https://superbet.bet.br/apostas-esportivas/futebol/brasil/campeonato-brasileiro-serie-a",
    "Libertadores":         "https://superbet.bet.br/apostas-esportivas/futebol/america-do-sul/copa-libertadores",
}
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=futebol+hoje+jogo&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "https://news.google.com/rss/search?q=football+match+today+premier+league&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "https://news.google.com/rss/search?q=lesao+suspensao+futebol+hoje&hl=pt-BR&gl=BR&ceid=BR:pt-419",
]

async def buscar_forma(tid, headers):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://v3.football.api-sports.io/fixtures?team={tid}&last=5", headers=headers)
            jogos = r.json().get("response", [])
        res = []
        for j in jogos:
            gols=j.get("goals",{}); times=j.get("teams",{})
            casa=(times.get("home",{}).get("id")==tid)
            gm=gols.get("home") if casa else gols.get("away")
            ga=gols.get("away") if casa else gols.get("home")
            if gm is None or ga is None: res.append("?")
            elif gm>ga: res.append("V")
            elif gm<ga: res.append("D")
            else: res.append("E")
        return " ".join(res) or "N/D"
    except Exception: return "N/D"

async def buscar_jogos():
    hoje=datetime.now().strftime("%Y-%m-%d")
    agora_utc=datetime.now(timezone.utc)
    headers={"x-apisports-key": API_FOOTBALL_KEY}
    validas=[]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r=await c.get(f"https://v3.football.api-sports.io/fixtures?date={hoje}", headers=headers)
            r.raise_for_status()
            partidas=r.json().get("response",[])
        log.info(f"API-Football: {len(partidas)} partidas")
        for p in partidas:
            fix=p.get("fixture",{}); liga=p.get("league",{}); times=p.get("teams",{})
            lid=liga.get("id",0)
            if lid not in LIGAS_MAP: continue
            if fix.get("status",{}).get("short","NS") not in ("NS","TBD","PST"): continue
            ds=fix.get("date","")
            if not ds: continue
            try:
                dj=datetime.fromisoformat(ds.replace("Z","+00:00"))
                if dj<=agora_utc: continue
                hora=dj.astimezone().strftime("%H:%M")
            except Exception: hora="?"
            home=times.get("home",{}).get("name","?")
            away=times.get("away",{}).get("name","?")
            nome_liga=LIGAS_MAP[lid]
            validas.append({"jogo":f"{home} x {away}","liga":nome_liga,"horario":hora,
                "hid":times.get("home",{}).get("id"),"aid":times.get("away",{}).get("id"),
                "superbet_url":SUPERBET_URLS.get(nome_liga,"https://superbet.bet.br/apostas-esportivas/futebol")})
        fh=await asyncio.gather(*[buscar_forma(v["hid"],headers) for v in validas])
        fa=await asyncio.gather(*[buscar_forma(v["aid"],headers) for v in validas])
        for i,v in enumerate(validas): v["forma_home"]=fh[i]; v["forma_away"]=fa[i]
        log.info(f"Jogos futuros: {len(validas)}")
    except Exception as ex: log.error(f"Erro API-Football: {ex}")
    return validas

async def buscar_noticias():
    noticias=[]
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent":"Mozilla/5.0"}) as c:
        for url in RSS_FEEDS:
            try:
                r=await c.get(url); r.raise_for_status()
                root=ET.fromstring(r.text)
                for item in root.findall(".//item")[:4]:
                    t=item.findtext("title","").strip()
                    d=item.findtext("description","").strip()[:120]
                    if t: noticias.append(f"{t}. {d}")
            except Exception: pass
    log.info(f"Noticias: {len(noticias)}")
    return noticias[:12]

def gerar_apostas_ia(jogos, noticias):
    agora=datetime.now().strftime("%d/%m/%Y %H:%M")
    prompt=f"""Voce e um analista conservador de apostas esportivas especialista em valor real.
Hoje e {agora} (Brasilia UTC-3). Gere EXATAMENTE {N_APOSTAS} sugestoes para a Superbet.
REGRAS ESTRITAS:
- Apenas jogos AINDA NAO INICIADOS. Odd minima: {MIN_ODD}. Confianca minima: {MIN_CONF}%.
- Priorize: BTTS, Mais de 1.5 gols, Dupla Chance, Handicap asiatico.
- REJEITE times com 3+ derrotas seguidas. REJEITE odds dos dois lados entre 1.7-2.2.
- Para BTTS: ambos times devem ter marcado em 3+ dos ultimos 5 jogos.
- Retorne UM JSON por linha, sem markdown, sem ```.
Formato:
{{"jogo":"A x B","liga":"Premier League","horario":"21:00","mercado":"Mais de 1.5 gols","sugestao":"Mais de 1.5 gols","odd":1.65,"confianca":78,"razao":"Motivo.","superbet_url":"https://superbet.bet.br/..."}}
JOGOS: {json.dumps(jogos,ensure_ascii=False)}
NOTICIAS: {json.dumps(noticias,ensure_ascii=False)}"""
    try:
        resp=OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
            model="gpt-4o-mini", messages=[{"role":"user","content":prompt}],
            temperature=0.35, max_tokens=2400)
        apostas=[]
        for linha in resp.choices[0].message.content.strip().splitlines():
            if linha.strip().startswith("{"):
                try: apostas.append(json.loads(linha.strip()))
                except: pass
        log.info(f"IA gerou {len(apostas)} apostas")
        return apostas
    except Exception as ex:
        log.error(f"Erro OpenAI: {ex}"); return []

def montar_acumulador(apostas):
    vistos=set(); cands=[]
    for a in sorted(apostas,key=lambda x:x.get("confianca",0),reverse=True):
        j=a.get("jogo","")
        if j in vistos: continue
        vistos.add(j); cands.append(a)
        if len(cands)==3: break
    if len(cands)<3: return None
    odd=round(cands[0]["odd"]*cands[1]["odd"]*cands[2]["odd"],2)
    return {"apostas":cands,"odd":odd} if odd>=4.0 else None

def formatar_mensagem(apostas, acum):
    agora=datetime.now().strftime("%d/%m/%Y %H:%M")
    linhas=[f"⚽ *APOSTAS DO DIA — SUPERBET*\n📅 {agora}\n{'─'*28}\n\n"]
    for i,a in enumerate(apostas,1):
        c=a.get("confianca","?"); e="🔥" if c>=85 else "✅"
        linhas.append(f"{e} *{i}. {a.get('jogo','')}* _{a.get('liga','')}_\n")
        linhas.append(f"   {a.get('mercado','')} → *{a.get('sugestao','')}*\n")
        linhas.append(f"   💰 Odd: *{a.get('odd','')}x* | 🎯 {c}%\n")
        linhas.append(f"   📝 _{str(a.get('razao',''))[:200]}_\n")
        url=a.get("superbet_url","")
        if url: linhas.append(f"   🔗 {url}\n\n")
    if acum:
        linhas.append(f"\n{'─'*28}\n🎰 *MINI ACUMULADOR* — Odd: *{acum['odd']}x*\n")
        for i,a in enumerate(acum["apostas"],1):
            linhas.append(f"  {i}. {a['jogo']} | {a['mercado']} ({a['odd']}x)\n")
            linhas.append(f"     🔗 {a.get('superbet_url','')}\n")
    linhas.append("\n⚠️ _Aposte com responsabilidade. 18+._")
    return "".join(linhas)

async def enviar_telegram(msg):
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"})
        if r.status_code==200: log.info("Telegram: enviado!")
        else: log.error(f"Telegram erro {r.status_code}: {r.text[:100]}")

async def pipeline():
    log.info("=== Iniciando pipeline ===")
    jogos,noticias=await asyncio.gather(buscar_jogos(),buscar_noticias())
    if not jogos:
        await enviar_telegram("⚽ Bot: nenhum jogo futuro encontrado hoje."); return
    apostas=gerar_apostas_ia(jogos,noticias)
    apostas=[a for a in apostas if a.get("odd",0)>=MIN_ODD and a.get("confianca",0)>=MIN_CONF][:N_APOSTAS]
    await enviar_telegram(formatar_mensagem(apostas,montar_acumulador(apostas)))
    log.info(f"=== {len(apostas)} apostas enviadas ===")

async def main():
    log.info(f"Bot iniciado. Disparo diario as {HORA_DISPARO.strftime('%H:%M')} BRT.")
    ultimo_dia=None
    while True:
        agora=datetime.now()
        if agora.time()>=HORA_DISPARO and agora.date()!=ultimo_dia:
            ultimo_dia=agora.date()
            try: await pipeline()
            except Exception as ex:
                log.error(f"Erro pipeline: {ex}")
                try: await enviar_telegram(f"⚠️ Erro: `{ex}`")
                except: pass
        await asyncio.sleep(60)

if __name__=="__main__":
    asyncio.run(main())
