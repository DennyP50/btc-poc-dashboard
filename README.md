# BTC Daily POC dashboard

Webová aplikace pro vizualizaci BTCUSDT perpetual futures, předchozího denního POC, VAH/VAL a dosud netestovaných („naked“) POC. Session je vždy `00:00–24:00 UTC`.

> POC/VA jsou aproximované z 1min svíček: objem každé svíčky se rozdělí přes její cenový rozsah. Přesný volume profile vyžaduje aggTrades/ticková data. Aplikace je analytická pomůcka, ne finanční doporučení.

Volba `30D` načítá 30 celých dní grafové historie plus dvě pomocné session pro úroveň na levém okraji. Tabulka záměrně zobrazuje pouze osm nejnovějších dokončených UTC dnů.

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

Aplikace musí mít odchozí HTTPS přístup k `fapi.binance.com`. Pokud Binance vrací z IP serveru HTTP 451, použijte přes proměnnou `BINANCE_API_BASE` jiný legálně dostupný kompatibilní endpoint nebo jiný datový zdroj. Neměňte endpoint způsobem, který obchází místní omezení.

## Konfigurace

| Proměnná | Výchozí | Význam |
|---|---:|---|
| `SYMBOL` | `BTCUSDT` | Binance USDⓈ-M futures symbol |
| `DEFAULT_DAYS` | `14` | Úvodní rozsah: 7, 14 nebo 30 dní |
| `DEFAULT_BIN_SIZE` | `25` | Velikost cenového koše v USD |
| `CACHE_TTL_SECONDS` | `180` | Serverová cache Binance dat |
| `BINANCE_API_BASE` | `https://fapi.binance.com` | Základní URL kompatibilního futures API |
| `PORT` | `8050` | Interní HTTP port |

První načtení může trvat několik sekund, protože server stáhne 1min svíčky po stránkách. Další návštěvy v rámci cache jsou rychlé.
