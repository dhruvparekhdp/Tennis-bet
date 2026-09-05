import Foundation
import Testing
@testable import dhruv

/// Decodes a REAL response captured from a running instance of the backend
/// (GET /api/predict) rather than a hand-typed guess at its shape. This is
/// the specific defence PORT_NOTES.md §10.2 asks for: two of the worst bugs
/// in the Python project's history were parsers that silently accepted the
/// WRONG shape and rendered it as if it were correct (a 24-hour volume
/// figure stamped onto every one-minute bar; 15-minute candles loaded into a
/// 1-minute series). A hand-written fixture can encode the same wrong
/// assumption the code has; a captured one cannot, because it was produced
/// by the server's actual serialisation path, not by guessing what that
/// path probably does.
struct PriceOutlookDecodingTests {

    /// Captured 2026-09-05 from a local run of scheduler/health.py's
    /// _api_predict with seeded market data (see PORT_NOTES.md for how to
    /// reproduce). Trimmed to three markets; the shape is what matters, not
    /// the count — a fourth market would exercise nothing a third does not.
    static let fixture = #"""
{
  "generated_at": "2026-09-05T14:28:14.081550+00:00",
  "note": "The band is roughly one standard deviation of this market's own recent range projected over the horizon: price should land inside it about two times in three. The centre is shifted from spot by where the evidence leans, capped at a third of the band — no indicator set earns more than that.",
  "markets": [
    {
      "symbol": "BCHUSDT",
      "price": 252.74595765988542,
      "change_24h_pct": 2.17,
      "rsi": 50.7,
      "atr_pct": 0.1097,
      "relative_volume": 0.7,
      "candles": 240,
      "data_age_minutes": 1.1,
      "stale": false,
      "forecasts": [
        {
          "horizon_minutes": 60,
          "label": "1h",
          "centre": 252.79304022,
          "low": 250.63574116,
          "high": 254.95033928,
          "change_pct": 0.019,
          "band_pct": 0.854,
          "direction": "flat",
          "lean": 0.062,
          "confidence": 0.567
        },
        {
          "horizon_minutes": 240,
          "label": "4h",
          "centre": 252.84012279,
          "low": 248.52552467,
          "high": 257.15472091,
          "change_pct": 0.037,
          "band_pct": 1.707,
          "direction": "flat",
          "lean": 0.062,
          "confidence": 0.567
        },
        {
          "horizon_minutes": 1440,
          "label": "24h",
          "centre": 252.97661418,
          "low": 242.40805034,
          "high": 263.54517802,
          "change_pct": 0.091,
          "band_pct": 4.181,
          "direction": "flat",
          "lean": 0.062,
          "confidence": 0.567
        }
      ],
      "direction": "flat",
      "confidence": 0.567
    },
    {
      "symbol": "BNBUSDT",
      "price": 676.0871130649653,
      "change_24h_pct": -2.11,
      "rsi": 51.4,
      "atr_pct": 0.0835,
      "relative_volume": 1.09,
      "candles": 240,
      "data_age_minutes": 1.1,
      "stale": false,
      "forecasts": [
        {
          "horizon_minutes": 60,
          "label": "1h",
          "centre": 675.90181219,
          "low": 671.55776235,
          "high": 680.24586203,
          "change_pct": -0.027,
          "band_pct": 0.643,
          "direction": "down",
          "lean": -0.122,
          "confidence": 0.549
        },
        {
          "horizon_minutes": 240,
          "label": "4h",
          "centre": 675.71651131,
          "low": 667.02841163,
          "high": 684.40461099,
          "change_pct": -0.055,
          "band_pct": 1.285,
          "direction": "down",
          "lean": -0.122,
          "confidence": 0.549
        },
        {
          "horizon_minutes": 1440,
          "label": "24h",
          "centre": 675.17932787,
          "low": 653.89791683,
          "high": 696.46073892,
          "change_pct": -0.134,
          "band_pct": 3.148,
          "direction": "down",
          "lean": -0.122,
          "confidence": 0.549
        }
      ],
      "direction": "down",
      "confidence": 0.549
    },
    {
      "symbol": "BTCUSDT",
      "price": 78227.84633207023,
      "change_24h_pct": -0.83,
      "rsi": 32.1,
      "atr_pct": 0.1017,
      "relative_volume": 1.52,
      "candles": 240,
      "data_age_minutes": 1.1,
      "stale": false,
      "forecasts": [
        {
          "horizon_minutes": 60,
          "label": "1h",
          "centre": 78239.49179181,
          "low": 77679.10876689,
          "high": 78799.87481672,
          "change_pct": 0.015,
          "band_pct": 0.716,
          "direction": "flat",
          "lean": 0.059,
          "confidence": 0.574
        },
        {
          "horizon_minutes": 240,
          "label": "4h",
          "centre": 78251.13725154,
          "low": 77130.37120171,
          "high": 79371.90330137,
          "change_pct": 0.03,
          "band_pct": 1.433,
          "direction": "flat",
          "lean": 0.059,
          "confidence": 0.574
        },
        {
          "horizon_minutes": 1440,
          "label": "24h",
          "centre": 78284.89720042,
          "low": 75539.59225731,
          "high": 81030.20214353,
          "change_pct": 0.073,
          "band_pct": 3.509,
          "direction": "flat",
          "lean": 0.059,
          "confidence": 0.574
        }
      ],
      "direction": "flat",
      "confidence": 0.574
    }
  ],
  "surges": [
    {
      "symbol": "SOLUSDT",
      "kind": "volume",
      "detail": "volume 3.6x its recent average",
      "magnitude": 1.21,
      "direction": 0
    }
  ]
}

"""#

    @Test func decodesTheFullResponseShape() throws {
        let data = Data(Self.fixture.utf8)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = LiveMarketDataClient.parseServerTimestamp(raw) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "bad timestamp: \(raw)")
        }

        let response = try decoder.decode(PriceOutlookResponse.self, from: data)

        #expect(response.markets.count == 3)
        #expect(response.surges.count == 1)
        #expect(!response.note.isEmpty)
    }

    @Test func everyMarketCarriesThreeHorizons() throws {
        let response = try Self.decode()
        for market in response.markets {
            #expect(market.forecasts.count == 3)
            #expect(market.forecast(for: "1h") != nil)
            #expect(market.forecast(for: "4h") != nil)
            #expect(market.forecast(for: "24h") != nil)
            // A horizon the server never sent must not silently succeed.
            #expect(market.forecast(for: "7d") == nil)
        }
    }

    @Test func bandsWidenWithTheHorizon() throws {
        // Mirrors the server-side invariant in analysis/forecast.py: a
        // longer horizon always produces a WIDER band, never narrower, since
        // it is the same per-bar range projected further by sqrt(time).
        let response = try Self.decode()
        for market in response.markets {
            let widths = market.forecasts.map { $0.high - $0.low }
            #expect(widths == widths.sorted())
        }
    }

    @Test func directionFallsBackToFlatForUnknownValues() throws {
        // A vocabulary change on the server (a new direction the app does
        // not know about yet) must degrade this one field, not fail the
        // whole decode and blank the screen.
        let json = #"{"horizon_minutes":60,"label":"1h","centre":1,"low":0,"high":2,"change_pct":0,"band_pct":1,"direction":"sideways","lean":0,"confidence":0.5}"#
        let forecast = try JSONDecoder().decode(PriceForecast.self, from: Data(json.utf8))
        #expect(forecast.direction == .flat)
    }

    @Test func theCapturedTimestampParsesToTheRightInstant() {
        // "2026-09-05T14:28:14.081550+00:00" — Python's isoformat() on a
        // UTC-aware datetime: six-digit microseconds, "+00:00" not "Z".
        // ISO8601DateFormatter alone does not parse this; see
        // LiveMarketDataClient.parseServerTimestamp for why.
        let date = LiveMarketDataClient.parseServerTimestamp(
            "2026-09-05T14:28:14.081550+00:00")
        #expect(date != nil)

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let components = calendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second], from: date!)
        #expect(components.year == 2026)
        #expect(components.month == 9)
        #expect(components.day == 5)
        #expect(components.hour == 14)
        #expect(components.minute == 28)
        #expect(components.second == 14)
    }

    @Test func aTimestampWithNoFractionalSecondsStillParses() {
        // isoformat() omits the fraction entirely when microseconds happen
        // to be exactly zero — a real, if rare, shape the parser must not
        // choke on.
        #expect(LiveMarketDataClient.parseServerTimestamp(
            "2026-09-05T14:28:14+00:00") != nil)
    }

    @Test func garbageTimestampsReturnNilRatherThanCrashing() {
        #expect(LiveMarketDataClient.parseServerTimestamp("not a date") == nil)
        #expect(LiveMarketDataClient.parseServerTimestamp("") == nil)
    }

    private static func decode() throws -> PriceOutlookResponse {
        let data = Data(fixture.utf8)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            return LiveMarketDataClient.parseServerTimestamp(raw) ?? Date()
        }
        return try decoder.decode(PriceOutlookResponse.self, from: data)
    }
}
