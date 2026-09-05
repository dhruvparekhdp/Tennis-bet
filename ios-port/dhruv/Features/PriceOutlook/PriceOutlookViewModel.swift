import Foundation
import Observation

/// Polls GET /api/predict and holds the current picture for the view.
///
/// Owns its own refresh loop rather than relying on `.task` + `.onAppear`
/// alone, so it can be started once from RootView and keep polling across
/// tab switches within a session — matching the web dashboard's own 20-second
/// cadence (see the `setInterval(load, 20000)` in scheduler/health.py's
/// _PREDICT_HTML), which was tuned against how often the underlying data
/// actually changes; there is no reason for the iOS client to poll on a
/// different rhythm than the web one already validated.
@Observable
@MainActor
final class PriceOutlookViewModel {
    private(set) var response: PriceOutlookResponse?
    private(set) var isLoading = false
    private(set) var lastError: MarketDataError?
    private(set) var lastUpdated: Date?

    private let client: MarketDataClient
    private let refreshInterval: Duration
    private var pollTask: Task<Void, Never>?

    init(client: MarketDataClient, refreshInterval: Duration = .seconds(20)) {
        self.client = client
        self.refreshInterval = refreshInterval
    }

    /// Markets sorted so the ones actually saying something (a real lean, or
    /// a stale warning) surface first — matching the instinct behind the
    /// web page's surge banner: the screen should lead with what deserves
    /// attention, not with whatever order the server happened to return.
    var sortedMarkets: [MarketOutlook] {
        guard let markets = response?.markets else { return [] }
        return markets.sorted { a, b in
            if a.stale != b.stale { return b.stale }  // stale entries last
            let aLean = abs(a.forecasts.first?.lean ?? 0)
            let bLean = abs(b.forecasts.first?.lean ?? 0)
            return aLean > bLean
        }
    }

    var surges: [MarketSurge] {
        response?.surges ?? []
    }

    /// One-shot fetch, callable directly (pull-to-refresh) independent of
    /// the polling loop — the two must not fight each other over
    /// `isLoading`, so this is the single place that mutates it.
    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            response = try await client.fetchPriceOutlook()
            lastError = nil
            lastUpdated = Date()
        } catch let error as MarketDataError {
            lastError = error
        } catch {
            lastError = .transport(error.localizedDescription)
        }
    }

    /// Starts the poll loop if it is not already running. Idempotent on
    /// purpose — a view's `.task` modifier can re-run this on every
    /// re-appearance without spawning a second concurrent poller.
    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                await self.refresh()
                try? await Task.sleep(for: self.refreshInterval)
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    deinit {
        pollTask?.cancel()
    }
}
