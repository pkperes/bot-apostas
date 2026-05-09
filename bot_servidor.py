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

MODO_TESTE = True  # ALTERADO PARA TRUE - Roda imediatamente ao iniciar
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
    "Ligue 2", "Segunda Division",
}

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# ========== PRIORIDADE DE MERCADOS (NOVO) ==========
# Baseado na análise de assertividade dos dias 04, 05, 06, 07 e 08/05:
# Tier 1 (~75-85%): under 3.5, DC, DNB, BTTS
# Tier 2 (~70-75%): over 1.5, over 2.5
# Tier 3 (~50-70%): escanteios, cartões
# Tier 4 (~50%): mercados de 1º tempo

MARKET_PRIORITY = {
    # Tier 1 - Melhor assertividade (75-85%)
    "menos de 3.5 gols": 1,
    "under 3.5": 1,
    "dupla chance": 1,
    "vence ou empata": 1,
    "ou empate": 1,
    "empate anula": 1,
    "dnb": 1,
    "ambas marcam": 1,
    "btts": 1,
    
    # Tier 2 - Boa assertividade (70-75%)
    "mais de 1.5 gols": 2,
    "over 1.5": 2,
    "mais de 2.5 gols": 2,
    "over 2.5": 2,
    
    # Tier 3 - Assertividade média (50-70%)
    "escanteios": 3,
    "corners": 3,
    "escanteio": 3,
    "cart": 3,
    "amarelo": 3,
    
    # Tier 4 - Menor assertividade (~50%)
    "1º tempo": 4,
    "1o tempo": 4,
    "primeiro tempo": 4,
    "ht": 4,
    "intervalo": 4,
}

MIN_ODDS_BY_TIER = {
    1: 1.35,  # Tier 1: under 3.5, DC, DNB, BTTS
    2: 1.40,  # Tier 2: over 1.5, over 2.5
    3: 1.50,  # Tier 3: escanteios, cartões
    4: 1.60,  # Tier 4: 1º tempo
}

def get_market_tier(mercado_texto: str) -> int:
    """Retorna o tier (prioridade) do mercado baseado no texto."""
    if not mercado_texto:
        return 99
    texto_lower = mercado_texto.lower().strip()
    for key, tier in MARKET_PRIORITY.items():
        if key in texto_lower:
            return tier
    return 99  # Mercados não classificados vão pro fim

def odd_ok_for_market(mercado_texto: str, odd: float) -> bool:
    """Verifica se a odd atende o mínimo para o tier do mercado."""
    tier = get_market_tier(mercado_texto)
    min_odd = MIN_ODDS_BY_TIER.get(tier, 1.40)
    return odd >= min_odd

def selecionar_apostas_priorizadas(candidatos: list, qtd_alvo: int = 10) -> list:
    """Seleciona apostas priorizando mercados com melhor assertividade.
    
    Só usa mercados de menor assertividade (Tier 3 e 4) quando não houver
    apostas suficientes nos mercados prioritários (Tier 1 e 2).
    
    Args:
        candidatos: Lista de apostas candidatas
        qtd_alvo: Quantidade desejada de apostas
    
    Returns:
        Lista de apostas selecionadas e filtradas
    """
    aprovadas = []
    jogos_usados = set()
    
    # Filtrar apostas com odd adequada ao mercado
    validas = [
        c for c in candidatos
        if odd_ok_for_market(c.get("mercado", ""), float(c.get("odd", 0)))
    ]
    
    log.info(f"Candidatos: {len(candidatos)} | Válidos por odd mínima: {len(validas)}")
    
    # Ordenar por tier, confiança e odd
    validas_ordenadas = sorted(
        validas,
        key=lambda x: (
            get_market_tier(x.get("mercado", "")),
            -float(x.get("confianca", 0)),
            -float(x.get("odd", 0))
        )
    )
    
    # Preencher por tier até atingir quantidade alvo
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
    
    # Se não tiver apostas suficientes, permite múltiplos mercados por jogo
    if len(aprovadas) < qtd_alvo:
        log.warning(f"Apenas {len(aprovadas)} apostas únicas. Permitindo múltiplos mercados...")
        mercados_usados = set()
        
        for item in validas_ordenadas:
            if len(aprovadas) >= qtd_alvo:
                break
            
            chave = f"{item.get('jogo', '')}_{item.get('mercado', '')}"
            if chave not in mercados_usados:
                if item not in aprovadas:
                    aprovadas.append(item)
                    mercados_usados.add(chave)
    
    log.info(
        f"Seleção: {len(aprovadas)} apostas | "
        f"Tier 1: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado',''))==1)} | "
        f"Tier 2: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado',''))==2)} | "
        f"Tier 3: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado',''))==3)} | "
        f"Tier 4+: {sum(1 for a in aprovadas if get_market_tier(a.get('mercado',''))>=4)}"
    )
    
    return aprovadas
# ========== FIM PRIORIDADE DE MERCADOS ==========

