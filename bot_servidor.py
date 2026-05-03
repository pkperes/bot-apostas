#!/usr/bin/env python3
import os, sys, json, asyncio, logging, xml.etree.ElementTree as ET, re, unicodedata
from datetime import datetime, timezone, time as dtime, timedelta

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

MODO_TESTE     = True       # ← mude para False para rodar só no horário agendado
HORA_APOSTAS   = dtime(11, 0)
HORA_RESULTADO = dtime(23, 50)
N_APOSTAS      = 10
SUPERBET_BASE  = "https://superbet.bet.br/apostas-esportivas/futebol"
MODELO_IA      = "gpt-5.4-mini"   # fallback automático para gpt-4o-mini se indisponível

apostas_do_dia = []

LIGAS_BOAS = {
    "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1",
    "UEFA Champions League", "UEFA Europa League", "UEFA Conference League",
    "Brasileirao Serie A", "Brasileirao Serie B", "Copa Libertadores",
    "Copa Sudamericana", "Primeira Liga", "Eredivisie", "Pro League",
    "Scottish Premiership", "Super Lig", "Saudi Pro League", "MLS", "Liga MX",
    "Championship", "League One", "League Two", "Serie B", "2. Bundesliga",
    "Ligue 2", "Segunda Division", "Segunda División",
}

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# ─── URL DIRETA SUPERBET ──────────────────────────────────────────────────────

def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")

def url_superbet(fixture_id, home, away):
    return f"https://superbet.bet.br/odds/futebol/{slugify(home)}-x-{slugify(away)}-{fixture_id}"

# ─── ODDS REAIS DA SUPERBET ───────────────────────────────────────────────────

MERCADO_KEYWORDS = {
    "resultado":    ["resultado final", "1x2"],
    "dupla chance": ["dupla chance"],
    "mais de 1.5":  ["mais de 1.5", "over 1.5"],
    "mais de 2.5":  ["mais de 2.5", "over 2.5"],
    "menos de 2.5": ["menos de 2.5", "under 2.5"],
    "btts":         ["ambas as equipes marcam", "ambas marcam", "btts"],
}

async def buscar_odd_real_superbet(fixture_id, home, away, mercado_ia, odd_ia):
    url = url_superbet(fixture_id, home, away)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS_BROWSER) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return None, url, "indisponivel"

        html   = r.text
        html_l = html.lower()
        todas_odds = [float(o) for o in re.findall(r"\b([1-9]\d*\.\d{2})\b", html)
                      if 1.01 <= float(o) <= 25.0]

        if not todas_odds:
            return None, url, "indisponivel"

        mercado_lower = mercado_ia.lower()
        for chave, kws in MERCADO_KEYWORDS.items():
            if any(kw in mercado_lower for kw in kws):
                for kw in kws:
                    pos = html_l.find(kw)
                    if pos != -1:
                        trecho = html[max(0, pos-30):pos+400]
                        odds_trecho = [float(o) for o in re.findall(r"\b([1-9]\d*\.\d{2})\b", trecho)
                                       if 1.01 <= float(o) <= 25.0]
                        if odds_trecho:
                            odd_real = min(odds_trecho, key=lambda x: abs(x - odd_ia))
                            log.info(f"  ✔ {home} x {away} | {chave}: odd={odd_real}")
                            return odd_real, url, "confirmada"

        return None, url, "indisponivel"

    except Exception as ex:
        log.warning(f"Superbet {home} x {away}: {ex}")
        return None, url, "indisponivel"

async def enriquecer_apostas(apostas):
    """Busca odds reais. SÓ mantém apostas com odd CONFIRMADA."""
    log.info(f"Verificando odds na Superbet para {len(apostas)} apostas...")
    confirmadas = []

    async def processar(a):
        fid     = a.get("fixture_id", 0)
        home    = a.get("home", a.get("jogo","").split(" x ")[0])
        away    = a.get("away", a.get("jogo","").split(" x ")[-1])
        mercado = a.get("mercado","")
        odd_ia  = float(a.get("odd", 1.5))
        odd_real, url, status = await buscar_odd_real_superbet(fid, home, away, mercado, odd_ia)
        a["superbet_url"] = url
        a["odd_status"]   = status
        a["odd_ia"]       = odd_ia
        if status == "confirmada" and odd_real is not None:
            a["odd"] = odd_real
            return a
        return None

    for i in range(0, len(apostas), 3):
        lote = await asyncio.gather(*[processar(a) for a in apostas[i:i+3]])
        confirmadas.extend([a for a in lote if a is not None])
        if i + 3 < len(apostas):
            await asyncio.sleep(1.5)

    log.info(f"Odds confirmadas: {len(confirmadas)}/{len(apostas)}")
    return confirmadas

