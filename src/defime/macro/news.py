"""Fase 6 — choque de noticia. Entra como GATE, nunca como score.

O PLAN.md e explicito: noticia **nunca** vira parcela de um score somado. Ela
faz duas coisas e so duas:

  1. choque alto => WAIT (gate)
  2. choque alto => infla o sigma da simulacao, alargando os ranges por efeito
     ECONOMICO, com unidade preservada

A diferenca importa. Um score somado deixa "noticia ruim" ser comprada por
"fee alta"; um gate nao deixa. E inflar sigma nao e opiniao: e dizer "o mundo
esta mais incerto", que e exatamente o que o simulador sabe usar.

**O GDELT FUNCIONA, e a primeira leitura disso estava errada.** Um `curl` sem
espera deu timeout e eu registrei "nao responde". Repetindo com intervalo, a
resposta veio: HTTP 200. O que existe e RATE LIMIT — o proprio corpo do 429 diz
"limit requests to one every 5 seconds". Na pratica o limite e ERRATICO.

Em 2026-08-21, 87 coletas seguidas falharam com "HTTP 429 apos 4 tentativas" —
a espera era FIXA em 6s, ou seja 24 segundos no total antes de desistir. Agora e
backoff exponencial com jitter: 6+12+24+48+96 = **186s** de janela coberta pelo
mesmo punhado de tentativas. Falha continua sendo estado normal, nao erro.

Fonte externa nao alcancavel e fonte throttled sao coisas diferentes, e tratar
uma como a outra teria jogado fora a unica fonte macro ao vivo do projeto.

Sem nenhuma fonte, `choque = 0` e o sistema opera igual — criterio 3 do
check-6. Mas hoje ha uma fonte.

Classificacao deliberadamente grosseira: contagem por termo, sem modelo. Um
classificador de sentimento aqui seria precisao falsa sobre uma fonte que nem
esta ligada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .base import Encadeado, Leitura, ManualCSV, Provider

# Termos que historicamente acompanham movimento grande no petroleo.
# Peso e ordem de grandeza, nao calibracao — nao ha amostra para calibrar.
#
# **Invariante: nenhum termo pode ser substring de outro.** `choque` e uma SOMA
# sobre os termos que casam, entao termo contido em termo conta duas vezes.
# Havia um caso, e ele era grave: `opec` dentro de `opec+` fazia qualquer
# manchete com "OPEC+" somar 3,0 + 3,0 = **6,0 sozinha** — exatamente o
# `CHOQUE_ALTO`. Uma unica noticia rotineira disparava o gate. `opec+` saiu
# porque `opec` ja o cobre, com o mesmo peso. Ha teste do invariante.
#
# `strait of hormuz` virou `hormuz` em 2026-08-22, por leitura ao vivo: a
# manchete "Brent crude surpasses $94 amid the Hormuz crisis" pontuava **0**,
# porque o texto corrente diz so `Hormuz`. Trocar em vez de acrescentar e o
# que mantem o invariante acima — as duas formas juntas dariam 6,0.
TERMOS = {
    "opec": 3.0, "production cut": 3.0, "output cut": 3.0,
    "sanction": 2.5, "embargo": 2.5, "hormuz": 3.0,
    "attack": 2.0, "strike": 1.5, "pipeline": 1.5, "refinery": 1.5,
    "hurricane": 2.0, "inventory": 1.0, "stockpile": 1.0,
    "recession": 1.5, "demand": 0.5,
}

CHOQUE_ALTO = 6.0        # soma de pesos que dispara o gate

# Termos de SEMICONDUTORES, para as pools de acao tokenizada (MU, NVDA, TSM).
#
# **So EVENTO pontua. ASSUNTO nao.** A primeira versao misturou os dois e o
# choque saiu 35,0 contra um limiar de 6,0 — `hbm` casou 7 vezes, `dram`,
# `nand` e `semiconductor` uma cada. Isso nao media noticia relevante: media
# que a CONSULTA funcionou. Palavra de assunto aparece em toda materia do
# setor, tenha acontecido algo ou nao.
#
# A lista do petroleo nao tinha esse problema por acidente de vocabulario:
# "ataque", "sancao" e "corte de producao" ja sao eventos. Aqui era preciso
# escolher.
#
# **O invariante de substring vale igual**, e ele e o que impede a familia de
# bug que `opec` dentro de `opec+` criou: `choque` e uma SOMA, e um par
# aninhado conta duas vezes na mesma manchete. Por isso `memory` esta fora
# (casaria dentro de "memory chip") e `chip` esta fora (casa em "chipmaker",
# "chipset", e ate em nome proprio).
TERMOS_SEMICONDUTORES = {
    # oferta: o que muda o preco da memoria
    "export control": 3.0, "export restriction": 3.0, "tariff": 2.5,
    "production cut": 3.0, "capacity expansion": 2.0,
    "shortage": 2.5, "glut": 2.5, "oversupply": 2.5,
    # empresa: o que move a acao num dia
    "guidance": 2.0, "downgrade": 2.0, "upgrade": 1.5, "beats": 1.5,
    "misses": 2.0, "recall": 2.0,
    # macro do setor
    "recession": 1.5, "capex cut": 2.5,
    # **o que PARA uma fab.** Lacuna achada em 2026-09-01 com o feed ao vivo:
    # o Google News trouxe "Micron's Taiwan unions threaten strike over bonus
    # dispute" e o choque saiu 0,0. O vocabulario cobria oferta (export
    # control, glut) e resultado (guidance, misses) e nao cobria interrupcao
    # de producao — que e a categoria que mais move preco de memoria, porque
    # tira capacidade sem aviso.
    #
    # `strike over` e nao `strike`: "strike price" aparece em toda materia de
    # opcoes e viraria falso positivo diario. O invariante de substring cobre
    # o resto — nenhum destes esta contido em outro termo do dicionario.
    "strike over": 2.5, "walkout": 2.5, "earthquake": 3.0,
    "power outage": 2.5, "contamination": 2.5,
}

# Palavras que confirmam que a materia e DO SETOR, sem pontuar. Servem de
# filtro: manchete sem nenhuma delas provavelmente veio junto por acaso na
# busca e nao deveria somar choque nenhum.
ASSUNTO_SEMICONDUTORES = ("dram", "nand", "hbm", "semiconductor", "foundry",
                          "micron", "chipmaker", "memory")

# Perfil de jogos/plataforma social, para a RBLX. **O vocabulario nao e o de
# semicondutores traduzido:** o que move uma plataforma de jogos nao e oferta
# de capacidade, e engajamento, regulacao de menores e monetizacao.
#
# Tres categorias, e a primeira e a que mais move o preco num dia:
TERMOS_JOGOS = {
    # metricas da plataforma — o que a empresa reporta e o mercado repreca
    "bookings": 2.5, "daily active users": 2.5, "engagement hours": 2.0,
    "guidance": 2.0, "downgrade": 2.0, "beats": 1.5, "misses": 2.0,
    # **`analyst upgrade` e nao `upgrade`.** Medido no mesmo instante: casou
    # com "My years-old gaming PC became the best AI machine in the house,
    # and it only needed one upgrade". Em noticia de jogos, `upgrade` e
    # hardware muito mais vezes que revisao de recomendacao. `downgrade` NAO
    # tem o problema — ninguem faz downgrade de PC por escolha — e fica como
    # esta, porque encurtar os dois por simetria trocaria um falso positivo
    # por um falso negativo.
    "analyst upgrade": 1.5, "price target": 1.5,
    # **regulacao e seguranca de menores.** E a categoria de risco propria
    # desta acao e nao existe no perfil de semicondutores: uma acao judicial
    # sobre protecao de criancas move a RBLX de um jeito que nenhuma noticia
    # de capacidade move. `lawsuit` sozinho pegaria disputa de patente, que e
    # rotina; os termos aqui sao especificos.
    # **`child predator` e nao `predator`.** Medido na primeira leitura ao
    # vivo, 03/09: `predator` casou duas vezes e as duas eram o **monitor
    # Acer Predator** ("Acer's Latest Predator Gaming Monitor", "Acer
    # Predator Atlas 7 Gaming Handheld"). Marca de hardware gamer, dentro do
    # filtro de assunto "gaming" — falso positivo diario, e ele sozinho
    # levava o choque a 6,0. Mesma familia do `strike price` e do `opec+`.
    "child safety": 3.0, "child predator": 3.0, "attorney general": 3.0,
    "age verification": 2.5, "coppa": 3.0, "class action": 2.5,
    "ftc probe": 3.0,
    # plataforma e distribuicao — quem controla o canal
    "app store": 1.5, "ban": 2.5, "outage": 2.5, "data breach": 3.0,
    # o dinheiro
    "short report": 3.0, "stock offering": 2.5, "lockup expiration": 2.0,
    "buyback": 1.5,
}

# Assunto FILTRA e nao pontua — a licao de 2026-09-01, quando `hbm` e `dram`
# pontuavam e o choque saiu 35,0 contra limiar 6,0 porque palavra de assunto
# aparece em toda materia do setor, tenha acontecido algo ou nao.
ASSUNTO_JOGOS = ("roblox", "gaming", "video game", "metaverse", "unity",
                 "epic games", "game developer")

# `perfil -> (termos, consulta, assunto, limiar)`. A consulta vai para o Google
# News RSS, o unico degrau da escada que aceita busca arbitraria. O limiar e
# por perfil porque a base de manchetes difere: uma busca estreita de petroleo
# devolve menos materia irrelevante que uma de "semiconductor".
PERFIS = {
    "petroleo": (None, "crude oil OPEC", (), CHOQUE_ALTO),
    "semicondutores": (TERMOS_SEMICONDUTORES,
                       "Micron OR DRAM OR HBM OR semiconductor memory",
                       ASSUNTO_SEMICONDUTORES, 6.0),
    # **A consulta e so de termos de UMA palavra, e isso e medido.** Em
    # 2026-09-03 o Google News RSS devolveu HTTP 200, RSS valido e ZERO itens
    # para `Micron OR DRAM OR HBM OR semiconductor memory`, enquanto
    # `Micron OR DRAM OR HBM` devolvia 100. Operando arbitrario com uma
    # expressao de duas palavras no fim envenena a consulta inteira, e o
    # sintoma e um feed vazio que se le como "nao ha noticia".
    "jogos": (TERMOS_JOGOS, "Roblox OR gaming OR metaverse",
              ASSUNTO_JOGOS, 6.0),
}
JANELA_H = 12.0


@dataclass
class Choque:
    valor: float
    artigos: int
    termos: dict[str, int]
    fonte: str
    disponivel: bool

    @property
    def alto(self) -> bool:
        return self.disponivel and self.valor >= CHOQUE_ALTO

    def resumo(self) -> dict:
        return {"choque": round(self.valor, 2), "alto": self.alto,
                "artigos": self.artigos, "fonte": self.fonte,
                "disponivel": self.disponivel,
                "termos": dict(sorted(self.termos.items(),
                                      key=lambda x: -x[1])[:5]),
                "limiar": CHOQUE_ALTO}


class Gdelt(Provider):
    """GDELT DOC 2.0. Funciona, com rate limit de 1 requisicao a cada 5s.

    O 429 nao e bloqueio: o corpo da resposta diz o intervalo exigido. Respeitar
    o intervalo e tentar de novo resolve — foi o que separou "fonte inexistente"
    de "unica fonte macro ao vivo do projeto".
    """

    nome = "gdelt"
    URL = ("https://api.gdeltproject.org/api/v2/doc/doc"
           "?query={q}&mode=artlist&maxrecords=75&format=json&timespan={h}h")
    # O limite anunciado e 1/5s, mas na pratica e erratico: medido, ~2 de 3
    # tentativas passam mesmo com 15s de espera. Entao intervalo folgado e
    # varias tentativas — e ainda assim `disponivel=False` e um desfecho
    # normal, nao um bug a perseguir.
    # Backoff EXPONENCIAL, nao intervalo fixo. A versao anterior esperava 6s
    # entre 4 tentativas — 24 segundos no total, e desistia. Medido em
    # 2026-08-21: 87 coletas seguidas com `artigos=0` e "HTTP 429 apos 4
    # tentativas". Contra rate limit erratico, dobrar a espera cobre muito mais
    # janela pelo mesmo numero de tentativas: 6+12+24+48+96 = 186s contra 24s.
    #
    # O jitter existe para nao entrar em lockstep com outros clientes batendo
    # no mesmo limite por IP — sem ele, varios processos sincronizam e todos
    # levam 429 juntos.
    INTERVALO_S = 6.0
    FATOR_BACKOFF = 2.0
    # MEDIDO em 2026-08-22, e o numero CAIU de 5 para 2 por causa da medicao.
    # A rajada de 5 tentativas nao vencia o limite: gastava o orcamento dele.
    # Sequencia observada, nesta ordem:
    #   1. um pedido isolado, depois de horas de silencio -> HTTP 200, 5 artigos
    #   2. seis variacoes de consulta, 8s entre elas      -> 429 em TODAS
    #   3. UM pedido depois de 120s parado                -> 429
    # A consulta nao muda nada (simples, com aspas, com OR, com parenteses,
    # 5 ou 75 registros: todas 429). O limite e por IP e tem MEMORIA: rajada
    # recente fecha a porta para o pedido seguinte, mesmo espacado. Logo o
    # backoff exponencial, que parecia defesa, era ataque contra nos mesmos.
    #
    # Politica nova: no maximo 2 tentativas por chamada, e a repeticao de
    # verdade fica por conta da CADENCIA do coletor (`run_macro --loop-h 1`),
    # que da uma hora de silencio entre tentativas em vez de 6 segundos.
    TENTATIVAS = 2
    JITTER = 0.25              # +-25% sobre o intervalo

    # DISJUNTOR. Sem ele o backoff vira um tiro no pe: medido em 2026-08-21,
    # `contexto.montar()` passou de ~24s para **196s** — e ela roda a cada
    # coleta do `run_macro`, em cada relatorio e em cada rerun do dashboard.
    # Gastar 3 minutos para reconfirmar um 429 que acabou de acontecer nao
    # traz informacao nenhuma.
    #
    # Depois de uma falha, o provider devolve indisponivel NA HORA por
    # `PAUSA_APOS_FALHA_S`. Quem quiser tentar de novo antes disso chama
    # `Gdelt.rearmar()`.
    PAUSA_APOS_FALHA_S = 1800.0        # 30 min
    _falhou_em = 0.0
    _motivo_da_falha = ""

    _ultima_chamada = 0.0      # de classe: o limite e por IP, nao por instancia

    def __init__(self, consulta: str = "crude oil OR petroleum OR OPEC"):
        self.consulta = consulta

    def _esperar(self, tentativa: int = 0) -> None:
        """Espera com backoff exponencial e jitter, a partir da ultima chamada.

        `tentativa` 0 usa `INTERVALO_S`; cada repeticao multiplica por
        `FATOR_BACKOFF`.
        """
        import random
        import time
        alvo = Gdelt.INTERVALO_S * (Gdelt.FATOR_BACKOFF ** tentativa)
        alvo *= 1.0 + random.uniform(-Gdelt.JITTER, Gdelt.JITTER)
        falta = alvo - (time.time() - Gdelt._ultima_chamada)
        if falta > 0:
            time.sleep(falta)
        Gdelt._ultima_chamada = time.time()

    @classmethod
    def rearmar(cls) -> None:
        """Zera o disjuntor. Para teste e para forcar nova tentativa."""
        cls._falhou_em = 0.0
        cls._motivo_da_falha = ""

    @classmethod
    def em_pausa(cls) -> float:
        """Segundos que faltam da pausa, ou 0 se pode tentar."""
        import time
        if not cls._falhou_em:
            return 0.0
        return max(0.0, cls.PAUSA_APOS_FALHA_S - (time.time() - cls._falhou_em))

    def ler(self, horas: float = JANELA_H, **kw) -> Leitura:
        import json
        import urllib.error
        import urllib.parse
        import urllib.request

        falta = Gdelt.em_pausa()
        if falta > 0:
            return self._falha(
                f"{Gdelt._motivo_da_falha}; disjuntor aberto, nova tentativa "
                f"em {falta / 60:.0f} min (evita gastar "
                f"{sum(self.INTERVALO_S * self.FATOR_BACKOFF ** i for i in range(self.TENTATIVAS)):.0f}s "
                "para reconfirmar o mesmo 429)")

        url = self.URL.format(q=urllib.parse.quote(self.consulta), h=int(horas))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        ultimo = "sem tentativa"
        for tentativa in range(self.TENTATIVAS):
            self._esperar(tentativa)
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    bruto = resp.read().decode("utf-8", "replace")
                dados = json.loads(bruto)
                artigos = [a.get("title", "") for a in dados.get("articles", [])]
                Gdelt.rearmar()          # sucesso fecha o disjuntor
                return Leitura(self.nome, True, {"titulos": artigos},
                               ao_vivo=True)
            except urllib.error.HTTPError as exc:
                ultimo = f"HTTP {exc.code}"
                if exc.code != 429:
                    break
            except json.JSONDecodeError:
                # o 429 do GDELT vem como texto, nao json
                ultimo = "resposta nao-json (provavel rate limit)"
            except Exception as exc:  # noqa: BLE001
                ultimo = f"{type(exc).__name__}: {str(exc)[:100]}"
                break
        import time
        espera_total = sum(self.INTERVALO_S * self.FATOR_BACKOFF ** i
                           for i in range(self.TENTATIVAS))
        Gdelt._falhou_em = time.time()
        Gdelt._motivo_da_falha = f"{ultimo} apos {self.TENTATIVAS} tentativas"
        return self._falha(f"{ultimo} apos {self.TENTATIVAS} tentativas "
                           f"({espera_total:.0f}s de espera acumulada); "
                           f"disjuntor aberto por "
                           f"{self.PAUSA_APOS_FALHA_S / 60:.0f} min")


# Feeds RSS publicos, sem chave e sem cadastro. `{q}` e substituido pela
# consulta; feed sem `{q}` e uma pauta fixa de petroleo e entra inteiro.
#
# Medidos em 2026-08-22, todos HTTP 200 sem cabecalho nenhum alem do
# User-Agent: Google News 100 itens, OilPrice 15, Investing 10, EIA 18.
FEEDS_PADRAO = (
    ("google_news",
     "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"),
    ("oilprice", "https://oilprice.com/rss/main"),
    ("investing_commodities", "https://www.investing.com/rss/news_11.rss"),
)


class RssNoticias(Provider):
    """Feeds RSS publicos. Sem chave, sem cadastro, sem cota.

    E o degrau que responde a pergunta original — "noticia de mercado de graca"
    — sem pedir nada em troca. O Google News RSS aceita consulta arbitraria,
    que e exatamente o que se usava do GDELT, e devolveu **100 itens** contra
    os 75 que o GDELT prometia e os 0 que ele vinha entregando.

    Tres decisoes que o codigo carrega:

    1. **Varios feeds, e falha parcial nao derruba a leitura.** Um feed fora do
       ar reduz a amostra; nao apaga a fonte. `feeds_ok` e `feeds_falhos` saem
       em `dados` para o relatorio poder dizer o que faltou.
    2. **Frescor se mede no item MAIS NOVO, e a janela filtra item a item.**
       Um feed vivo pode trazer materia de dez dias atras — medido: o Google
       News devolveu item de 13/08 num feed de 22/08. Julgar o feed pelo item
       mais velho o mataria sem motivo; deixar o item velho entrar na janela de
       12h contaria noticia vencida como choque de agora.
    3. **Deduplicacao por titulo normalizado.** Os feeds se sobrepoem, e o
       `choque` e uma SOMA de pesos: a mesma materia em tres feeds triplicaria
       o peso dela e poderia sozinha estourar o limiar de 6,0.

    Ha guarda de intervalo com cache pelo mesmo motivo do Alpha Vantage:
    `contexto.montar()` roda em cada rerun do dashboard, e RSS de graca nao e
    RSS sem custo para quem serve.
    """

    nome = "rss_noticias"
    INTERVALO_MINIMO_S = 300.0         # 5 min entre baterias de feeds
    IDADE_MAXIMA_H = 48.0              # feed parado nao e feed
    TIMEOUT_S = 15

    _ultima_chamada = 0.0
    _ultima_leitura = None

    def __init__(self, consulta: str = "crude oil OPEC",
                 feeds=FEEDS_PADRAO):
        self.consulta = consulta
        self.feeds = tuple(feeds)

    @classmethod
    def rearmar(cls) -> None:
        cls._ultima_chamada = 0.0
        cls._ultima_leitura = None

    # ------------------------------------------------------------ parsing

    @staticmethod
    def _sem_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @classmethod
    def itens(cls, xml: str) -> list[tuple[str, object]]:
        """(titulo, datetime|None) de cada item. RSS 2.0 e Atom.

        Nao levanta com XML quebrado: devolve o que conseguiu ler. Feed alheio
        quebrado e caso normal, nao excecao.
        """
        import html
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        try:
            raiz = ET.fromstring(xml)
        except ET.ParseError:
            return []

        saida = []
        for no in raiz.iter():
            if cls._sem_ns(no.tag) not in ("item", "entry"):
                continue
            titulo, quando = "", None
            for filho in no:
                marca = cls._sem_ns(filho.tag)
                if marca == "title" and filho.text:
                    titulo = html.unescape(filho.text).strip()
                elif marca in ("pubDate", "published", "updated") and filho.text:
                    bruto = filho.text.strip()
                    try:
                        quando = parsedate_to_datetime(bruto)
                    except (TypeError, ValueError):
                        try:
                            quando = datetime.fromisoformat(
                                bruto.replace("Z", "+00:00"))
                        except ValueError:
                            quando = None
                    if quando is not None and quando.tzinfo is None:
                        quando = quando.replace(tzinfo=timezone.utc)
            if titulo:
                saida.append((titulo, quando))
        return saida

    def montar(self, por_feed: dict, *, agora=None,
               horas: float = JANELA_H) -> Leitura:
        """`por_feed`: {nome: xml}. Separada de `ler` para teste sem rede."""
        agora = agora or datetime.now(timezone.utc)
        corte = agora - timedelta(hours=horas)

        titulos, vistos = [], set()
        ok, falhos = [], {}
        mais_novo = None
        for nome, xml in por_feed.items():
            itens = self.itens(xml)
            if not itens:
                falhos[nome] = "sem itens legiveis"
                continue
            datados = [q for _, q in itens if q is not None]
            if not datados:
                falhos[nome] = "nenhum item com data"
                continue
            topo = max(datados)
            if mais_novo is None or topo > mais_novo:
                mais_novo = topo
            idade_h = (agora - topo).total_seconds() / 3600.0
            if idade_h > self.IDADE_MAXIMA_H:
                falhos[nome] = f"parado ha {idade_h / 24:.1f} dias"
                continue
            ok.append(nome)
            for titulo, quando in itens:
                if quando is None or quando < corte:
                    continue
                chave = " ".join(titulo.lower().split())
                if chave in vistos:
                    continue
                vistos.add(chave)
                titulos.append(titulo)

        if not ok:
            motivo = "; ".join(f"{k}: {v}" for k, v in falhos.items())
            return self._falha(motivo or "nenhum feed consultado")

        return Leitura(self.nome, True, {
            "titulos": titulos,
            "feeds_ok": ok,
            "feeds_falhos": falhos,
            "idade_h": (round((agora - mais_novo).total_seconds() / 3600.0, 2)
                        if mais_novo else None),
        }, ao_vivo=True)

    def ler(self, horas: float = JANELA_H, **kw) -> Leitura:
        import time
        import urllib.parse
        import urllib.request

        falta = self.INTERVALO_MINIMO_S - (time.time()
                                           - RssNoticias._ultima_chamada)
        if falta > 0 and RssNoticias._ultima_leitura is not None:
            cache = RssNoticias._ultima_leitura
            return Leitura(self.nome, cache.disponivel, dict(cache.dados),
                           motivo=cache.motivo, ao_vivo=False)

        RssNoticias._ultima_chamada = time.time()
        q = urllib.parse.quote_plus(self.consulta)
        por_feed, erros = {}, {}
        for nome, molde in self.feeds:
            url = molde.format(q=q) if "{q}" in molde else molde
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0;"
                                                " Win64; x64)"})
                with urllib.request.urlopen(req, timeout=self.TIMEOUT_S) as r:
                    por_feed[nome] = r.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                erros[nome] = f"{type(exc).__name__}: {str(exc)[:80]}"

        if not por_feed:
            leitura = self._falha("; ".join(f"{k}: {v}"
                                            for k, v in erros.items())
                                  or "nenhum feed respondeu")
        else:
            leitura = self.montar(por_feed, horas=horas)
            if erros and leitura.disponivel:
                leitura.dados.setdefault("feeds_falhos", {}).update(erros)
        RssNoticias._ultima_leitura = leitura
        return leitura


class AlphaVantageNews(Provider):
    """Alpha Vantage NEWS_SENTIMENT. Chave gratuita, 25 requisicoes por DIA.

    Existe porque o GDELT, medido em 2026-08-22, e uma fonte que responde
    quando quer: um pedido isolado passa, qualquer rajada recente fecha a porta
    por tempo indeterminado. Uma fonte assim serve de complemento, nao de base.

    O que esta API entrega e mais do que o GDELT: `title`, `summary`, `source`,
    `time_published` e sentimento ja calculado por artigo. **O sentimento fica
    em `dados` como CONTEXTO e nao entra no gate**, que continua sendo a
    contagem grosseira de `TERMOS`. O paragrafo 7 do PLAN.md proibe sinal novo
    antes de o replay poder julga-lo, e sentimento de terceiro e sinal novo.

    Duas armadilhas desta API, ambas com teste nomeando o erro:

    1. **Estouro de cota devolve HTTP 200.** O corpo vira `{"Information": ...}`
       sem a chave `feed`. Ler isso como "zero artigos" viraria `choque = 0`,
       ou seja, "nenhuma noticia relevante" — que e uma AFIRMACAO, e falsa. Sai
       indisponivel, como manda o `base.py`.
    2. **A chave `demo` responde 200 com dado real.** Ela so cobre os exemplos
       da documentacao (AAPL). Aceitar `demo` como chave configurada entregaria
       noticia de acao de tecnologia como se fosse do petroleo.

    O orcamento e defendido por construcao: `INTERVALO_MINIMO_S = 3600` cabe em
    24 chamadas por dia, dentro das 25. Chamada antes da hora devolve a ultima
    leitura, marcada `ao_vivo=False`.
    """

    nome = "alphavantage_news"
    URL = ("https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
           "&topics={t}&sort=LATEST&limit=50&apikey={k}")

    ORCAMENTO_DIARIO = 25
    INTERVALO_MINIMO_S = 3600.0        # 24/dia, dentro do orcamento
    IDADE_MAXIMA_H = 72.0              # feed parado nao e feed

    # De classe: a cota e da CHAVE, nao da instancia. Cada relatorio e cada
    # rerun do dashboard cria um provider novo; o contador nao pode nascer zero.
    _ultima_chamada = 0.0
    _ultima_leitura = None

    def __init__(self, topicos: str = "energy_transportation",
                 chave: str | None = None):
        self.topicos = topicos
        self._chave = chave

    @property
    def chave(self) -> str:
        if self._chave is not None:
            return self._chave
        from ..config import settings
        return settings().alphavantage_api_key or ""

    @classmethod
    def rearmar(cls) -> None:
        """Zera intervalo e cache. Para teste e para forcar nova tentativa."""
        cls._ultima_chamada = 0.0
        cls._ultima_leitura = None

    @staticmethod
    def _quando(bruto: str):
        """`time_published` vem como `20260822T134500`, sem fuso. E UTC."""
        try:
            return datetime.strptime(bruto, "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    def montar(self, dados: dict, *, agora=None,
               horas: float = JANELA_H) -> Leitura:
        """Separada de `ler` para ter teste com resposta gravada, sem rede."""
        if "feed" not in dados:
            # Cota estourada, chave invalida ou aviso: a API responde 200 e
            # explica em `Information` / `Note` / `Error Message`.
            aviso = (dados.get("Information") or dados.get("Note")
                     or dados.get("Error Message") or "resposta sem `feed`")
            return self._falha(str(aviso)[:200])

        agora = agora or datetime.now(timezone.utc)
        corte = agora - timedelta(hours=horas)
        titulos, notas, fontes = [], [], set()
        mais_novo = None
        for artigo in dados["feed"]:
            quando = self._quando(artigo.get("time_published", ""))
            if quando is None:
                continue
            if mais_novo is None or quando > mais_novo:
                mais_novo = quando
            if quando < corte:
                continue
            # Titulo e resumo juntos: os TERMOS do gate aparecem no corpo com
            # mais frequencia do que na manchete.
            titulos.append(" ".join(
                x for x in (artigo.get("title", ""), artigo.get("summary", ""))
                if x))
            fontes.add(artigo.get("source", ""))
            try:
                notas.append(float(artigo["overall_sentiment_score"]))
            except (KeyError, TypeError, ValueError):
                pass

        if mais_novo is None:
            return self._falha("nenhum artigo com data legivel")
        idade_h = (agora - mais_novo).total_seconds() / 3600.0
        if idade_h > self.IDADE_MAXIMA_H:
            # Mesma familia da armadilha dos futuros da EIA congelados em
            # 2024-04-05: HTTP 200 com dado morto.
            return self._falha(
                f"serie parada: artigo mais novo tem {idade_h / 24:.1f} dias "
                f"({mais_novo.date()}), maximo {self.IDADE_MAXIMA_H / 24:.0f}")

        return Leitura(self.nome, True, {
            "titulos": titulos,
            "n_no_feed": len(dados["feed"]),
            "fontes": sorted(f for f in fontes if f),
            # CONTEXTO, nao sinal. Ver docstring.
            "sentimento_medio": (round(sum(notas) / len(notas), 4)
                                 if notas else None),
            "idade_h": round(idade_h, 2),
        }, ao_vivo=True)

    def ler(self, horas: float = JANELA_H, **kw) -> Leitura:
        import json
        import time
        import urllib.request

        chave = self.chave
        if not chave:
            return self._falha(
                "sem alphavantage_api_key no .env (registro gratuito em "
                "https://www.alphavantage.co/support/#api-key)")
        if chave == "demo":
            return self._falha(
                "chave `demo` so cobre os exemplos da documentacao (AAPL); "
                "entregaria noticia de acao, nao de petroleo")

        falta = self.INTERVALO_MINIMO_S - (time.time()
                                           - AlphaVantageNews._ultima_chamada)
        if falta > 0:
            cache = AlphaVantageNews._ultima_leitura
            if cache is not None:
                return Leitura(self.nome, cache.disponivel, dict(cache.dados),
                               motivo=cache.motivo, ao_vivo=False)
            return self._falha(
                f"orcamento: {self.ORCAMENTO_DIARIO} chamadas/dia exige "
                f"{self.INTERVALO_MINIMO_S / 60:.0f} min entre elas; "
                f"faltam {falta / 60:.0f} min")

        AlphaVantageNews._ultima_chamada = time.time()
        url = self.URL.format(t=self.topicos, k=chave)
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                dados = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            leitura = self._falha(f"{type(exc).__name__}: {str(exc)[:120]}")
        else:
            leitura = self.montar(dados, horas=horas)
        AlphaVantageNews._ultima_leitura = leitura
        return leitura


def provider_noticias() -> Encadeado:
    """`dados_macro/noticias.csv` com colunas: data,titulo.

    Ordem: CSV manual -> RSS -> Alpha Vantage -> GDELT. Reordenada em
    2026-08-22 por confiabilidade MEDIDA, nao por qualidade de dado.

    O CSV segue no topo por ser o unico degrau que funciona sem rede. Em
    seguida vem o RSS, e nao o Alpha Vantage, por um motivo concreto: RSS nao
    pede chave, nao pede cadastro e nao tem cota, entao funciona no `.env` como
    ele esta hoje. O Alpha Vantage exige registro e so permite 25 chamadas por
    dia; entra como reforco quando a chave existir, trazendo `summary` e
    sentimento. O GDELT desce para ultimo recurso: responde de graca, mas o
    limite por IP dele tem memoria e fecha por tempo indeterminado
    (ver `Gdelt`).

    A escada inteira pode falhar sem quebrar nada: `choque = 0` com
    `disponivel=False` e o sistema opera igual — criterio 3 do check-6.
    """
    return provider_de_perfil("petroleo")


def provider_de_perfil(perfil: str) -> Encadeado:
    """A mesma escada, com a consulta do perfil pedido.

    O CSV manual fica de fora dos perfis que nao sao o padrao: ele e um
    arquivo unico e serviria noticia de petroleo para uma pergunta sobre
    memoria — dado errado entregue em silencio, que e a familia de erro que
    este arquivo mais documenta.
    """
    consulta = PERFIS.get(perfil, PERFIS["petroleo"])[1]
    if perfil == "petroleo":
        return Encadeado(ManualCSV("noticias.csv", ("data", "titulo")),
                         RssNoticias(consulta), AlphaVantageNews(), Gdelt(),
                         nome="noticias")
    return Encadeado(RssNoticias(consulta, feeds=FEEDS_PADRAO[:1]),
                     Gdelt(consulta), nome=f"noticias_{perfil}")


def _titulos(leitura: Leitura, agora: datetime, horas: float) -> list[str]:
    if "titulos" in leitura.dados:
        return leitura.dados["titulos"]
    corte = agora - timedelta(hours=horas)
    saida = []
    for linha in leitura.dados.get("linhas", []):
        try:
            quando = datetime.fromisoformat(linha["data"])
            if quando.tzinfo is None:
                quando = quando.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        if quando >= corte:
            saida.append(linha.get("titulo", ""))
    return saida


def limiar_do_perfil(perfil: str) -> float:
    """O choque que dispara o gate NAQUELE perfil."""
    return PERFIS.get(perfil, PERFIS["petroleo"])[3]


def choque(agora: datetime | None = None, horas: float = JANELA_H,
           perfil: str = "petroleo") -> Choque:
    """Intensidade da noticia nas ultimas `horas`. Sem fonte => zero, declarado.

    `perfil` escolhe o vocabulario E a consulta do feed. Sem ele, uma pergunta
    sobre memoria seria respondida com manchetes de petroleo — e o choque
    sairia zero, que nao e "sem fonte": e a AFIRMACAO de que nao ha noticia
    relevante.
    """
    agora = agora or datetime.now(timezone.utc)
    cfg = PERFIS.get(perfil, PERFIS["petroleo"])
    termos, assunto = (cfg[0] or TERMOS), cfg[2]
    leitura = provider_de_perfil(perfil).ler(horas=horas)
    if not leitura.disponivel:
        return Choque(0.0, 0, {}, leitura.motivo or "sem fonte", False)

    titulos = _titulos(leitura, agora, horas)
    contagem: dict[str, int] = {}
    total = 0.0
    for titulo in titulos:
        baixo = (titulo or "").lower()
        # Manchete que nao fala do setor nao soma. A busca do Google News traz
        # vizinhanca — "memory" puxa materia de RAM de celular, "tariff" puxa
        # comercio exterior de tudo — e sem este filtro elas pontuariam.
        if assunto and not any(a in baixo for a in assunto):
            continue
        for termo, peso in termos.items():
            if termo in baixo:
                contagem[termo] = contagem.get(termo, 0) + 1
                total += peso
    return Choque(total, len(titulos), contagem, leitura.fonte, True)


__all__ = ["Choque", "choque", "provider_noticias", "Gdelt",
           "AlphaVantageNews", "RssNoticias", "FEEDS_PADRAO", "TERMOS",
           "TERMOS_SEMICONDUTORES", "ASSUNTO_SEMICONDUTORES", "PERFIS",
           "provider_de_perfil", "limiar_do_perfil",
           "CHOQUE_ALTO", "JANELA_H"]
