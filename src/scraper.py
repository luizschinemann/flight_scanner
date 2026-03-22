"""
Scraper de passagens aéreas via Playwright (Google Flights).

Estratégia v2 — Form-filling:
  1. Abre o Google Flights (página inicial, sem parâmetros de busca).
  2. Preenche o formulário passo a passo:
       a) Seleciona "Só de ida"
       b) Preenche origem (autocomplete)
       c) Preenche destino (autocomplete)
       d) Navega o calendário até o mês/dia correto e clica
       e) Clica em Pesquisar
  3. Aguarda os cards de resultado carregarem.
  4. Extrai preços, companhias, horários e escalas via JavaScript.
"""
from __future__ import annotations

import asyncio
import base64
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .config import ScraperConfig, SearchConfig
from .storage import FlightResult


# ── Regexes ────────────────────────────────────────────────────────────────────
_RE_PRICE = re.compile(r"R\$\s*([\d.]+(?:,\d+)?)")
_RE_TIME  = re.compile(r"\b(\d{1,2}:\d{2})\b")
_RE_DUR   = re.compile(r"(\d+)\s*h\s*(?:(\d+)\s*min)?")
_RE_STOPS = re.compile(r"sem parada|(\d+)\s*parada", re.IGNORECASE)


def _parse_price(text: str) -> Optional[float]:
    m = _RE_PRICE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_stops(text: str) -> int:
    m = _RE_STOPS.search(text)
    if not m:
        return -1
    return 0 if m.group(0).lower().startswith("sem") else int(m.group(1))


def _build_search_url(search: SearchConfig, currency: str = "BRL", lang: str = "pt-BR") -> str:
    """
    Constrói a URL de busca do Google Flights com o parâmetro `tfs` codificado.

    Estrutura do protobuf (obtida por reverse-engineering das URLs reais):
      Outer message:
        field 1 (varint) = 28            # constante
        field 2 (varint) = 1 (round)     # 1=ida+volta, 2=só de ida
                        ou 2 (one-way)
        field 3 (bytes)  = leg
      Leg:
        field 2  (bytes)  = "YYYY-MM-DD"    # data de saída
        field 13 (bytes)  = airport         # origem
        field 14 (bytes)  = airport         # destino
      Airport:
        field 1 (varint) = 1                # type=airport
        field 2 (bytes)  = "VDC"            # código IATA
    """
    def _airport(code: str) -> bytes:
        b = code.encode()
        return b"\x08\x01\x12" + bytes([len(b)]) + b

    def _leg(date_str: str, origin: str, dest: str) -> bytes:
        date_b = date_str.encode()
        orig_b = _airport(origin)
        dest_b = _airport(dest)
        return (
            b"\x12" + bytes([len(date_b)]) + date_b +   # field 2 = date
            b"\x6a" + bytes([len(orig_b)]) + orig_b +   # field 13 = origin
            b"\x72" + bytes([len(dest_b)]) + dest_b     # field 14 = destination
        )

    trip_type = 1 if search.is_round_trip else 2
    leg1 = _leg(search.outbound_date, search.origin, search.destination)
    msg  = b"\x08\x1c" + bytes([0x10, trip_type]) + b"\x1a" + bytes([len(leg1)]) + leg1

    if search.is_round_trip and search.return_date:
        # Adiciona leg de volta (sem data fixa, ou com data de retorno)
        leg2 = _leg(search.return_date, search.destination, search.origin)
        msg += b"\x1a" + bytes([len(leg2)]) + leg2

    tfs = base64.urlsafe_b64encode(msg).decode().rstrip("=")
    return (
        f"https://www.google.com/travel/flights/search"
        f"?tfs={tfs}&hl={lang.replace('-', '_')}&curr={currency}"
    )


