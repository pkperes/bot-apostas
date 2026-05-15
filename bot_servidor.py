#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import logging
import xml.etree.ElementTree as ET
import re
import unicodedata
from datetime import datetime, time as dtime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

try:
    import httpx
    from openai import OpenAI
except ImportError:
    os.system(f"{sys.executable} -m pip install httpx openai -q")
    import httpx
    from openai import OpenAI

for nome in ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "FOOTBALL_DATA_KEY"]:
    v = os.environ.get(nome, "")
    log.info(f" {'OK' if v else 'AUSENTE'}: {nome}{' = ' + v[:8] + '...' if v else ''}")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "").strip()

ausentes = [n for n, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "FOOTBALL_DATA_KEY": FOOTBALL_DATA_KEY,
}.items() if not v]

if ausentes:
    for n in ausentes:
        log.error(f"VARIAVEL AUSENTE: {n}")
    sys.exit(1)

MODO_TESTE = False
HORA_APOSTAS = dtime(11, 0)
N_APOSTAS = 10
SUPERBET_BASE = "https://superbet.bet.br/apostas-esportivas/futebol"
MODELO_IA = "gpt-5.4-mini"

LIGAS_BOAS = {
    "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1",
    "UEFA Champions League", "UEFA Europa League", "UEFA Conference League",
    "Brasileirao Serie A", "Brasileirao Serie B", "Copa Libertadores",
    "Copa Sudamericana", "Primeira Liga", "Eredivisie", "Pro League",
    "Scottish Premiership", "Super Lig", "Saudi Pro League", "MLS", "Liga MX",
    "Championship", "League One", "League Two", "Serie B", "2. Bundesliga",
    "Ligue 2", "Segunda Division", "Primeira Liga", "Eredivisie"
}

MARKET_PRIORITY = {
    "menos de 3.5 gols": 1,
    "under 3.5": 1,
    "dupla chance": 1,
    "vence ou empata": 1,
    "ou empate": 1,
    "empate anula": 1,
    "dnb": 1,
    "ambas marcam": 1,
    "btts": 1,

    "mais de 1.5 gols": 2,
    "over 1.5": 2,
    "mais de 2.5 gols": 2,
    "over 2.5": 2,

    "escanteios": 3,
    "corners": 3,
    "escanteio": 3,
    "cart": 3,
    "amarelo": 3,

    "1º tempo": 4,
    "1o tempo": 4,
    "primeiro tempo": 4,
    "ht": 4,
    "intervalo": 4,
}

MIN_ODDS_BY_TIER = {
    1: 1.35,
    2: 1.40,
    3: 1.50,
    4: 1.60,
}