def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")

def url_superbet(fixture_id, home, away):
    return f"https://superbet.bet.br/odds/futebol/{slugify(home)}-x-{slugify(away)}-{fixture_id}"

async def buscar_jogos():
    base = datetime.now()
    data_alvo = (base + timedelta(days=1)).strftime("%Y-%m-%d") if MODO_TESTE else base.strftime("%Y-%m-%d")
    data_ate = (datetime.strptime(data_alvo, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    validas = []
    vistos = set()
    headers_fd = {
        "X-Auth-Token": FOOTBALL_DATA_KEY,
        "User-Agent": "Mozilla/5.0",
    }
    COMPETICOES_FREE = [
        "CL", "PL", "BL1", "SA", "PD", "FL1", "EL", "DED", "BSA", "PPL", "EC", "WC",
    ]

    async def get_matches(url, client):
        try:
            r = await client.get(url, headers=headers_fd)
            log.info(f"FD {url[:90]}... | status={r.status_code}")
            if r.status_code == 429:
                log.warning("Rate limit atingido na football-data.org")
                return []
            if r.status_code != 200:
                log.error(f"FD erro {r.status_code}: {r.text[:200]}")
                return []
            return r.json().get("matches", [])
        except Exception as ex:
            log.error(f"Falha football-data.org: {ex}")
            return []

    def processar(m, liga_override=""):
        ds = m.get("utcDate", "")
        if not ds:
            return None
        try:
            from datetime import timezone
            dt_jogo = datetime.fromisoformat(ds.replace("Z", "+00:00")).astimezone()
        except Exception:
            return None
        status = m.get("status", "")
        if status not in ("SCHEDULED", "TIMED", "POSTPONED"):
            return None
        home = m.get("homeTeam", {}).get("name", "?") or m.get("homeTeam", {}).get("shortName", "?")
        away = m.get("awayTeam", {}).get("name", "?") or m.get("awayTeam", {}).get("shortName", "?")
        fid = str(m.get("id", "0"))
        chave = (fid, home, away)
        if chave in vistos:
            return None
        vistos.add(chave)
        liga = liga_override or m.get("competition", {}).get("name", "")
        return {
            "fixture_id": fid,
            "home": home,
            "away": away,
            "jogo": f"{home} x {away}",
            "liga": liga,
            "pais": m.get("area", {}).get("name", ""),
            "horario": dt_jogo.strftime("%H:%M"),
            "data": dt_jogo.strftime("%Y-%m-%d"),
            "superbet_url": url_superbet(fid, home, away),
        }

    try:
        async with httpx.AsyncClient(timeout=30) as c:
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
                for comp in COMPETICOES_FREE:
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
        log.error(f"Erro football-data.org [buscar_jogos]: {ex}")

    priorizados = [j for j in validas if j["liga"] in LIGAS_BOAS]
    outros = [j for j in validas if j["liga"] not in LIGAS_BOAS]
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
            except Exception:
                pass
    log.info(f"Noticias: {len(noticias)}")
    return noticias[:10]

def _usar_max_completion_tokens(model: str) -> bool:
    prefixos_novos = ("o1", "o3", "o4", "gpt-5")
    return any(model.startswith(p) for p in prefixos_novos)

def gerar_apostas_ia(jogos, noticias):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    n_pedir = N_APOSTAS * 3  # Pedir mais para ter pool maior
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
        "- Prefira ligas conhecidas: Premier League, Brasileirao, La Liga, etc.",
        "- MERCADOS PRIORITARIOS (maior assertividade, odd >= 1.35): menos de 3.5 gols, dupla chance, empate anula (DNB), ambas marcam",
        "- MERCADOS BONS (boa assertividade, odd >= 1.40): mais de 1.5 gols, mais de 2.5 gols",
        "- MERCADOS SECUNDARIOS (odd >= 1.50): escanteios, cartoes",
        "- MERCADOS DE MENOR PRIORIDADE (odd >= 1.60): mercados de 1º tempo",
        "- Outros mercados aceitos: resultado final, handicaps, chutes, jogador marca, intervalos",
        f"- OBRIGATORIO: gere exatamente {n_pedir} apostas de jogos diferentes, priorizando mercados de maior assertividade",
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
        for linha in content.strip().splitlines():
            linha = linha.strip()
            if linha.startswith("{"):
                try:
                    result.append(json.loads(linha))
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
        apostas = parse_apostas(resp.choices[0].message.content)
        log.info(f"IA ({MODELO_IA}) gerou {len(apostas)} apostas")
        return apostas
    except Exception as ex:
        log.error(f"Erro {MODELO_IA}: {ex}")
        try:
            log.info("Fallback: gpt-4o-mini...")
            resp = _call("gpt-4o-mini")
            apostas = parse_apostas(resp.choices[0].message.content)
            log.info(f"Fallback gpt-4o-mini: {len(apostas)} apostas")
            return apostas
        except Exception as ex2:
            log.error(f"Fallback falhou: {ex2}")
            return []

def montar_acumulador(apostas):
    vistos, cands = set(), []
    for a in sorted(apostas, key=lambda x: x.get("confianca", 0), reverse=True):
        j = a.get("jogo", "")
        if j in vistos:
            continue
        vistos.add(j)
        cands.append(a)
        if len(cands) == 3:
            break
    if len(cands) < 3:
        return None
    odd = round(cands[0]["odd"] * cands[1]["odd"] * cands[2]["odd"], 2)
    return {"apostas": cands, "odd": odd} if odd >= 3.0 else None

def formatar_mensagem(apostas, acum):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    sep = "\u2500" * 30
    linhas = [f"\u26bd APOSTAS DO DIA \u2014 SUPERBET\n\U0001f4c5 {agora}\n{sep}\n\n"]
    for i, a in enumerate(apostas, 1):
        c = a.get("confianca", 0)
        tier = get_market_tier(a.get("mercado", ""))
        # Emoji baseado no tier: 🔥 para Tier 1, ✅ para Tier 2, 📌 para outros
        emoji = "\U0001f525" if tier == 1 else "\u2705" if tier == 2 else "\U0001f4cc"
        dlabel = " (amanhã)" if a.get("data", "") != hoje_str else ""
        linhas.append(f"{emoji} {i}. {a.get('jogo', '')}\n")
        linhas.append(f"   \U0001f3c6 {a.get('liga', '')} \u2014 {a.get('pais', '')} | \u23f0 {a.get('horario', '')}{dlabel}\n")
        linhas.append(f"   \U0001f4ca {a.get('mercado', '')} \u2192 {a.get('sugestao', '')}\n")
        linhas.append(f"   \U0001f4b0 Odd: {a.get('odd', '')}x | Confiança: {c}%\n")
        linhas.append(f"   \U0001f4a1 {str(a.get('razao', ''))[:180]}\n")
        linhas.append(f"   \U0001f517 {a.get('superbet_url', SUPERBET_BASE)}\n\n")
    if acum:
        linhas.append(f"{sep}\n\U0001f3af MINI ACUMULADOR \u2014 Odd total: {acum['odd']}x\n")
        for i, a in enumerate(acum["apostas"], 1):
            linhas.append(f"   {i}. {a['jogo']} | {a['mercado']} ({a['odd']}x)\n")
        linhas.append("\n")
    linhas.append("\n\u26a0\ufe0f Aposte com responsabilidade. Apenas maiores de 18 anos.")
    return "".join(linhas)

async def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i in range(0, len(msg), 4000):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg[i:i+4000]})
            if r.status_code == 200:
                log.info("Telegram: mensagem enviada!")
            else:
                log.error(f"Telegram erro {r.status_code}: {r.text[:200]}")
        await asyncio.sleep(0.5)

