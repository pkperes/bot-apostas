#!/usr/bin/env python3
import os, sys, json, asyncio, logging, xml.etree.ElementTree as ET
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

MODO_TESTE = True
HORA_DISPARO = dtime(11, 0)
N_APOSTAS = 10
SUPERBET_BASE = "https://superbet.bet.br/apostas-esportivas/futebol"

# SEM FILTRO DE LIGA — aceita qualquer liga com jogos
# So filtra por paises/competicoes de baixissima qualidade
LIGAS_EXCLUIR = {
    # Ligas muito obscuras ou de qualidade muito baixa
    666, 667, 668, 669, 670  # IDs fictícios de ligas amadoras
}

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

async def buscar_jogos_data(data_str, headers):
    validas = []
    ligas_encontradas = {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://v3.football.api-sports.io/fixtures?date={data_str}", headers=headers)
            r.raise_for_status()
            partidas = r.json().get("response", [])
        log.info(f"API-Football [{data_str}]: {len(partidas)} partidas totais")

        for p in partidas:
            fix = p.get("fixture", {})
            liga = p.get("league", {})
            times = p.get("teams", {})
            lid = liga.get("id", 0)
            nome_liga = liga.get("name", "Desconhecida")
            pais = liga.get("country", "")
            status = fix.get("status", {}).get("short", "NS")

            # Conta ligas encontradas para debug
            ligas_encontradas[f"{nome_liga} ({pais}) [id:{lid}]"] = ligas_encontradas.get(f"{nome_liga} ({pais}) [id:{lid}]", 0) + 1

            if lid in LIGAS_EXCLUIR:
                continue
            if status not in ("NS", "TBD", "PST"):
                continue

            ds = fix.get("date", "")
            if not ds:
                continue
            try:
                dj = datetime.fromisoformat(ds.replace("Z", "+00:00"))
                hora = dj.astimezone().strftime("%H:%M")
            except Exception:
                hora = "?"

            home = times.get("home", {}).get("name", "?")
            away = times.get("away", {}).get("name", "?")

            validas.append({
                "jogo": f"{home} x {away}",
                "liga": nome_liga,
                "pais": pais,
                "horario": hora,
                "data": data_str,
                "hid": times.get("home", {}).get("id"),
                "aid": times.get("away", {}).get("id"),
                "superbet_url": SUPERBET_BASE,
            })

        # Log das top ligas com mais jogos
        top_ligas = sorted(ligas_encontradas.items(), key=lambda x: x[1], reverse=True)[:15]
        log.info(f"Top ligas em {data_str}:")
        for nome, qtd in top_ligas:
            log.info(f"  {qtd}x {nome}")

    except Exception as ex:
        log.error(f"Erro API-Football [{data_str}]: {ex}")
    log.info(f"Jogos validos em {data_str}: {len(validas)}")
    return validas

async def buscar_jogos():
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    hoje = datetime.now().strftime("%Y-%m-%d")
    amanha = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    resultados = await asyncio.gather(
        buscar_jogos_data(hoje, headers),
        buscar_jogos_data(amanha, headers)
    )
    validas = resultados[0] + resultados[1]
    if validas:
        fh = await asyncio.gather(*[buscar_forma(v["hid"], headers) for v in validas])
        fa = await asyncio.gather(*[buscar_forma(v["aid"], headers) for v in validas])
        for i, v in enumerate(validas):
            v["forma_home"] = fh[i]
            v["forma_away"] = fa[i]
    log.info(f"TOTAL jogos (hoje+amanha): {len(validas)}")
    return validas

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
                    if t: noticias.append(t)
            except Exception:
                pass
    log.info(f"Noticias coletadas: {len(noticias)}")
    return noticias[:10]

def gerar_apostas_ia(jogos, noticias):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    n_pedir = min(N_APOSTAS, max(5, len(jogos)))
    prompt = (
        f"Voce e um especialista em apostas esportivas. Hoje e {agora} (Brasilia UTC-3).\n"
        f"Gere EXATAMENTE {n_pedir} sugestoes de apostas para os jogos abaixo.\n\n"
        f"CRITERIOS:\n"
        f"- Odd minima: 1.30\n"
        f"- Confianca minima: 60%\n"
        f"- Mercados: Resultado (1X2), Dupla Chance, Mais/Menos gols, BTTS, Handicap\n"
        f"- OBRIGATORIO: gere {n_pedir} apostas mesmo conservadoras\n\n"
        f"Retorne UM JSON por linha, sem markdown:\n"
        f'{{"jogo":"A x B","liga":"Liga","pais":"Brasil","horario":"21:00","data":"2026-05-03",'
        f'"mercado":"Resultado","sugestao":"Vitoria A","odd":1.85,"confianca":72,"razao":"Motivo.",'
        f'"superbet_url":"https://superbet.bet.br/apostas-esportivas/futebol"}}\n\n'
        f"JOGOS:\n{json.dumps(jogos[:50], ensure_ascii=False)}\n\n"
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
                try: apostas.append(json.loads(linha))
                except Exception: pass
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
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    linhas = [f"⚽ APOSTAS DO DIA — SUPERBET\n📅 {agora}\n{sep}\n\n"]
    for i, a in enumerate(apostas, 1):
        c = a.get("confianca", 0)
        emoji = "🔥" if c >= 80 else "✅" if c >= 70 else "📌"
        data_label = " (amanhã)" if a.get("data","") != hoje_str else ""
        linhas.append(f"{emoji} {i}. {a.get('jogo','')}\n")
        linhas.append(f"   🏆 {a.get('liga','')} — {a.get('pais','')} | ⏰ {a.get('horario','')}{data_label}\n")
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
    for i in range(0, len(msg), 4000):
        chunk = msg[i:i+4000]
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
            if r.status_code == 200: log.info("Telegram: mensagem enviada!")
            else: log.error(f"Telegram erro {r.status_code}: {r.text[:200]}")
        await asyncio.sleep(0.5)

async def pipeline():
    log.info("=== Iniciando pipeline de apostas ===")
    jogos, noticias = await asyncio.gather(buscar_jogos(), buscar_noticias())
    if not jogos:
        await enviar_telegram("⚽ Bot: nenhum jogo encontrado hoje nem amanha.")
        return
    apostas = gerar_apostas_ia(jogos, noticias)
    apostas = [a for a in apostas if a.get("odd", 0) >= 1.30][:N_APOSTAS]
    if not apostas:
        await enviar_telegram("⚽ Bot: IA nao gerou apostas validas.")
        return
    await enviar_telegram(formatar_mensagem(apostas, montar_acumulador(apostas)))
    log.info(f"=== Pipeline concluido: {len(apostas)} apostas enviadas ===")

async def main():
    log.info(f"Bot iniciado. Disparo diario as {HORA_DISPARO.strftime('%H:%M')} BRT.")
    if MODO_TESTE:
        log.info("MODO TESTE — rodando pipeline imediatamente!")
        try:
            await pipeline()
        except Exception as ex:
            log.error(f"Erro: {ex}")
            try: await enviar_telegram(f"Erro no bot: {ex}")
            except Exception: pass
    ultimo_dia = datetime.now().date() if MODO_TESTE else None
    while True:
        agora = datetime.now()
        if agora.time() >= HORA_DISPARO and agora.date() != ultimo_dia:
            ultimo_dia = agora.date()
            try:
                await pipeline()
            except Exception as ex:
                log.error(f"Erro: {ex}")
                try: await enviar_telegram(f"Erro no bot: {ex}")
                except Exception: pass
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
