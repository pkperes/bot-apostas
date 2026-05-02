#!/usr/bin/env python3
import os, sys, json, asyncio, logging
from datetime import datetime, timezone, time as dtime

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
    for n in ausentes:
        log.error(f"VARIAVEL AUSENTE: {n}")
    sys.exit(1)

log.info("Todas as variaveis carregadas com sucesso.")

HORA_DISPARO = dtime(19, 13)
N_APOSTAS = 10

# Ligas ampliadas para garantir jogos todos os dias
LIGAS_MAP = {
    39: "Premier League", 140: "La Liga", 78: "Bundesliga",
    135: "Serie A", 61: "Ligue 1", 2: "Champions League",
    3: "Europa League", 848: "Conference League",
    71: "Brasileirao Serie A", 72: "Brasileirao Serie B",
    13: "Libertadores", 11: "Sul-Americana",
    94: "Primeira Liga Portugal", 88: "Eredivisie",
    144: "Pro League Belgica", 179: "Scottish Premiership",
    203: "Super Lig Turquia", 307: "Saudi Pro League",
    253: "MLS", 262: "Liga MX",
}

SUPERBET_BASE = "https://superbet.bet.br/apostas-esportivas/futebol"

async def buscar_forma(tid, headers):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://v3.football.api-sports.io/fixtures?team={tid}&last=5", headers=headers)
            jogos = r.json().get("response", [])
        res = []
        for j in jogos:
            gols = j.get("goals", {})
            times = j.get("teams", {})
            casa = (times.get("home", {}).get("id") == tid)
            gm = gols.get("home") if casa else gols.get("away")
            ga = gols.get("away") if casa else gols.get("home")
            if gm is None or ga is None: res.append("?")
            elif gm > ga: res.append("V")
            elif gm < ga: res.append("D")
            else: res.append("E")
        return " ".join(res) or "N/D"
    except Exception:
        return "N/D"

async def buscar_jogos():
    hoje = datetime.now().strftime("%Y-%m-%d")
    agora_utc = datetime.now(timezone.utc)
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    validas = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://v3.football.api-sports.io/fixtures?date={hoje}", headers=headers)
            r.raise_for_status()
            partidas = r.json().get("response", [])
        log.info(f"API-Football: {len(partidas)} partidas encontradas")
        for p in partidas:
            fix = p.get("fixture", {})
            liga = p.get("league", {})
            times = p.get("teams", {})
            lid = liga.get("id", 0)
            if lid not in LIGAS_MAP:
                continue
            status = fix.get("status", {}).get("short", "NS")
            if status not in ("NS", "TBD", "PST"):
                continue
            ds = fix.get("date", "")
            if not ds:
                continue
            try:
                dj = datetime.fromisoformat(ds.replace("Z", "+00:00"))
                if dj <= agora_utc:
                    continue
                hora = dj.astimezone().strftime("%H:%M")
            except Exception:
                hora = "?"
            home = times.get("home", {}).get("name", "?")
            away = times.get("away", {}).get("name", "?")
            nome_liga = LIGAS_MAP[lid]
            validas.append({
                "jogo": f"{home} x {away}",
                "liga": nome_liga,
                "horario": hora,
                "hid": times.get("home", {}).get("id"),
                "aid": times.get("away", {}).get("id"),
                "superbet_url": SUPERBET_BASE,
            })
        if validas:
            fh = await asyncio.gather(*[buscar_forma(v["hid"], headers) for v in validas])
            fa = await asyncio.gather(*[buscar_forma(v["aid"], headers) for v in validas])
            for i, v in enumerate(validas):
                v["forma_home"] = fh[i]
                v["forma_away"] = fa[i]
        log.info(f"Jogos futuros validos: {len(validas)}")
    except Exception as ex:
        log.error(f"Erro API-Football: {ex}")
    return validas

import xml.etree.ElementTree as ET