def slugify(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def url_superbet(fixture_id, home, away):
    return f"https://superbet.bet.br/odds/futebol/{slugify(home)}-x-{slugify(away)}-{fixture_id}"


def get_market_tier(mercado_texto: str) -> int:
    if not mercado_texto:
        return 99
    texto_lower = str(mercado_texto).lower().strip()
    for key, tier in MARKET_PRIORITY.items():
        if key in texto_lower:
            return tier
    return 99


def odd_ok_for_market(mercado_texto: str, odd: float) -> bool:
    tier = get_market_tier(mercado_texto)
    min_odd = MIN_ODDS_BY_TIER.get(tier, 1.40)
    return odd >= min_odd


def selecionar_apostas_priorizadas(candidatos: list, qtd_alvo: int = 10) -> list:
    aprovadas = []
    jogos_usados = set()

    validas = []
    for c in candidatos:
        try:
            odd = float(c.get("odd", 0))
        except Exception:
            odd = 0.0
        if odd_ok_for_market(c.get("mercado", ""), odd):
            validas.append(c)

    log.info(f"Candidatos: {len(candidatos)} | Válidos por odd mínima: {len(validas)}")

    validas_ordenadas = sorted(
        validas,
        key=lambda x: (
            get_market_tier(x.get("mercado", "")),
            -float(x.get("confianca", 0) or 0),
            -float(x.get("odd", 0) or 0)
        )
    )

    for tier in [1, 2, 3, 4, 99]:
        tier_items = [
            item for item in validas_ordenadas
            if get_market_tier(item.get("mercado", "")) == tier
        ]

        for item in tier_items:
            if len(aprovadas) >= qtd_alvo:
                break

            jogo = item.get("jogo", "")
            if jogo not in jogos_usados:
                aprovadas.append(item)
                jogos_usados.add(jogo)

        if len(aprovadas) >= qtd_alvo:
            break

    if len(aprovadas) < qtd_alvo:
        log.warning(f"Apenas {len(aprovadas)} apostas únicas. Permitindo múltiplos mercados...")
        mercados_usados = {f"{a.get('jogo','')}_{a.get('mercado','')}" for a in aprovadas}

        for item in validas_ordenadas:
            if len(aprovadas) >= qtd_alvo:
                break

            chave = f"{item.get('jogo', '')}_{item.get('mercado', '')}"
            if chave not in mercados_usados:
                aprovadas.append(item)
                mercados_usados.add(chave)

    log.info(
        f"Seleção: {len(aprovadas)} apostas | "
        f"Tier 1: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado','')) == 1)} | "
        f"Tier 2: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado','')) == 2)} | "
        f"Tier 3: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado','')) == 3)} | "
        f"Tier 4+: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado','')) >= 4)}"
    )
    return aprovadas


def normalizar_liga(nome):
    n = str(nome or "").strip()
    mapa = {
        "Campeonato Brasileiro Série A": "Brasileirao Serie A",
        "Brazilian Serie A": "Brasileirao Serie A",
        "Premier League": "Premier League",
        "Primera Division": "La Liga",
        "La Liga": "La Liga",
        "Bundesliga": "Bundesliga",
        "Serie A": "Serie A",
        "Ligue 1": "Ligue 1",
        "Primeira Liga": "Primeira Liga",
        "Eredivisie": "Eredivisie",
        "Liga MX": "Liga MX",
        "MLS": "MLS",
        "Championship": "Championship",
        "2. Bundesliga": "2. Bundesliga",
        "Serie B": "Serie B",
        "UEFA Champions League": "UEFA Champions League",
        "UEFA Europa League": "UEFA Europa League",
        "UEFA Conference League": "UEFA Conference League",
    }
    return mapa.get(n, n)


async def buscar_jogos_football_data(data_alvo: str, data_ate: str):
    validas = []
    vistos = set()

    headers_fd = {
        "X-Auth-Token": FOOTBALL_DATA_KEY,
        "User-Agent": "Mozilla/5.0",
    }

    competicoes = ["CL", "PL", "BL1", "SA", "PD", "FL1", "EL", "DED", "BSA", "PPL", "EC", "WC"]

    def processar(m, liga_override=""):
        ds = m.get("utcDate", "")
        if not ds:
            return None

        try:
            dt_jogo = datetime.fromisoformat(ds.replace("Z", "+00:00")).astimezone()
        except Exception:
            return None

        status = m.get("status", "")
        if status not in ("SCHEDULED", "TIMED", "POSTPONED"):
            return None

        home = m.get("homeTeam", {}).get("name") or m.get("homeTeam", {}).get("shortName") or "?"
        away = m.get("awayTeam", {}).get("name") or m.get("awayTeam", {}).get("shortName") or "?"
        fid = str(m.get("id", "0"))
        chave = (home.lower(), away.lower(), dt_jogo.strftime("%Y-%m-%d %H:%M"))

        if chave in vistos:
            return None
        vistos.add(chave)

        liga = normalizar_liga(liga_override or m.get("competition", {}).get("name", ""))
        pais = m.get("area", {}).get("name", "")

        return {
            "fixture_id": fid,
            "home": home,
            "away": away,
            "jogo": f"{home} x {away}",
            "liga": liga,
            "pais": pais,
            "horario": dt_jogo.strftime("%H:%M"),
            "data": dt_jogo.strftime("%Y-%m-%d"),
            "superbet_url": url_superbet(fid, home, away),
        }

    async def get_matches(url, client):
        try:
            r = await client.get(url, headers=headers_fd)
            log.info(f"FD {url[:110]}... | status={r.status_code}")

            if r.status_code == 429:
                log.warning("Rate limit atingido na football-data.org")
                return []
            if r.status_code != 200:
                log.error(f"FD erro {r.status_code}: {r.text[:300]}")
                return []

            data = r.json()
            return data.get("matches", [])
        except Exception as ex:
            log.error(f"Falha football-data.org [{type(ex).__name__}]: {repr(ex)}")
            return []

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            matches = await get_matches(
                f"https://api.football-data.org/v4/matches?dateFrom={data_alvo}&dateTo={data_ate}&status=SCHEDULED,TIMED,POSTPONED",
                c
            )
            log.info(f"FD matches geral {data_alvo}: {len(matches)} jogos")

            for m in matches:
                j = processar(m)
                if j:
                    validas.append(j)

            if not validas:
                log.info("FD sem jogos geral. Tentando por competicao...")
                for comp in competicoes:
                    ms = await get_matches(
                        f"https://api.football-data.org/v4/competitions/{comp}/matches?dateFrom={data_alvo}&dateTo={data_ate}&status=SCHEDULED,TIMED,POSTPONED",
                        c
                    )
                    log.info(f"FD {comp}: {len(ms)} jogos")
                    for m in ms:
                        j = processar(m)
                        if j:
                            validas.append(j)
    except Exception as ex:
        log.error(f"Erro football-data.org [buscar_jogos_football_data] [{type(ex).__name__}]: {repr(ex)}")

    return validas


async def buscar_jogos_thesportsdb(data_alvo: str):
    validas = []
    vistos = set()

    ligas = [
        ("English Premier League", "Premier League", "England"),
        ("Spanish La Liga", "La Liga", "Spain"),
        ("German Bundesliga", "Bundesliga", "Germany"),
        ("Italian Serie A", "Serie A", "Italy"),
        ("French Ligue 1", "Ligue 1", "France"),
        ("Brazilian Serie A", "Brasileirao Serie A", "Brazil"),
        ("Portuguese Primeira Liga", "Primeira Liga", "Portugal"),
        ("Dutch Eredivisie", "Eredivisie", "Netherlands"),
    ]

    async def get_events_by_league(client, league_name):
        url = "https://www.thesportsdb.com/api/v1/json/3/eventsday.php"
        params = {"d": data_alvo, "l": league_name}
        try:
            r = await client.get(url, params=params)
            log.info(f"TSD {league_name} | status={r.status_code}")
            if r.status_code != 200:
                log.error(f"TSD erro {r.status_code}: {r.text[:300]}")
                return []
            data = r.json()
            return data.get("events") or []
        except Exception as ex:
            log.error(f"Falha TheSportsDB [{league_name}] [{type(ex).__name__}]: {repr(ex)}")
            return []

    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as c:
            for league_api_name, liga_normalizada, pais in ligas:
                eventos = await get_events_by_league(c, league_api_name)
                log.info(f"TSD {league_api_name}: {len(eventos)} jogos")

                for ev in eventos:
                    home = (ev.get("strHomeTeam") or "").strip()
                    away = (ev.get("strAwayTeam") or "").strip()
                    ds = ev.get("dateEvent")
                    ts = ev.get("strTime") or "00:00:00"
                    eid = str(ev.get("idEvent") or "0")

                    if not home or not away or not ds:
                        continue

                    try:
                        dt_utc = datetime.fromisoformat(f"{ds}T{ts.replace('Z', '')}")
                        dt_local = dt_utc.replace(tzinfo=timezone.utc).astimezone()
                    except Exception:
                        try:
                            dt_local = datetime.strptime(ds, "%Y-%m-%d")
                        except Exception:
                            continue

                    chave = (home.lower(), away.lower(), dt_local.strftime("%Y-%m-%d %H:%M"))
                    if chave in vistos:
                        continue
                    vistos.add(chave)

                    validas.append({
                        "fixture_id": eid,
                        "home": home,
                        "away": away,
                        "jogo": f"{home} x {away}",
                        "liga": liga_normalizada,
                        "pais": pais,
                        "horario": dt_local.strftime("%H:%M"),
                        "data": dt_local.strftime("%Y-%m-%d"),
                        "superbet_url": url_superbet(eid, home, away),
                    })
    except Exception as ex:
        log.error(f"Erro TheSportsDB [buscar_jogos_thesportsdb] [{type(ex).__name__}]: {repr(ex)}")

    return validas


async def buscar_jogos():
    base = datetime.now()
    data_alvo = (base + timedelta(days=1)).strftime("%Y-%m-%d") if MODO_TESTE else base.strftime("%Y-%m-%d")
    data_ate = (datetime.strptime(data_alvo, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    jogos_fd = await buscar_jogos_football_data(data_alvo, data_ate)

    if jogos_fd:
        validas = jogos_fd
        log.info(f"Fonte principal OK: football-data.org com {len(validas)} jogos")
    else:
        log.warning("football-data.org sem jogos. Tentando fallback TheSportsDB...")
        jogos_tsd = await buscar_jogos_thesportsdb(data_alvo)
        validas = jogos_tsd
        log.info(f"Fallback TheSportsDB: {len(validas)} jogos")

    priorizados = [j for j in validas if normalizar_liga(j["liga"]) in LIGAS_BOAS]
    outros = [j for j in validas if normalizar_liga(j["liga"]) not in LIGAS_BOAS]
    selecionados = (priorizados + outros)[:50]

    log.info(f"Data alvo: {data_alvo} | Encontrados: {len(validas)} | Selecionados: {len(selecionados)}")
    return selecionados


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
            except Exception as ex:
                log.warning(f"Falha noticias [{type(ex).__name__}]: {repr(ex)}")

    log.info(f"Noticias: {len(noticias)}")
    return noticias[:10]


def _usar_max_completion_tokens(model: str) -> bool:
    prefixos_novos = ("o1", "o3", "o4", "gpt-5")
    return any(model.startswith(p) for p in prefixos_novos)


def gerar_apostas_ia(jogos, noticias):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    n_pedir = max(N_APOSTAS * 3, 20)

    exemplo = (
        '{"fixture_id":12345,"home":"Arsenal","away":"Chelsea",'
        '"jogo":"Arsenal x Chelsea","liga":"Premier League","pais":"England",'
        '"horario":"16:00","data":"2026-05-03","mercado":"Mais de 1.5 gols",'
        '"sugestao":"Mais de 1.5 gols","odd":1.45,"confianca":75,"razao":"Motivo breve."}'
    )

    partes = [
        f"Voce e um especialista em apostas esportivas. Hoje e {agora} (Brasilia UTC-3).",
        f"Gere EXATAMENTE {n_pedir} sugestoes de apostas para os jogos abaixo.",
        "",
        "CRITERIOS:",
        "- Confianca minima: 60%",
        "- Prefira ligas conhecidas: Premier League, Brasileirao, La Liga, Bundesliga, Serie A, Ligue 1, Eredivisie, Primeira Liga.",
        "- MERCADOS PRIORITARIOS (maior assertividade, odd >= 1.35): menos de 3.5 gols, dupla chance, empate anula (DNB), ambas marcam",
        "- MERCADOS BONS (boa assertividade, odd >= 1.40): mais de 1.5 gols, mais de 2.5 gols",
        "- MERCADOS SECUNDARIOS (odd >= 1.50): escanteios, cartoes",
        "- MERCADOS DE MENOR PRIORIDADE (odd >= 1.60): mercados de 1º tempo",
        "- Se houver poucos jogos no dia, voce pode sugerir mais de um mercado por jogo, mas priorize 1 aposta por jogo.",
        f"- OBRIGATORIO: gere exatamente {n_pedir} apostas em JSON por linha, sem markdown.",
        "",
        "Retorne UM JSON por linha, sem markdown. Inclua fixture_id, home e away:",
        exemplo,
        "",
        "JOGOS:",
        json.dumps(jogos, ensure_ascii=False),
        "",
        "NOTICIAS:",
        json.dumps(noticias, ensure_ascii=False),
    ]
    prompt = "\n".join(partes)

    def parse_apostas(content):
        result = []
        for linha in (content or "").strip().splitlines():
            linha = linha.strip()
            if linha.startswith("{"):
                try:
                    item = json.loads(linha)
                    item["odd"] = float(item.get("odd", 0) or 0)
                    item["confianca"] = int(float(item.get("confianca", 0) or 0))
                    result.append(item)
                except Exception:
                    pass
        return result

    client = OpenAI(api_key=OPENAI_API_KEY)

    def _call(model):
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        }
        if _usar_max_completion_tokens(model):
            kwargs["max_completion_tokens"] = 4000
        else:
            kwargs["max_tokens"] = 4000
        return client.chat.completions.create(**kwargs)

    try:
        resp = _call(MODELO_IA)
        content = resp.choices[0].message.content if resp and resp.choices else ""
        apostas = parse_apostas(content)
        log.info(f"IA ({MODELO_IA}) gerou {len(apostas)} apostas")
        return apostas
    except Exception as ex:
        log.error(f"Erro {MODELO_IA}: {type(ex).__name__} | {repr(ex)}")
        try:
            log.info("Fallback: gpt-4o-mini...")
            resp = _call("gpt-4o-mini")
            content = resp.choices[0].message.content if resp and resp.choices else ""
            apostas = parse_apostas(content)
            log.info(f"Fallback gpt-4o-mini: {len(apostas)} apostas")
            return apostas
        except Exception as ex2:
            log.error(f"Fallback falhou: {type(ex2).__name__} | {repr(ex2)}")
            return []


def montar_acumulador(apostas):
    vistos, cands = set(), []
    for a in sorted(apostas, key=lambda x: float(x.get("confianca", 0) or 0), reverse=True):
        j = a.get("jogo", "")
        if j in vistos:
            continue
        vistos.add(j)
        cands.append(a)
        if len(cands) == 3:
            break

    if len(cands) < 3:
        return None

    try:
        odd = round(float(cands[0]["odd"]) * float(cands[1]["odd"]) * float(cands[2]["odd"]), 2)
    except Exception:
        return None

    return {"apostas": cands, "odd": odd} if odd >= 3.0 else None


def formatar_mensagem(apostas, acum):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    sep = "─" * 30
    linhas = [f"⚽ APOSTAS DO DIA — SUPERBET\n📅 {agora}\n{sep}\n\n"]

    for i, a in enumerate(apostas, 1):
        c = a.get("confianca", 0)
        tier = get_market_tier(a.get("mercado", ""))
        emoji = "🔥" if tier == 1 else "✅" if tier == 2 else "📌"
        dlabel = " (amanhã)" if a.get("data", "") != hoje_str else ""

        linhas.append(f"{emoji} {i}. {a.get('jogo', '')}\n")
        linhas.append(f" 🏆 {a.get('liga', '')} — {a.get('pais', '')} | ⏰ {a.get('horario', '')}{dlabel}\n")
        linhas.append(f" 📊 {a.get('mercado', '')} → {a.get('sugestao', '')}\n")
        linhas.append(f" 💰 Odd: {a.get('odd', '')}x | Confiança: {c}%\n")
        linhas.append(f" 💡 {str(a.get('razao', ''))[:180]}\n")
        linhas.append(f" 🔗 {a.get('superbet_url', SUPERBET_BASE)}\n\n")

    if acum:
        linhas.append(f"{sep}\n🎯 MINI ACUMULADOR — Odd total: {acum['odd']}x\n")
        for i, a in enumerate(acum["apostas"], 1):
            linhas.append(f" {i}. {a['jogo']} | {a['mercado']} ({a['odd']}x)\n")
        linhas.append("\n")

    linhas.append("\n⚠️ Aposte com responsabilidade. Apenas maiores de 18 anos.")
    return "".join(linhas)


async def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(msg), 4000):
        trecho = msg[i:i + 4000]
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": trecho})
            if r.status_code == 200:
                log.info("Telegram: mensagem enviada!")
            else:
                log.error(f"Telegram erro {r.status_code}: {r.text[:300]}")
        await asyncio.sleep(0.5)


async def pipeline_apostas():
    log.info("=== Iniciando pipeline ===")
    jogos, noticias = await asyncio.gather(buscar_jogos(), buscar_noticias())

    if not jogos:
        await enviar_telegram("⚽ Bot: nenhum jogo encontrado nas fontes disponíveis hoje.")
        return

    apostas_brutas = gerar_apostas_ia(jogos, noticias)
    if not apostas_brutas:
        await enviar_telegram("⚽ Bot: IA nao gerou apostas validas.")
        return

    apostas = selecionar_apostas_priorizadas(apostas_brutas, N_APOSTAS)

    if not apostas:
        await enviar_telegram("⚽ Bot: nenhuma aposta passou no filtro de odds minimas.")
        return

    await enviar_telegram(formatar_mensagem(apostas, montar_acumulador(apostas)))
    log.info(f"=== {len(apostas)} apostas enviadas ===")


async def main():
    log.info(
        f"Bot v10-fallback-TSD | Modelo: {MODELO_IA} | "
        f"Apostas: {HORA_APOSTAS.strftime('%H:%M')} BRT"
    )

    if MODO_TESTE:
        log.info("MODO TESTE ATIVADO - rodando agora com jogos de amanhã!")
        try:
            await pipeline_apostas()
        except Exception as ex:
            log.error(f"Erro: {type(ex).__name__} | {repr(ex)}")
            try:
                await enviar_telegram(f"Erro no bot: {type(ex).__name__} | {repr(ex)}")
            except Exception:
                pass
        return

    ultimo_apostas = None
    while True:
        agora = datetime.now()
        hoje = agora.date()

        if agora.time() >= HORA_APOSTAS and hoje != ultimo_apostas:
            ultimo_apostas = hoje
            try:
                await pipeline_apostas()
            except Exception as ex:
                log.error(f"Erro: {type(ex).__name__} | {repr(ex)}")
                try:
                    await enviar_telegram(f"Erro: {type(ex).__name__} | {repr(ex)}")
                except Exception:
                    pass

        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
