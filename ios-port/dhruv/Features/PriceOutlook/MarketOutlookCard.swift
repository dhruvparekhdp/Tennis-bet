import SwiftUI

/// One market's card: symbol, price, the reading that feeds every forecast
/// (RSI / ATR / volume), and the three horizon bands underneath.
struct MarketOutlookCard: View {
    let market: MarketOutlook

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            readingRow
            if market.stale {
                staleNotice
            }
            ForEach(market.forecasts) { forecast in
                ForecastBandView(forecast: forecast, currentPrice: market.price)
            }
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(market.baseSymbol)
                    .font(.headline)
                Text(market.symbol)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(ForecastBandView.priceFormat(market.price))
                    .font(.headline.monospacedDigit())
                Text(change24hText)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(market.change24hPct >= 0 ? .green : .red)
            }
        }
    }

    private var change24hText: String {
        let sign = market.change24hPct > 0 ? "+" : ""
        return "\(sign)\(String(format: "%.2f", market.change24hPct))% (24h)"
    }

    private var readingRow: some View {
        HStack(spacing: 14) {
            statLabel("RSI", String(format: "%.0f", market.rsi))
            statLabel("ATR", market.atrPct.map { String(format: "%.3f%%/min", $0) } ?? "—")
            statLabel("Vol", market.relativeVolume.map { String(format: "×%.2f", $0) } ?? "—")
            Spacer()
            leanPill
        }
        .font(.caption)
    }

    private func statLabel(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).foregroundStyle(.secondary)
            Text(value).monospacedDigit()
        }
    }

    private var leanPill: some View {
        let direction = market.direction ?? .flat
        let (text, color): (String, Color) = switch direction {
        case .up: ("UP", .green)
        case .down: ("DOWN", .red)
        case .flat: ("FLAT", .secondary)
        }
        return Text(text)
            .font(.caption2.bold())
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }

    private var staleNotice: some View {
        Label {
            Text("Data is \(market.dataAgeMinutes.map { String(format: "%.0f", $0) } ?? "?") min old")
        } icon: {
            Image(systemName: "exclamationmark.triangle.fill")
        }
        .font(.caption2)
        .foregroundStyle(.orange)
    }
}

#Preview {
    MarketOutlookCard(market: MarketOutlook(
        symbol: "ETHUSDT", price: 2453.81, change24hPct: 2.61, rsi: 50.7,
        atrPct: 0.162, relativeVolume: 1.61, candles: 240, dataAgeMinutes: 1.1,
        stale: false,
        forecasts: [
            PriceForecast(horizonMinutes: 60, label: "1h", centre: 2508.45,
                         low: 2476.79, high: 2540.10, changePct: 0.03, bandPct: 0.85,
                         direction: .flat, lean: 0.06, confidence: 0.53),
            PriceForecast(horizonMinutes: 240, label: "4h", centre: 2509.21,
                         low: 2445.90, high: 2572.52, changePct: 0.06, bandPct: 1.71,
                         direction: .flat, lean: 0.06, confidence: 0.53),
        ],
        direction: .flat, confidence: 0.53))
    .padding()
}
