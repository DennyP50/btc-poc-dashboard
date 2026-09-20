# BTC Daily POC dashboard

Webová aplikace pro vizualizaci BTCUSDT perpetual futures, předchozího denního POC, VAH/VAL, dosud netestovaných („naked“) POC a stavů strategie P0. Session je vždy `00:00–24:00 UTC`.

> POC/VA jsou aproximované z 1min svíček: objem každé svíčky se rozdělí přes její cenový rozsah. Přesný volume profile vyžaduje aggTrades/ticková data. Aplikace je analytická pomůcka, ne finanční doporučení.

Volba `30D` načítá 30 celých dní grafové historie plus dvě pomocné session pro úroveň na levém okraji. Tabulka záměrně zobrazuje pouze osm nejnovějších dokončených UTC dnů.

## Dva datové režimy

- `POC_MODE=preview` počítá orientační POC/VA z 1min svíček. Biny jiné než `$10` jsou označené jako průzkum a negenerují stav P0.
- `POC_MODE=exact` čte pevný `$10` profil z SQLite, naplněný Binance USDⓈ-M `aggTrades`. Dashboard v tomto režimu POC nepočítá.

Import ukládá všechny úrovně a objemové biny. Při zpracování každého dalšího dne zároveň uloží přesný čas prvního překřížení dřívějšího POC. Proto backfill spouštějte chronologicky a začněte dostatečně před obdobím backtestu.

## Pravidla P0 implementovaná v backendu

1. Použije se pouze pevný bin `$10`; VAH/VAL jsou jen kontext.
2. Potvrzení musí vzniknout na **uzavřené** 4H svíčce, která POC obchodovala a zavřela nad nebo pod ním.
3. Potvrzovací svíčka nikdy není současně retest. Retest musí přijít během následujících šesti uzavřených 4H svíček.
4. SHORT retest má `high >= POC` a `close < POC`; LONG retest má `low <= POC` a `close > POC`.
5. Close zpět skrz POC setup invaliduje. Bez retestu po šesti svíčkách setup expiruje.
6. Stop je za knotem retestovací svíčky plus `0,25 × ATR(14)`; ATR používá Wilderovo vyhlazení a pouze uzavřená data.
7. Target je nejbližší tehdy známý a stále nedotčený denní POC ve směru obchodu. Vyhledává se v celé databázi, ne jen v osmi řádcích tabulky.

Neuzavřená 4H svíčka je v grafu stínovaná a označená `NEUZAVŘENO · BEZ SIGNÁLU`. Varianty P1–P3 zatím nejsou implementované, protože jejich pravidla nejsou definovaná.

## Lokální spuštění

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Otevřete `http://localhost:8050`.

## Nasazení přes Coolify

1. Nahrajte tento adresář do Git repozitáře.
2. V Coolify zvolte **New Resource → Application** a připojte repozitář.
3. Jako **Build Pack** vyberte `Dockerfile`; cesta je `/Dockerfile`.
4. Nastavte **Ports Exposes** na `8050` a přidejte doménu.
5. Volitelně vložte proměnné z `.env.example`. Žádný Binance API klíč není potřeba.
6. Spusťte deploy. Image už obsahuje health check na `/health`.

### Přechod na přesná aggTrades data

1. V Coolify přidejte persistentní storage/volume s cílem `/data`.
2. První deploy ponechte s `POC_MODE=preview`.
3. V terminálu aplikačního kontejneru spusťte například:

```bash
python exact_poc.py --start 2026-06-01 --end 2026-09-18 --bin 10 \
  --database /data/poc.sqlite3 --cache /data/aggtrades
```

4. Spočítejte a uložte stavy P0 pouze z uzavřených 4H svíček:

```bash
python strategy_job.py --database /data/poc.sqlite3
```

5. Po úspěšném chronologickém importu nastavte `POC_MODE=exact` a aplikaci restartujte nebo redeployněte. Dashboard pak pouze čte přesné profily a hotové P0 snapshoty z SQLite.

`strategy_job.py` spouštějte po uzavření nové 4H svíčky. Volitelný parametr `--as-of 2026-09-20T08:00:00` umožňuje deterministický backtest bez přístupu k budoucím svíčkám.

V Coolify lze job nastavit přes **Configuration → Scheduled Tasks**:

```text
Name: Update P0 states
Command: python strategy_job.py --database /data/poc.sqlite3
Frequency: 10 * * * *
Timeout: 600
```

Hodinová kontrola je záměrná: funguje bez ohledu na timezone serveru, ale engine stav změní jen tehdy, když existuje nová uzavřená UTC 4H svíčka. Po uložení úlohy ji nejdřív spusťte přes **Execute Now** a zkontrolujte výstup.

Přidejte také idempotentní import posledního dokončeného UTC dne:

```text
Name: Import exact daily POC
Command: python daily_exact_job.py --database /data/poc.sqlite3 --cache /data/aggtrades
Frequency: 20 * * * *
Timeout: 3600
```

Dokud Binance archiv nepublikuje, job pouze vypíše informaci a další hodinu to zkusí znovu. Již uložený den znovu nezpracovává.

Funding a posledních 30 dní 4H open interest ukládá oddělený kontextový job. P0 je záměrně nepoužívá:

```text
Name: Update market context
Command: python context_data_job.py --days 30 --database /data/poc.sqlite3
Frequency: 15 * * * *
Timeout: 600
```

Denní BTCUSDT archivy mohou mít desítky MB a rozbalená CSV jsou výrazně větší. Pro dlouhý backtest importujte po dávkách a sledujte volné místo. Cache ZIP archivů lze po úspěšném importu smazat; SQLite profily zůstanou v persistentním volume.

Aplikace musí mít odchozí HTTPS přístup k `fapi.binance.com`. Pokud Binance vrací z IP serveru HTTP 451, použijte přes proměnnou `BINANCE_API_BASE` jiný legálně dostupný kompatibilní endpoint nebo jiný datový zdroj. Neměňte endpoint způsobem, který obchází místní omezení.

## Konfigurace

| Proměnná | Výchozí | Význam |
|---|---:|---|
| `SYMBOL` | `BTCUSDT` | Binance USDⓈ-M futures symbol |
| `DEFAULT_DAYS` | `14` | Úvodní rozsah: 7, 14 nebo 30 dní |
| `DEFAULT_BIN_SIZE` | `10` | Výchozí cenový bin; P0 je pevně `$10` |
| `CACHE_TTL_SECONDS` | `180` | Serverová cache Binance dat |
| `BINANCE_API_BASE` | `https://fapi.binance.com` | Základní URL kompatibilního futures API |
| `POC_MODE` | `preview` | `preview` nebo `exact` |
| `DATABASE_PATH` | `/data/poc.sqlite3` | SQLite databáze přesných profilů |
| `AGGTRADES_CACHE` | `/data/aggtrades` | Dočasná cache stažených ZIP archivů |
| `PORT` | `8050` | Interní HTTP port |

První načtení může trvat několik sekund, protože server stáhne 1min svíčky po stránkách. Další návštěvy v rámci cache jsou rychlé.