# ─── API FOOTBALL ─────────────────────────────────────────────────────────────

async def buscar_jogos_data(data_str, headers_api):
    validas = []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://v3.football.api-sports.io/fixtures?date={data_str}", headers=headers_api)
            restantes = r.headers.get("x-ratelimit-requests-remaining", "?")
            log.info(f"API Football [{data_str}] | requests restantes hoje: {restantes}")
            r.raise_for_status()
            partidas = r.json().get("response", [])
        for p in partidas:
            fix   = p.get("fixture", {})
            liga  = p.get("league", {})
            times = p.get("teams", {})
            if fix.get("status",{}).get("short","NS") not in ("NS","TBD","PST"): continue
            ds = fix.get("date","")
            if not ds: continue
            try:
                hora = datetime.fromisoformat(ds.replace("Z","+00:00")).astimezone().strftime("%H:%M")
            except Exception:
                hora = "?"
            home = times.get("home",{}).get("name","?")
            away = times.get("away",{}).get("name","?")
            fid  = fix.get("id",0)
            validas.append({
                "fixture_id": fid, "home": home, "away": away,
                "jogo": f"{home} x {away}",
                "liga": liga.get("name",""), "pais": liga.get("country",""),
                "horario": hora, "data": data_str,
                "superbet_url": url_superbet(fid, home, away),
            })
    except Exception as ex:
        log.error(f"Erro API-Football [{data_str}]: {ex}")
    log.info(f"Jogos NS em {data_str}: {len(validas)}")
    return validas

async def buscar_jogos():
    headers_api = {"x-apisports-key": API_FOOTBALL_KEY}
    hoje   = datetime.now().strftime("%Y-%m-%d")
    amanha = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    res = await asyncio.gather(buscar_jogos_data(hoje, headers_api), buscar_jogos_data(amanha, headers_api))
    todos = res[0] + res[1]
    priorizados = [j for j in todos if j["liga"] in LIGAS_BOAS]
    outros      = [j for j in todos if j["liga"] not in LIGAS_BOAS]
    selecionados = (priorizados + outros)[:50]
    log.info(f"Jogos selecionados: {len(selecionados)}")
    return selecionados

# ─── NOTICIAS ─────────────────────────────────────────────────────────────────

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
                    t = item.findtext("title","").strip()
                    if t: noticias.append(t)
            except Exception: pass
    log.info(f"Noticias: {len(noticias)}")
    return noticias[:10]

# ─── IA ───────────────────────────────────────────────────────────────────────

def gerar_apostas_ia(jogos, noticias):
    agora   = datetime.now().strftime("%d/%m/%Y %H:%M")
    n_pedir = N_APOSTAS * 2  # pede o dobro para compensar descartes
    prompt = (
        f"Voce e um especialista em apostas esportivas. Hoje e {agora} (Brasilia UTC-3).\n"
        f"Gere EXATAMENTE {n_pedir} sugestoes de apostas para os jogos abaixo.\n\n"
        f"CRITERIOS:\n"
        f"- Odd estimada entre 1.30 e 2.50\n"
        f"- Confianca minima: 60%\n"
        f"- Prefira ligas conhecidas: Premier League, Brasileirao, La Liga, etc.\n"
        f"- Mercados aceitos: Resultado (1X2), Dupla Chance, Mais de 1.5 gols, "
        f"Mais de 2.5 gols, Menos de 2.5 gols, BTTS, Handicap\n"
        f"- OBRIGATORIO: gere exatamente {n_pedir} apostas, jogos diferentes\n\n"
        f"Retorne UM JSON por linha, sem markdown. Inclua fixture_id, home e away:\n"
        f'{{"fixture_id":12345,"home":"Arsenal","away":"Chelsea","jogo":"Arsenal x Chelsea",'
        f'"liga":"Premier League","pais":"England","horario":"16:00","data":"2026-05-03",'
        f'"mercado":"Mais de 1.5 gols","sugestao":"Mais de 1.5 gols",'
        f'"odd":1.45,"confianca":75,"razao":"Motivo breve."}}\n\n'
        f"JOGOS:\n{json.dumps(jogos, ensure_ascii=False)}\n\n"
        f"NOTICIAS:\n{json.dumps(noticias, ensure_ascii=False)}"
    )
    def parse_apostas(content):
        result = []
        for linha in content.strip().splitlines():
            linha = linha.strip()
            if linha.startswith("{"):
                try: result.append(json.loads(linha))
                except Exception: pass
        return result

    try:
        resp = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
            model=MODELO_IA,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=4000,
        )
        apostas = parse_apostas(resp.choices[0].message.content)
        log.info(f"IA ({MODELO_IA}) gerou {len(apostas)} apostas")
        return apostas
    except Exception as ex:
        log.error(f"Erro {MODELO_IA}: {ex}")
        try:
            log.info("Fallback: gpt-4o-mini...")
            resp = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4, max_tokens=4000,
            )
            apostas = parse_apostas(resp.choices[0].message.content)
            log.info(f"Fallback gpt-4o-mini: {len(apostas)} apostas")
            return apostas
        except Exception as ex2:
            log.error(f"Fallback falhou: {ex2}")
            return []