async def pipeline_apostas():
    log.info("=== Iniciando pipeline ===")
    jogos, noticias = await asyncio.gather(buscar_jogos(), buscar_noticias())
    if not jogos:
        await enviar_telegram("\u26bd Bot: nenhum jogo encontrado.")
        return
    
    apostas_brutas = gerar_apostas_ia(jogos, noticias)
    if not apostas_brutas:
        await enviar_telegram("\u26bd Bot: IA nao gerou apostas validas.")
        return
    
    # APLICAR FILTRO DE PRIORIDADE DE MERCADOS (NOVO)
    apostas = selecionar_apostas_priorizadas(apostas_brutas, N_APOSTAS)
    
    if not apostas:
        await enviar_telegram("\u26bd Bot: nenhuma aposta passou no filtro de odds minimas.")
        return
    
    await enviar_telegram(formatar_mensagem(apostas, montar_acumulador(apostas)))
    log.info(f"=== {len(apostas)} apostas enviadas ===")

async def main():
    log.info(
        f"Bot v9-priorizacao-TESTE | Modelo: {MODELO_IA} | "
        f"Apostas: {HORA_APOSTAS.strftime('%H:%M')} BRT"
    )
    if MODO_TESTE:
        log.info("MODO TESTE ATIVADO - rodando agora com jogos de amanha (09/05)!")
        try:
            await pipeline_apostas()
        except Exception as ex:
            log.error(f"Erro: {ex}")
            try:
                await enviar_telegram(f"Erro no bot: {ex}")
            except Exception:
                pass
        return  # ADICIONADO - Encerra após rodar o teste

    ultimo_apostas = datetime.now().date() if MODO_TESTE else None
    while True:
        agora = datetime.now()
        hoje = agora.date()
        if agora.time() >= HORA_APOSTAS and hoje != ultimo_apostas:
            ultimo_apostas = hoje
            try:
                await pipeline_apostas()
            except Exception as ex:
                log.error(f"Erro: {ex}")
                try:
                    await enviar_telegram(f"Erro: {ex}")
                except Exception:
                    pass
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
