import SwiftUI

/// Where every watchlist market is likely to be in 1, 4 and 24 hours —
/// ported first (see PORT_NOTES.md §10.4) because it is stateless, has no
/// cooldown or contradiction bookkeeping to replicate, and always has
/// something to show: the signal engine's honest answer is usually "no
/// trade right now", which leaves ITS screen empty. This one never is.
struct PriceOutlookView: View {
    @State private var viewModel: PriceOutlookViewModel

    init(client: MarketDataClient) {
        _viewModel = State(initialValue: PriceOutlookViewModel(client: client))
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                if !viewModel.surges.isEmpty {
                    surgesSection
                }
                if let error = viewModel.lastError, viewModel.response == nil {
                    errorState(error)
                } else if viewModel.response == nil && viewModel.isLoading {
                    loadingState
                } else if viewModel.sortedMarkets.isEmpty {
                    emptyState
                } else {
                    ForEach(viewModel.sortedMarkets) { market in
                        MarketOutlookCard(market: market)
                    }
                }
            }
            .padding(16)
        }
        .refreshable { await viewModel.refresh() }
        .navigationTitle("Price Outlook")
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                if let updated = viewModel.lastUpdated {
                    Text(updated, style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .task {
            viewModel.startPolling()
        }
        .onDisappear {
            viewModel.stopPolling()
        }
    }

    private var surgesSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(viewModel.surges) { surge in
                HStack(spacing: 8) {
                    Text(surge.kind.uppercased())
                        .font(.caption2.bold())
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(surgeColor(surge).opacity(0.18), in: Capsule())
                        .foregroundStyle(surgeColor(surge))
                    Text("\(surge.symbol) — \(surge.detail)")
                        .font(.caption)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(surgeColor(surge).opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func surgeColor(_ surge: MarketSurge) -> Color {
        switch surge.direction {
        case 1: return .green
        case -1: return .red
        default: return .orange
        }
    }

    private var loadingState: some View {
        VStack(spacing: 8) {
            ProgressView()
            Text("Loading the outlook…")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "chart.line.flattrend.xyaxis")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
            Text("No markets with enough history yet")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    private func errorState(_ error: MarketDataError) -> some View {
        VStack(spacing: 10) {
            Image(systemName: "wifi.exclamationmark")
                .font(.largeTitle)
                .foregroundStyle(.orange)
            Text(error.localizedDescription)
                .font(.subheadline)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button("Retry") {
                Task { await viewModel.refresh() }
            }
            .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }
}

#Preview {
    NavigationStack {
        PriceOutlookView(client: LiveMarketDataClient(baseURL: AppConfig.baseURL))
    }
}