# ─── ACUMULADOR ───────────────────────────────────────────────────────────────

def montar_acumulador(apostas):
    vistos, cands = set(), []
    for a in sorted(apostas, key=lambda x: x.get("confianca",0), reverse=True):
        j = a.get("jogo","")
        if j in vistos: continue
        vistos.add(j)
        cands.append(a)
        if len(cands) == 3: break
    if len(cands) < 3: return None
    odd = round(cands[0]["odd"] * cands[1]["odd"] * cands[2]["odd"], 2)
    return {"apostas": cands, "odd": odd} if odd >= 3.0 else None

# ─── MENSAGEM ─────────────────────────────────────────────────────────────────

def formatar_mensagem(apostas, acum):
    agora    = datetime.now().strftime("%d/%m/%Y %H:%M")
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    sep      = "─" * 30
    linhas   = [f"⚽ APOSTAS DO DIA — SUPERBET\n📅 {agora}\n{sep}\n\n"]
    for i, a in enumerate(apostas, 1):
        c      = a.get("confianca", 0)
        emoji  = "🔥" if c >= 80 else "✅" if c >= 70 else "📌"
        dlabel = " (amanhã)" if a.get("data","") != hoje_str else ""
        linhas.append(f"{emoji} {i}. {a.get('jogo','')}\n")
        linhas.append(f"   🏆 {a.get('liga','')} — {a.get('pais','')} | ⏰ {a.get('horario','')}{dlabel}\n")
        linhas.append(f"   📊 {a.get('mercado','')} → {a.get('sugestao','')}\n")
        linhas.append(f"   💰 Odd: {a.get('odd','')}x ✔️ | Confiança: {c}%\n")
        linhas.append(f"   💡 {str(a.get('razao',''))[:180]}\n")
        linhas.append(f"   🔗 {a.get('superbet_url', SUPERBET_BASE)}\n\n")
    if acum:
        linhas.append(f"{sep}\n🎯 MINI ACUMULADOR — Odd total: {acum['odd']}x\n")
        for i, a in enumerate(acum["apostas"], 1):
            linhas.append(f"  {i}. {a['jogo']} | {a['mercado']} ({a['odd']}x)\n")
        linhas.append("\n")
    linhas.append("\n⚠️ Aposte com responsabilidade. Apenas maiores de 18 anos.")
    return "".join(linhas)

# ─── RESULTADOS ───────────────────────────────────────────────────────────────

