import Foundation

// The wire contract for GET /api/predict. Every field name here is a
// CodingKey mapped onto the server's own JSON — see analysis/forecast.py's
// Forecast.as_dict() and scheduler/health.py's _api_predict for the source
// of truth. Keep these two in lockstep by hand; there is no shared schema
// between the two languages, which is exactly the kind of drift PORT_NOTES.md
// flags as the class of bug that hurt this project twice already (a parser
// silently accepting a shape that wasn't the real one). If a field is added
// or renamed on the server, this file has to be updated in the same change.

/// A band and a lean for one market at one horizon — never a point estimate.
/// Nobody can say a coin will be at an exact price in four hours; what can be
/// defended is how far it usually travels and which way the evidence leans.
struct PriceForecast: Codable, Identifiable, Hashable {
    var id: String { label }

    let horizonMinutes: Int
    /// "1h" | "4h" | "24h" — see HORIZON_LABEL in analysis/forecast.py.
    let label: String
    let centre: Double
    let low: Double
    let high: Double
    let changePct: Double
    /// One standard deviation of typical travel, as a percent of price.
    let bandPct: Double
    let direction: Direction
    /// -1...+1 weight of evidence from the five confluence families.
    let lean: Double
    /// 0...1 — how much of the panel actually voted, not a probability that
    /// the forecast is "right". Treat it as a coverage figure, not a P(true).
    let confidence: Double

    enum Direction: String, Codable {
        case up, down, flat

        /// Falls back to `.flat` for anything unrecognised rather than
        /// failing to decode the whole response — a server-side vocabulary
        /// change should degrade this one field, not blank the screen.
        init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Direction(rawValue: raw) ?? .flat
        }
    }

    enum CodingKeys: String, CodingKey {
        case horizonMinutes = "horizon_minutes"
        case label, centre, low, high
        case changePct = "change_pct"
        case bandPct = "band_pct"
        case direction, lean, confidence
    }
}

/// One market's current reading plus its forecasts — one entry of
/// `/api/predict`'s `markets` array.
struct MarketOutlook: Codable, Identifiable, Hashable {
    var id: String { symbol }

    let symbol: String
    let price: Double
    let change24hPct: Double
    let rsi: Double
    /// One-minute ATR as a percent of price — the number that decides every
    /// level on the server. nil means the feed has not warmed up yet.
    let atrPct: Double?
    let relativeVolume: Double?
    let candles: Int
    let dataAgeMinutes: Double?
    /// True when the newest candle is stale (see scheduler/health.py) — the
    /// view must show this rather than silently render an old picture as if
    /// it were live. A stale board caused real trading losses before it was
    /// surfaced; do not let the iOS port repeat that.
    let stale: Bool
    let forecasts: [PriceForecast]
    let direction: PriceForecast.Direction?
    let confidence: Double?

    enum CodingKeys: String, CodingKey {
        case symbol, price
        case change24hPct = "change_24h_pct"
        case rsi
        case atrPct = "atr_pct"
        case relativeVolume = "relative_volume"
        case candles
        case dataAgeMinutes = "data_age_minutes"
        case stale, forecasts, direction, confidence
    }

    /// The forecast for a horizon label, or nil. An absent forecast (too
    /// little history, or the server changed which horizons it serves) is a
    /// real answer the view has to handle, not an indexing bug.
    func forecast(for label: String) -> PriceForecast? {
        forecasts.first { $0.label == label }
    }

    /// Symbol with the quote currency stripped, for compact display: "ETH"
    /// rather than "ETHUSDT".
    var baseSymbol: String {
        symbol.hasSuffix("USDT") ? String(symbol.dropLast(4)) : symbol
    }
}

/// A market doing something unusual right now, reported the moment it
/// happens rather than waiting for a full setup — one entry of
/// `/api/predict`'s `surges` array.
struct MarketSurge: Codable, Identifiable, Hashable {
    var id: String { symbol + kind }

    let symbol: String
    /// "volume" | "price" | "both"
    let kind: String
    let detail: String
    let magnitude: Double
    /// +1 / -1 / 0. A volume-only surge has no direction, hence 0 — do not
    /// read a zero here as "flat", it means the surge itself is directionless.
    let direction: Int
}

/// The full response from GET /api/predict.
struct PriceOutlookResponse: Codable {
    let generatedAt: Date
    let note: String
    let markets: [MarketOutlook]
    let surges: [MarketSurge]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case note, markets, surges
    }
}
