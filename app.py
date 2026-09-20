"""BTC Daily POC web dashboard for a Docker/Coolify deployment."""

from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html
from plotly.subplots import make_subplots

from poc_engine import (
    MarketDataError,
    calculate_daily_levels,
    load_recent_klines,
    resample_candles,
    summarize_market,
)


SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))
DEFAULT_BIN_SIZE = float(os.getenv("DEFAULT_BIN_SIZE", "25"))

app = Dash(__name__, title="BTC Daily POC", update_title=None)
server = app.server


@server.get("/health")
def health():
    return {"status": "ok", "service": "btc-daily-poc"}, 200


def _metric(label: str, value_id: str, detail_id: str, accent: str = "") -> html.Div:
    return html.Div(
        className=f"metric-card {accent}",
        children=[
            html.Div(label, className="metric-label"),
            html.Div("—", id=value_id, className="metric-value"),
            html.Div("", id=detail_id, className="metric-detail"),
        ],
    )


app.layout = html.Div(
    className="app-shell",
    children=[
        html.Header(
            className="topbar",
            children=[
                html.Div(
                    className="brand",
                    children=[
                        html.Div("₿", className="brand-mark"),
                        html.Div([html.H1("BTC Daily POC"), html.P("Perpetual futures · UTC sessions")]),
                    ],
                ),
                html.Div([html.Span(className="status-dot"), html.Span("MARKET DATA")], className="live-badge"),
            ],
        ),
        html.Main(
            children=[
                html.Section(
                    className="metrics-grid",
                    children=[
                        _metric("BTCUSDT", "price-value", "price-detail", "price-card"),
                        _metric("Předchozí POC", "poc-value", "poc-detail", "poc-card"),
                        _metric("Value area", "va-value", "va-detail", "va-card"),
                        _metric("Tržní kontext", "context-value", "context-detail", "context-card"),
                    ],
                ),
                html.Section(
                    className="workspace",
                    children=[
                        html.Div(
                            className="chart-panel",
                            children=[
                                html.Div(
                                    className="control-row",
                                    children=[
                                        html.Div(
                                            [html.Label("Timeframe"), dcc.RadioItems(
                                                id="timeframe",
                                                options=[{"label": x, "value": x} for x in ("15m", "1h", "4h")],
                                                value="1h",
                                                inline=True,
                                                className="segmented",
                                                inputClassName="segment-input",
                                                labelClassName="segment-label",
                                            )],
                                            className="control-group",
                                        ),
                                        html.Div(
                                            [html.Label("Historie"), dcc.RadioItems(
                                                id="lookback",
                                                options=[
                                                    {"label": "7D", "value": 7},
                                                    {"label": "14D", "value": 14},
                                                    {"label": "30D", "value": 30},
                                                ],
                                                value=DEFAULT_DAYS if DEFAULT_DAYS in (7, 14, 30) else 14,
                                                inline=True,
                                                className="segmented",
                                                inputClassName="segment-input",
                                                labelClassName="segment-label",
                                            )],
                                            className="control-group",
                                        ),
                                        html.Div(
                                            [html.Label("Price bin"), dcc.Dropdown(
                                                id="bin-size",
                                                options=[
                                                    {"label": "$10", "value": 10},
                                                    {"label": "$25", "value": 25},
                                                    {"label": "$50", "value": 50},
                                                    {"label": "$100", "value": 100},
                                                ],
                                                value=DEFAULT_BIN_SIZE if DEFAULT_BIN_SIZE in (10, 25, 50, 100) else 25,
                                                clearable=False,
                                                searchable=False,
                                            )],
                                            className="control-group bin-control",
                                        ),
                                        html.Div(id="update-status", className="update-status"),
                                    ],
                                ),
                                html.Div(
                                    className="method-notice",
                                    children=[
                                        html.Span("ORIENTAČNÍ PROFIL", className="method-tag"),
                                        html.Span("POC/VA jsou počítané z 1min svíček; objem svíčky se rozděluje přes její high–low. Nejde o přesný aggTrades profil."),
                                    ],
                                ),
                                dcc.Loading(
                                    type="dot",
                                    color="#f7931a",
                                    children=dcc.Graph(
                                        id="market-chart",
                                        config={
                                            "displaylogo": False,
                                            "scrollZoom": True,
                                            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                                            "responsive": True,
                                        },
                                    ),
                                ),
                            ],
                        ),
                        html.Aside(
                            className="side-panel",
                            children=[
                                html.Div(
                                    className="panel-heading",
                                    children=[html.Div([html.H2("Denní úrovně"), html.P("Posledních 8 dokončených UTC dnů")])],
                                ),
                                html.Div(id="levels-table", className="levels-table-wrap"),
                                html.Div(
                                    className="playbook",
                                    children=[
                                        html.H3("Jak číst kontext"),
                                        html.Div([html.Span("Nad VAH", className="guide-chip above"), html.P("Sleduj přijetí ceny nad value, nebo návrat zpět dovnitř.")], className="guide-row"),
                                        html.Div([html.Span("Uvnitř VA", className="guide-chip inside"), html.P("Trh bývá více rotační; POC funguje jako střed hodnoty.")], className="guide-row"),
                                        html.Div([html.Span("Pod VAL", className="guide-chip below"), html.P("Sleduj přijetí pod value, nebo odmítnutí a návrat výš.")], className="guide-row"),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                html.Footer(
                    [
                        html.Span("POC/VA jsou aproximované z 1min svíček Binance Futures; přesný profil vyžaduje jednotlivé obchody."),
                        html.Span("Pouze analytický nástroj — ne finanční doporučení."),
                    ]
                ),
            ]
        ),
        dcc.Interval(id="refresh", interval=60_000, n_intervals=0),
    ],
)


def _price(value: float) -> str:
    return f"${value:,.0f}".replace(",", " ")


def _build_chart(klines: pd.DataFrame, levels: pd.DataFrame, timeframe: str, days: int) -> go.Figure:
    candles = resample_candles(klines, timeframe)
    display_start = klines["ts"].max() - pd.Timedelta(days=days)
    candles = candles[candles.index >= display_start]
    chart_end = candles.index[-1] + pd.Timedelta(timeframe.replace("m", "min"))

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.78, 0.22],
    )
    figure.add_trace(
        go.Candlestick(
            x=candles.index,
            open=candles["open"],
            high=candles["high"],
            low=candles["low"],
            close=candles["close"],
            name=SYMBOL,
            increasing_line_color="#22c997",
            decreasing_line_color="#ff5c73",
            increasing_fillcolor="#22c997",
            decreasing_fillcolor="#ff5c73",
            whiskerwidth=0.35,
        ),
        row=1,
        col=1,
    )
    volume_colors = ["rgba(34,201,151,.40)" if close >= open_ else "rgba(255,92,115,.38)" for open_, close in zip(candles["open"], candles["close"])]
    figure.add_trace(
        go.Bar(x=candles.index, y=candles["volume"], marker_color=volume_colors, name="Objem", hovertemplate="%{y:,.2f} BTC<extra>Objem</extra>"),
        row=2,
        col=1,
    )

    completed = levels[levels["day"] < klines["ts"].max().floor("D")]

    def add_segments(column: str, name: str, color: str, dash: str = "solid", width: float = 1.4) -> None:
        xs: list = []
        ys: list = []
        for level in completed.itertuples(index=False):
            start = level.day + pd.Timedelta(days=1)
            end = min(start + pd.Timedelta(days=1), chart_end)
            if end >= display_start and start <= chart_end:
                xs.extend([max(start, display_start), end, None])
                ys.extend([getattr(level, column), getattr(level, column), None])
        figure.add_trace(
            go.Scatter(x=xs, y=ys, mode="lines", name=name, line={"color": color, "width": width, "dash": dash}, hoverinfo="skip"),
            row=1,
            col=1,
        )

    add_segments("poc", "Previous day POC", "#f7931a", width=2.2)
    add_segments("vah", "Previous day VAH", "#31c8e6", dash="dot")
    add_segments("val", "Previous day VAL", "#b084ff", dash="dot")

    naked_x: list = []
    naked_y: list = []
    for level in completed.itertuples(index=False):
        start = level.day + pd.Timedelta(days=1)
        if pd.notna(level.touched_at):
            continue
        if start <= chart_end:
            naked_x.extend([max(start, display_start), chart_end, None])
            naked_y.extend([level.poc, level.poc, None])
    figure.add_trace(
        go.Scatter(
            x=naked_x,
            y=naked_y,
            mode="lines",
            name="Naked POC",
            line={"color": "#f4c95d", "width": 1.1, "dash": "dash"},
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )

    last_price = float(klines.iloc[-1]["close"])
    figure.add_hline(y=last_price, line_width=1, line_dash="dot", line_color="rgba(255,255,255,.5)", row=1, col=1)
    figure.add_annotation(
        x=1,
        xref="paper",
        y=last_price,
        yref="y",
        text=_price(last_price),
        showarrow=False,
        xanchor="left",
        bgcolor="#e8edf6",
        borderpad=3,
        font={"color": "#111827", "size": 11},
    )

    figure.update_layout(
        height=690,
        margin={"l": 12, "r": 72, "t": 18, "b": 20},
        paper_bgcolor="#0b1018",
        plot_bgcolor="#0b1018",
        font={"family": "Inter, ui-sans-serif, system-ui, sans-serif", "color": "#8f9bad", "size": 11},
        hovermode="x unified",
        hoverlabel={"bgcolor": "#141c28", "bordercolor": "#263245", "font_color": "#e8edf6"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0, "font": {"size": 11}},
        xaxis_rangeslider_visible=False,
        bargap=0.12,
        dragmode="pan",
        uirevision=f"{timeframe}-{days}",
    )
    figure.update_xaxes(
        gridcolor="rgba(143,155,173,.08)",
        showspikes=True,
        spikecolor="rgba(255,255,255,.25)",
        spikethickness=1,
        rangeslider_visible=False,
    )
    figure.update_yaxes(gridcolor="rgba(143,155,173,.10)", side="right", fixedrange=False, row=1, col=1)
    figure.update_yaxes(gridcolor="rgba(143,155,173,.06)", side="right", title=None, row=2, col=1)
    return figure


def _levels_table(levels: pd.DataFrame, latest_day: pd.Timestamp) -> html.Table:
    rows = []
    completed = levels[levels["day"] < latest_day].tail(8).iloc[::-1]
    for row in completed.itertuples(index=False):
        is_naked = pd.isna(row.touched_at)
        rows.append(
            html.Tr(
                [
                    html.Td(row.day.strftime("%d.%m"), className="date-cell"),
                    html.Td(_price(row.poc), className="number-cell poc-number"),
                    html.Td(f"{_price(row.val)} – {_price(row.vah)}", className="number-cell range-number"),
                    html.Td(html.Span("NAKED" if is_naked else "TEST", className=f"level-state {'naked' if is_naked else 'tested'}")),
                ]
            )
        )
    return html.Table(
        [html.Thead(html.Tr([html.Th("Den"), html.Th("POC"), html.Th("VAL – VAH"), html.Th("Stav")])) , html.Tbody(rows)],
        className="levels-table",
    )


def _empty_figure(message: str) -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False, font={"size": 15, "color": "#ff8b9b"})
    figure.update_layout(height=690, paper_bgcolor="#0b1018", plot_bgcolor="#0b1018", xaxis={"visible": False}, yaxis={"visible": False}, margin={"l": 20, "r": 20, "t": 20, "b": 20})
    return figure


@app.callback(
    Output("market-chart", "figure"),
    Output("price-value", "children"),
    Output("price-detail", "children"),
    Output("poc-value", "children"),
    Output("poc-detail", "children"),
    Output("va-value", "children"),
    Output("va-detail", "children"),
    Output("context-value", "children"),
    Output("context-detail", "children"),
    Output("context-value", "className"),
    Output("levels-table", "children"),
    Output("update-status", "children"),
    Input("timeframe", "value"),
    Input("lookback", "value"),
    Input("bin-size", "value"),
    Input("refresh", "n_intervals"),
)
def update_dashboard(timeframe: str, days: int, bin_size: float, _refresh: int):
    try:
        # Two extra sessions provide the previous-day level at the left edge.
        klines = load_recent_klines(SYMBOL, int(days) + 2)
        levels = calculate_daily_levels(klines, float(bin_size))
        summary = summarize_market(klines, levels)
        figure = _build_chart(klines, levels, timeframe, int(days))
        display_start = klines["ts"].max() - pd.Timedelta(days=int(days))
        displayed_minutes = int(klines.loc[klines["ts"] >= display_start, "ts"].nunique())
        expected_minutes = int(days) * 1440 + 1
        coverage = min(100.0, displayed_minutes / expected_minutes * 100)
        direction = "+" if summary.change_24h_pct >= 0 else ""
        distance = "+" if summary.distance_to_poc_pct >= 0 else ""
        return (
            figure,
            _price(summary.price),
            f"{direction}{summary.change_24h_pct:.2f}% za 24 h",
            _price(summary.poc),
            f"{distance}{summary.distance_to_poc_pct:.2f}% od POC · {summary.level_day}",
            f"{_price(summary.val)} – {_price(summary.vah)}",
            "70 % objemu předchozí UTC session",
            summary.context,
            "Cena vůči včerejší value area",
            f"metric-value context-{summary.context_class}",
            _levels_table(levels, klines["ts"].max().floor("D")),
            f"{int(days)}D · {coverage:.1f}% 1m dat · do {summary.updated_at.strftime('%H:%M')} UTC",
        )
    except (MarketDataError, ValueError, KeyError) as exc:
        message = str(exc)
        return (
            _empty_figure(message),
            "—", "Data nejsou dostupná", "—", "—", "—", "—", "Bez dat", "Zkontrolujte připojení", "metric-value context-below",
            html.Div(message, className="table-error"),
            "Chyba načtení",
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8050")), debug=os.getenv("DEBUG", "false").lower() == "true")