async def verificar_resultados(apostas_enviadas):
    if not apostas_enviadas: return
    headers_api = {"x-apisports-key": API_FOOTBALL_KEY}
    hoje  = datetime.now().strftime("%Y-%m-%d")
    ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    resultados = {}
    for data in [hoje, ontem]:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"https://v3.football.api-sports.io/fixtures?date={data}", headers=headers_api)
                for p in r.json().get("response", []):
                    fix   = p.get("fixture",{})
                    times = p.get("teams",{})
                    goals = p.get("goals",{})
                    if fix.get("status",{}).get("short","") not in ("FT","AET","PEN"): continue
                    home = times.get("home",{}).get("name","")
                    away = times.get("away",{}).get("name","")
                    gh   = goals.get("home",0) or 0
                    ga   = goals.get("away",0) or 0
                    resultados[f"{home} x {away}"] = {
                        "gh": gh, "ga": ga, "total": gh+ga,
                        "venc": "home" if gh>ga else "away" if ga>gh else "empate",
                        "btts": gh>0 and ga>0,
                    }
        except Exception as ex:
            log.error(f"Erro resultados [{data}]: {ex}")
    if not resultados: return
    acertos = 0
    linhas = ["📊 RESULTADO DAS APOSTAS\n" + "─"*30 + "\n\n"]
    for a in apostas_enviadas:
        jogo     = a.get("jogo","")
        mercado  = a.get("mercado","").lower()
        sugestao = a.get("sugestao","").lower()
        res      = resultados.get(jogo)
        if not res:
            linhas.append(f"⏳ {jogo} — resultado não disponível\n\n"); continue
        gh, ga, total = res["gh"], res["ga"], res["total"]
        venc, btts    = res["venc"], res["btts"]
        acertou = False
        if "mais de 1.5" in mercado:    acertou = total >= 2
        elif "mais de 2.5" in mercado:  acertou = total >= 3
        elif "menos de 2.5" in mercado: acertou = total <= 2
        elif "btts" in mercado or "ambas marcam" in mercado: acertou = btts
        elif "vitoria" in sugestao or "vence" in sugestao:
            th = jogo.split(" x ")[0].lower()
            acertou = venc == ("home" if th in sugestao else "away")
        elif "empate" in sugestao: acertou = venc == "empate"
        elif "dupla chance" in mercado:
            if "1 ou 2" in sugestao: acertou = venc != "empate"
            else: acertou = venc in ("home","empate") or venc in ("away","empate")
        if acertou: acertos += 1
        linhas.append(f"{'✅' if acertou else '❌'} {jogo}\n   {a.get('mercado','')} → {a.get('sugestao','')}\n   Placar: {gh}x{ga}\n\n")
    tot = sum(1 for a in apostas_enviadas if a.get("jogo","") in resultados)
    if tot > 0:
        pct = round(acertos/tot*100)
        linhas.append("─"*30 + f"\n🎯 Acerto: {acertos}/{tot} ({pct}%)\n")
    await enviar_telegram("".join(linhas))

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

async def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(msg), 4000):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg[i:i+4000]})
            if r.status_code == 200: log.info("Telegram: mensagem enviada!")
            else: log.error(f"Telegram erro {r.status_code}: {r.text[:200]}")
        await asyncio.sleep(0.5)

# ─── PIPELINES ────────────────────────────────────────────────────────────────

async def pipeline_apostas():
    global apostas_do_dia
    log.info("=== Iniciando pipeline ===")
    jogos, noticias = await asyncio.gather(buscar_jogos(), buscar_noticias())
    if not jogos:
        await enviar_telegram("⚽ Bot: nenhum jogo encontrado."); return
    apostas = gerar_apostas_ia(jogos, noticias)
    apostas = [a for a in apostas if float(a.get("odd", 0)) >= 1.30]
    if not apostas:
        await enviar_telegram("⚽ Bot: IA não gerou apostas válidas."); return
    apostas = await enriquecer_apostas(apostas)
    if not apostas:
        await enviar_telegram(
            "⚽ Bot: nenhuma odd foi confirmada na Superbet hoje.\n"
            "Verifique manualmente em: " + SUPERBET_BASE
        ); return
    apostas = apostas[:N_APOSTAS]
    apostas_do_dia = apostas
    await enviar_telegram(formatar_mensagem(apostas, montar_acumulador(apostas)))
    log.info(f"=== {len(apostas)} apostas enviadas com odds confirmadas ===")

async def pipeline_resultado():
    log.info("=== Verificando resultados ===")
    if not apostas_do_dia: return
    await verificar_resultados(apostas_do_dia)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    global apostas_do_dia
    log.info(f"Bot v7 | Modelo: {MODELO_IA} | Apostas: {HORA_APOSTAS.strftime('%H:%M')} | Resultados: {HORA_RESULTADO.strftime('%H:%M')} BRT")
    if MODO_TESTE:
        log.info("MODO TESTE — rodando agora!")
        try: await pipeline_apostas()
        except Exception as ex:
            log.error(f"Erro: {ex}")
            try: await enviar_telegram(f"Erro no bot: {ex}")
            except Exception: pass
    ultimo_apostas   = datetime.now().date() if MODO_TESTE else None
    ultimo_resultado = None
    while True:
        agora = datetime.now()
        hoje  = agora.date()
        if agora.time() >= HORA_APOSTAS and hoje != ultimo_apostas:
            ultimo_apostas = hoje
            try: await pipeline_apostas()
            except Exception as ex:
                log.error(f"Erro: {ex}")
                try: await enviar_telegram(f"Erro: {ex}")
                except Exception: pass
        if agora.time() >= HORA_RESULTADO and hoje != ultimo_resultado:
            ultimo_resultado = hoje
            try: await pipeline_resultado()
            except Exception as ex: log.error(f"Erro: {ex}")
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