# ── JavaScript de extração ──────────────────────────────────────────────────────
# Roda no contexto do browser. Retorna lista de objetos com dados dos voos.
_EXTRACT_JS = r"""
() => {
    const results = [];

    // Lista de seletores para os cards de voo (do mais específico ao mais genérico)
    const listSelectors = [
        'ul.Rk10dc > li',
        '[jsname="Mb7eie"] > li',
        '[role="list"] > [role="listitem"]',
        'li[data-id]',
    ];

    let cards = [];
    for (const sel of listSelectors) {
        const found = Array.from(document.querySelectorAll(sel));
        if (found.length >= 1) { cards = found; break; }
    }

    for (const card of cards.slice(0, 12)) {
        const text = (card.innerText || '').trim();
        if (text.length < 20) continue;
        if (!/R\$/.test(text)) continue;

        // ── Preço ──────────────────────────────────────────────────────
        const priceMatch = text.match(/R\$\s*([\d.]+(?:,\d+)?)/);
        if (!priceMatch) continue;
        const price = parseFloat(
            priceMatch[1].replace(/\./g, '').replace(',', '.')
        );
        if (isNaN(price) || price <= 0) continue;

        // ── Horários: captura TODOS os horários no card ────────────────
        // Para round-trip: [0]=saída ida, [1]=chegada ida, [2]=saída volta, [3]=chegada volta
        // Para one-way: [0]=saída, [1]=chegada
        const times = (text.match(/\b\d{1,2}:\d{2}\b/g) || []);
        const outbound_dep = times[0] || '';
        const outbound_arr = times[1] || '';
        const return_dep   = times[2] || '';
        const return_arr   = times[3] || '';

        // ── Companhia: detecta pelo texto do card ─────────────────────
        let airline = '';
        const airlineMap = [
            [/latam/i,   'Latam Airlines'],
            [/\bgol\b/i, 'Gol'],
            [/azul/i,    'Azul'],
            [/avianca/i, 'Avianca'],
            [/passaredo/i, 'Passaredo'],
        ];
        for (const [re, name] of airlineMap) {
            if (re.test(text)) { airline = name; break; }
        }

        results.push({
            priceText    : text,
            price        : price,
            outbound_dep : outbound_dep,
            outbound_arr : outbound_arr,
            return_dep   : return_dep,
            return_arr   : return_arr,
            airline      : airline,
        });
    }
    return results;
}
"""


# ── Scraper principal ──────────────────────────────────────────────────────────

