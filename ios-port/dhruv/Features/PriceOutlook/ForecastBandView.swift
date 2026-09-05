import SwiftUI

/// One horizon's forecast, rendered as a band with the current price marked
/// inside it — the mobile equivalent of the `.bar` element on the web
/// dashboard's /predict page (scheduler/health.py's _PREDICT_HTML), which
/// draws exactly this: a low-to-high range, coloured by direction, with a
/// vertical tick at the live price.
///
/// A band, not a single number, because nobody can say a market will be at
/// an exact price at a future time — what can be defended is the range it
/// usually travels and which way the evidence leans. Rendering a bare number
/// here would misrepresent the forecast's own honesty about its precision.
struct ForecastBandView: View {
    let forecast: PriceForecast
    let currentPrice: Double

    private var tint: Color {
        switch forecast.direction {
        case .up: return .green
        case .down: return .red
        case .flat: return .secondary
        }
    }

    /// Where the current price sits within [low, high], as 0...1 — clamped,
    /// since the live price can drift outside a band computed moments ago
    /// and the marker must not be drawn off the visible track.
    private var priceFraction: Double {
        let span = forecast.high - forecast.low
        guard span > 0 else { return 0.5 }
        return min(1, max(0, (currentPrice - forecast.low) / span))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(forecast.label)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(formattedCentre)
                    .font(.caption.monospacedDigit())
                Text(formattedChange)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(tint)
            }

            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(tint.opacity(0.18))
                        .frame(height: 6)

                    Circle()
                        .fill(Color.primary)
                        .frame(width: 8, height: 8)
                        .offset(x: geo.size.width * priceFraction - 4)
                }
            }
            .frame(height: 8)

            HStack {
                Text(formattedBound(forecast.low))
                Spacer()
                Text(formattedBound(forecast.high))
            }
            .font(.caption2.monospacedDigit())
            .foregroundStyle(.secondary)
        }
    }

    private var formattedCentre: String {
        Self.priceFormat(forecast.centre)
    }

    private func formattedBound(_ value: Double) -> String {
        Self.priceFormat(value)
    }

    private var formattedChange: String {
        let sign = forecast.changePct > 0 ? "+" : ""
        return "\(sign)\(String(format: "%.2f", forecast.changePct))%"
    }

    /// Matches the web dashboard's own `fmtPrice` precision rule (see
    /// scheduler/health.py): more decimals for sub-$1 assets like XRP than
    /// for BTC, so both remain readable at a glance instead of either
    /// truncating XRP to "1" or drowning BTC in trailing zeros.
    static func priceFormat(_ value: Double) -> String {
        let magnitude = abs(value)
        let decimals = magnitude >= 1000 ? 2 : (magnitude >= 1 ? 2 : 4)
        return value.formatted(.number.precision(.fractionLength(decimals)))
    }
}

#Preview {
    ForecastBandView(
        forecast: PriceForecast(
            horizonMinutes: 240, label: "4h", centre: 2509.21, low: 2445.90,
            high: 2572.52, changePct: 0.08, bandPct: 1.7, direction: .flat,
            lean: 0.062, confidence: 0.567),
        currentPrice: 2453.81
    )
    .padding()
}