async def buscar_noticias():
    feeds = [
        "https://news.google.com/rss/search?q=futebol+hoje+jogo&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "https://news.google.com/rss/search?q=football+match+today&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    ]
    noticias = []
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for url in feeds:
            try:
                r = await c.get(url)
                root = ET.fromstring(r.text)
                for item in root.findall(".//item")[:5]:
                    t = item.findtext("title", "").strip()
                    if t:
                        noticias.append(t)
            except Exception:
                pass
    log.info(f"Noticias coletadas: {len(noticias)}")
    return noticias[:10]

def gerar_apostas_ia(jogos, noticias):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Se poucos jogos, pedir menos apostas (minimo 5)
    n_pedir = min(N_APOSTAS, max(5, len(jogos)))

    prompt = (
        f"Voce e um especialista em apostas esportivas. Hoje e {agora} (Brasilia UTC-3).\n"
        f"Gere EXATAMENTE {n_pedir} sugestoes de apostas para os jogos abaixo.\n\n"
        f"CRITERIOS (flexiveis para garantir sugestoes):\n"
        f"- Odd minima: 1.30 (pode ser baixa se for muito segura)\n"
        f"- Confianca minima: 60%\n"
        f"- Mercados preferidos: Resultado (1X2), Dupla Chance, Mais/Menos gols, BTTS, Handicap\n"
        f"- OBRIGATORIO: gere {n_pedir} apostas mesmo que as odds sejam conservadoras\n"
        f"- Analise forma recente: V=vitoria, D=derrota, E=empate (mais recente por ultimo)\n\n"
        f"Retorne UM JSON por linha, sem markdown:\n"
        f'{{"jogo":"A x B","liga":"Liga","horario":"21:00","mercado":"Resultado",'
        f'"sugestao":"Vitoria A","odd":1.85,"confianca":72,"razao":"Motivo breve.",'
        f'"superbet_url":"https://superbet.bet.br/apostas-esportivas/futebol"}}\n\n'
        f"JOGOS DISPONIVEIS:\n{json.dumps(jogos, ensure_ascii=False)}\n\n"
        f"NOTICIAS:\n{json.dumps(noticias, ensure_ascii=False)}"
    )
    try:
        resp = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=3000,
        )
        apostas = []
        for linha in resp.choices[0].message.content.strip().splitlines():
            linha = linha.strip()
            if linha.startswith("{"):
                try:
                    apostas.append(json.loads(linha))
                except Exception:
                    pass
        log.info(f"IA gerou {len(apostas)} apostas")
        return apostas
    except Exception as ex:
        log.error(f"Erro OpenAI: {ex}")
        return []

def montar_acumulador(apostas):
    vistos, cands = set(), []
    for a in sorted(apostas, key=lambda x: x.get("confianca", 0), reverse=True):
        j = a.get("jogo", "")
        if j in vistos: continue
        vistos.add(j)
        cands.append(a)
        if len(cands) == 3: break
    if len(cands) < 3: return None
    odd = round(cands[0]["odd"] * cands[1]["odd"] * cands[2]["odd"], 2)
    return {"apostas": cands, "odd": odd} if odd >= 3.0 else None

def formatar_mensagem(apostas, acum):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    sep = "─" * 30
    linhas = [f"⚽ APOSTAS DO DIA — SUPERBET\n📅 {agora}\n{sep}\n\n"]
    for i, a in enumerate(apostas, 1):
        c = a.get("confianca", 0)
        emoji = "🔥" if c >= 80 else "✅" if c >= 70 else "📌"
        linhas.append(f"{emoji} {i}. {a.get('jogo','')}\n")
        linhas.append(f"   🏆 {a.get('liga','')} | ⏰ {a.get('horario','')}\n")
        linhas.append(f"   📊 {a.get('mercado','')} → {a.get('sugestao','')}\n")
        linhas.append(f"   💰 Odd: {a.get('odd','')}x | Confiança: {c}%\n")
        linhas.append(f"   💡 {str(a.get('razao',''))[:180]}\n")
        url = a.get("superbet_url","")
        if url: linhas.append(f"   🔗 {url}\n\n")
    if acum:
        linhas.append(f"{sep}\n🎯 MINI ACUMULADOR — Odd total: {acum['odd']}x\n")
        for i, a in enumerate(acum["apostas"], 1):
            linhas.append(f"  {i}. {a['jogo']} | {a['mercado']} ({a['odd']}x)\n")
        linhas.append(f"  🔗 {SUPERBET_BASE}\n")
    linhas.append("\n⚠️ Aposte com responsabilidade. Apenas maiores de 18 anos.")
    return "".join(linhas)

async def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram tem limite de 4096 chars por mensagem
    for i in range(0, len(msg), 4000):
        chunk = msg[i:i+4000]
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
            if r.status_code == 200:
                log.info("Telegram: mensagem enviada com sucesso!")
            else:
                log.error(f"Telegram erro {r.status_code}: {r.text[:200]}")
        await asyncio.sleep(0.5)

async def pipeline():
    log.info("=== Iniciando pipeline de apostas ===")
    jogos, noticias = await asyncio.gather(buscar_jogos(), buscar_noticias())
    if not jogos:
        await enviar_telegram("⚽ Bot: nenhum jogo futuro encontrado hoje nas ligas monitoradas.")
        return
    apostas = gerar_apostas_ia(jogos, noticias)
    # Sem filtro rigido — aceita tudo que a IA retornar com odd >= 1.30
    apostas = [a for a in apostas if a.get("odd", 0) >= 1.30][:N_APOSTAS]
    if not apostas:
        await enviar_telegram("⚽ Bot: IA não gerou apostas válidas hoje.")
        return
    await enviar_telegram(formatar_mensagem(apostas, montar_acumulador(apostas)))
    log.info(f"=== Pipeline concluido: {len(apostas)} apostas enviadas ===")

async def main():
    log.info(f"Bot iniciado. Disparo diario as {HORA_DISPARO.strftime('%H:%M')} BRT.")
    ultimo_dia = None
    while True:
        agora = datetime.now()
        if agora.time() >= HORA_DISPARO and agora.date() != ultimo_dia:
            ultimo_dia = agora.date()
            try:
                await pipeline()
            except Exception as ex:
                log.error(f"Erro no pipeline: {ex}")
                try:
                    await enviar_telegram(f"❌ Erro no bot: {ex}")
                except Exception:
                    pass
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