class FlightScraper:
    def __init__(self, config: ScraperConfig, currency: str = "BRL", language: str = "pt-BR"):
        self.config   = config
        self.currency = currency
        self.language = language
        self._pw      = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None

    async def start(self):
        self._pw = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": self.config.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
            ],
        }
        if self.config.proxy:
            launch_kwargs["proxy"] = {"server": self.config.proxy}

        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._ctx = await self._browser.new_context(
            user_agent=self.config.user_agent,
            viewport={"width": 1366, "height": 768},
            locale=self.language,
            timezone_id="America/Sao_Paulo",
        )
        await self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

    async def stop(self):
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ── API pública ────────────────────────────────────────────────────────────

    async def search(self, search: SearchConfig) -> list[FlightResult]:
        last_err: Exception = RuntimeError("sem tentativas")
        for attempt in range(1, self.config.retry_attempts + 1):
            try:
                results = await self._do_search(search, attempt)
                if results:
                    # Ordena por score (horário prioritário, depois preço)
                    sorted_results = sorted(results, key=lambda r: _flight_score(r, search))
                    if sorted_results:
                        best = sorted_results[0]
                        logger.info(
                            f"[{search.id}] Melhor opção (horário prioritário): "
                            f"ida {best.departure_time} volta {best.return_departure_time or 'N/A'} "
                            f"— R$ {best.price:,.2f}"
                        )
                    return sorted_results
                logger.warning(f"[{search.id}] Tentativa {attempt}: sem resultados")
            except Exception as exc:
                last_err = exc
                logger.warning(f"[{search.id}] Tentativa {attempt} falhou: {exc}")
                if attempt < self.config.retry_attempts:
                    await asyncio.sleep(5)
        logger.error(f"[{search.id}] Todas as tentativas falharam: {last_err}")
        return []

    # ── Implementação ──────────────────────────────────────────────────────────

    async def _do_search(self, search: SearchConfig, attempt: int) -> list[FlightResult]:
        page: Page = await self._ctx.new_page()
        scraped_at = datetime.now().isoformat(timespec="seconds")
        try:
            # ── Navega direto para a URL de busca (sem form-filling) ───────────
            search_url = _build_search_url(search, self.currency, self.language)
            logger.debug(f"[{search.id}] Navegando para: {search_url[:120]}")
            await page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
            await self._handle_consent(page)

            # Para round-trip, a URL preenche o form mas não o submete;
            # detecta isso e clica em Pesquisar
            if search.is_round_trip:
                page_title = await page.title()
                if "→" not in page_title:
                    logger.debug(f"[{search.id}] Explore page detectada — clicando Pesquisar")
                    await self._submit_search(page)
                    await asyncio.sleep(3)

            # Aguarda resultados carregarem
            loaded = await self._wait_for_results(page)
            if not loaded:
                if self.config.screenshot_on_error:
                    await self._screenshot(page, search.id, attempt)
                raise RuntimeError("Timeout aguardando resultados")

            # Pausa extra para lazy-loading de preços
            await asyncio.sleep(4)

            # Debug: URL e título da página
            current_url = page.url
            page_title  = await page.title()
            logger.debug(f"[{search.id}] URL final: {current_url[:120]}")
            logger.debug(f"[{search.id}] Título: {page_title[:80]}")

            # Screenshot dos resultados
            await self._screenshot(page, search.id + "_results", attempt)

            # 9. Extração
            raw_cards: list[dict] = await page.evaluate(_EXTRACT_JS)
            logger.debug(f"[{search.id}] {len(raw_cards)} cards extraídos")
            if raw_cards:
                first = raw_cards[0]
                logger.debug(f"[{search.id}] Primeiro card — preço={first.get('price')} airline='{first.get('airline')}' outbound={first.get('outbound_dep')}→{first.get('outbound_arr')} return={first.get('return_dep')}→{first.get('return_arr')}")
                logger.debug(f"[{search.id}] Texto bruto (200 chars): {first.get('priceText','')[:200]}")

            results: list[FlightResult] = []
            for card in raw_cards:
                price = card.get("price", 0.0)
                if not price or price <= 0:
                    continue
                text = card.get("priceText", "")
                dur_m = _RE_DUR.search(text)
                duration = ""
                if dur_m:
                    h = dur_m.group(1)
                    m = dur_m.group(2) or "0"
                    duration = f"{h}h{m}min" if int(m) else f"{h}h"

                airline = card.get("airline", "")

                results.append(FlightResult(
                    search_id             = search.id,
                    origin                = search.origin,
                    destination           = search.destination,
                    outbound_date         = search.outbound_date,
                    return_date           = search.return_date,
                    price                 = price,
                    currency              = self.currency,
                    airline               = airline,
                    departure_time        = card.get("outbound_dep", ""),
                    arrival_time          = card.get("outbound_arr", ""),
                    duration              = duration,
                    stops                 = _parse_stops(text),
                    scraped_at            = scraped_at,
                    return_departure_time = card.get("return_dep", ""),
                    return_arrival_time   = card.get("return_arr", ""),
                ))
            return results

        finally:
            await page.close()

    # ── Helpers de formulário ──────────────────────────────────────────────────

    async def _handle_consent(self, page: Page):
        """Fecha popup de cookies/consentimento do Google."""
        for text in ["Aceitar tudo", "Accept all", "Concordo", "Agree"]:
            try:
                btn = page.locator(f"button:has-text('{text}')").first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                pass

    async def _set_one_way(self, page: Page):
        """Seleciona 'Só de ida' no dropdown de tipo de viagem."""
        # Tenta clicar no botão do tipo de viagem (mostra "Viagem de ida e volta")
        trip_selectors = [
            'button[data-value="1"]',
            '[jsname="SyVSEc"]',
            'div[jsname="wQNmvb"] button',
        ]
        for sel in trip_selectors:
            try:
                await page.click(sel, timeout=3000)
                await asyncio.sleep(0.5)
                # Seleciona "Só de ida"
                for opt in ['[data-value="2"]', 'li:has-text("Só de ida")', 'li:has-text("One way")']:
                    try:
                        await page.click(opt, timeout=2000)
                        logger.debug("Trip type: só de ida")
                        return
                    except Exception:
                        pass
            except Exception:
                pass

    async def _fill_airport(self, page: Page, code: str, is_origin: bool):
        """
        Preenche o campo de aeroporto (origem ou destino) e seleciona via autocomplete.
        Usa page.keyboard.type() para disparar os eventos de input corretamente.
        """
        label_candidates = (
            ["De onde", "Origem", "Where from", "From"] if is_origin
            else ["Para onde", "Destino", "Where to", "To"]
        )

        input_el = None
        matched_label = ""
        for label in label_candidates:
            for sel in [
                f'input[aria-label*="{label}"]',
                f'[aria-label*="{label}"] input',
                f'[placeholder*="{label}"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        input_el = el
                        matched_label = label
                        break
                except Exception:
                    pass
            if input_el:
                break

        if input_el is None:
            # Fallback: n-ésimo input de texto
            idx = 0 if is_origin else 1
            try:
                inputs = await page.locator('input[type="text"]').all()
                if len(inputs) > idx:
                    input_el = inputs[idx]
                    matched_label = f"input[{idx}]"
            except Exception:
                pass

        if input_el is None:
            logger.warning(f"Campo de aeroporto não encontrado para {code}")
            return

        # Um único clique abre o modal/dialog e limpa o campo
        await input_el.click()
        await asyncio.sleep(1.0)
        # Seleciona tudo e digita (por segurança, mas o campo já deve estar vazio)
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.1)
        # type() simula digitação real (dispara keydown/keyup/input events)
        await page.keyboard.type(code, delay=100)
        logger.debug(f"Código '{code}' digitado no campo '{matched_label}'")

        # Aguarda dropdown aparecer
        await asyncio.sleep(2.5)

        # Seleciona a primeira opção que contenha o código IATA
        # Google Flights exibe "São Paulo (GRU)" — has-text faz match de substring
        for opt_sel in [
            f'[role="option"]:has-text("{code}")',
            f'li[role="option"]:has-text("{code}")',
            '[role="listbox"] [role="option"]',
            'ul[role="listbox"] li',
            '[role="option"]',
        ]:
            try:
                first_opt = page.locator(opt_sel).first
                if await first_opt.is_visible(timeout=2000):
                    opt_text = await first_opt.inner_text()
                    await first_opt.click()
                    logger.debug(f"Autocomplete {code}: selecionou '{opt_text[:50].strip()}'")
                    return
            except Exception:
                pass

        # Último recurso: Enter
        logger.warning(f"Dropdown não apareceu para {code}, pressionando Enter")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

    async def _fill_date(self, page: Page, date_str: str, is_return: bool = False):
        """
        Preenche a data de ida/volta.
        Estratégia 1: digita DD/MM/YYYY no campo de texto e pressiona Enter.
        Estratégia 2: navega calendário mês a mês e clica no dia.
        """
        year, month, day = [int(x) for x in date_str.split("-")]
        target = date(year, month, day)
        # Formato esperado pelo Google Flights em pt-BR
        date_formatted = f"{day:02d}/{month:02d}/{year}"

        date_label_candidates = (
            ["Data de volta", "Volta", "Return"] if is_return
            else ["Data de ida", "Partida", "Departure", "Ida"]
        )

        date_el = None
        for label in date_label_candidates:
            for sel in [
                f'input[aria-label*="{label}"]',
                f'[aria-label*="{label}"] input',
                f'[placeholder*="{label}"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        date_el = el
                        logger.debug(f"Campo de data encontrado: '{label}'")
                        break
                except Exception:
                    pass
            if date_el:
                break

        # ── Estratégia 1: digitar a data diretamente ───────────────────────────
        if date_el is not None:
            try:
                await date_el.click()
                await asyncio.sleep(0.5)
                await page.keyboard.press("Control+a")
                await asyncio.sleep(0.1)
                await page.keyboard.type(date_formatted, delay=60)
                logger.debug(f"Data digitada: {date_formatted}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Erro ao digitar data: {e}")
        else:
            logger.warning(f"Campo de data não encontrado — tentando calendário direto")

        # ── Tenta selecionar via data-iso (aparece após digitar ou no calendário) ──
        try:
            iso_sel = f'[data-iso="{date_str}"]'
            el = page.locator(iso_sel).first
            if await el.is_visible(timeout=2000):
                await el.click()
                logger.debug(f"Data {date_str} selecionada via data-iso")
                return
        except Exception:
            pass

        # ── Estratégia 2: navegar calendário mês a mês ────────────────────────
        today = date.today()
        months_to_advance = max(0, (target.year - today.year) * 12 + (target.month - today.month))

        next_btn_selectors = [
            '[aria-label="Próximo mês"]',
            '[aria-label="Next month"]',
            'button[aria-label*="ximo"]',
            'button[aria-label*="next"]',
        ]

        for _ in range(months_to_advance):
            for sel in next_btn_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    pass

        await asyncio.sleep(0.5)

        # Nomes dos meses em português para montar aria-label
        _PT_MONTHS = {
            1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
            5: "maio", 6: "junho", 7: "julho", 8: "agosto",
            9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
        }
        month_name = _PT_MONTHS.get(month, "")

        day_selectors = [
            f'[data-iso="{date_str}"]',
            f'[aria-label="{day} de {month_name} de {year}"]',
            f'[aria-label*="{day} de {month_name}"]',
            f'td[role="gridcell"]:has-text("{day}")',
            f'div[role="gridcell"]:has-text("{day}")',
        ]
        for sel in day_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    logger.debug(f"Dia clicado no calendário: {day_str_log(date_str)}")
                    return
            except Exception:
                pass

        # Último recurso: Enter confirma o valor já digitado
        logger.warning(f"Data {date_str}: usando Enter como fallback final")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

    async def _submit_search(self, page: Page):
        """Confirma o calendário (se aberto) e clica no botão Pesquisar."""
        # Tenta fechar calendário clicando em "Concluído" ou "Done"
        for close_label in ["Concluído", "Done", "OK"]:
            try:
                btn = page.locator(f'button:has-text("{close_label}")').first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        # Clica em Pesquisar
        search_selectors = [
            'button[aria-label*="Pesquisar"]',
            'button[aria-label*="Search"]',
            'button:has-text("Pesquisar")',
            '[jsname="vLv7Lb"]',
        ]
        for sel in search_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    logger.debug("Botão Pesquisar clicado")
                    return
            except Exception:
                pass

        # Fallback: Enter
        await page.keyboard.press("Enter")

    async def _wait_for_results(self, page: Page) -> bool:
        """Aguarda pelo menos um card de resultado com preço aparecer."""
        timeout = self.config.timeout_ms
        card_selectors = [
            "ul.Rk10dc > li",
            "[jsname='Mb7eie'] > li",
            "li[data-id]",
            "[role='list'] > [role='listitem']",
        ]
        for sel in card_selectors:
            try:
                await page.wait_for_selector(sel, timeout=timeout // len(card_selectors))
                return True
            except Exception:
                pass

        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
            return "R$" in await page.content()
        except Exception:
            return False

    async def _screenshot(self, page: Page, search_id: str, attempt: int):
        path = (
            Path("data/screenshots")
            / f"{search_id}_attempt{attempt}_{datetime.now():%Y%m%d_%H%M%S}.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(path), full_page=True)
            logger.info(f"Screenshot: {path}")
        except Exception as e:
            logger.warning(f"Screenshot falhou: {e}")


def day_str_log(date_str: str) -> str:
    try:
        y, m, d = date_str.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return date_str


def _time_penalty(actual: str, preferred: str) -> float:
    """
    Calcula penalidade baseada na diferença entre horário real e preferido.
    Retorna: 0.0 (perfeito) até 1.0 (12h de diferença).
    """
    if not actual or not preferred:
        return 0.5  # Penalidade neutra se horário não disponível

    try:
        # Converte HH:MM para minutos desde meia-noite
        h_act, m_act = map(int, actual.split(":"))
        h_pref, m_pref = map(int, preferred.split(":"))

        minutes_actual = h_act * 60 + m_act
        minutes_pref = h_pref * 60 + m_pref

        # Diferença em minutos (absoluta)
        diff = abs(minutes_actual - minutes_pref)

        # Normaliza para 0-1 (12h = 720min como máximo)
        penalty = min(diff / 720.0, 1.0)
        return penalty
    except Exception:
        return 0.5


def _flight_score(flight: FlightResult, search: SearchConfig) -> float:
    """
    Calcula score de ordenação combinando preço e preferências de horário.
    Score menor = melhor opção.

    Componentes:
    - Horário de ida diferente do preferido (peso 40%)
    - Horário de volta diferente do preferido (peso 40%)
    - Preço normalizado (peso 20%)
    """
    # Peso de cada componente (HORÁRIO É PRIORITÁRIO)
    OUTBOUND_TIME_WEIGHT = 0.40
    RETURN_TIME_WEIGHT = 0.40
    PRICE_WEIGHT = 0.20

    # Componente 1: Horário de ida (PRIORITÁRIO)
    outbound_penalty = _time_penalty(flight.departure_time, search.preferred_outbound_time)

    # Componente 2: Horário de volta (PRIORITÁRIO, apenas para round-trip)
    return_penalty = 0.0
    if search.is_round_trip and flight.return_departure_time:
        return_penalty = _time_penalty(flight.return_departure_time, search.preferred_return_time)

    # Componente 3: Preço (secundário)
    # Normaliza assumindo que R$ 3000 seria um preço muito alto
    price_score = min(flight.price / 3000.0, 1.0)

    # Score final (0.0 = perfeito, 1.0 = péssimo)
    # Horários têm peso 80% combinado, preço apenas 20%
    total_score = (
        outbound_penalty * OUTBOUND_TIME_WEIGHT +
        return_penalty * RETURN_TIME_WEIGHT +
        price_score * PRICE_WEIGHT
    )

    return total_score
