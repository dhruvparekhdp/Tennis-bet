import Foundation
import Testing
@testable import dhruv

/// A scripted MarketDataClient — no network, no timing dependency. Each
/// call to `fetchPriceOutlook` pops the next queued result, so a test can
/// script exactly the sequence of successes/failures it needs to exercise.
actor FakeMarketDataClient: MarketDataClient {
    enum Result {
        case success(PriceOutlookResponse)
        case failure(MarketDataError)
    }

    private var queue: [Result]
    private(set) var verifyCalls: [String] = []
    private let verifyResult: Bool

    init(queue: [Result], verifyResult: Bool = true) {
        self.queue = queue
        self.verifyResult = verifyResult
    }

    func fetchPriceOutlook() async throws -> PriceOutlookResponse {
        guard !queue.isEmpty else {
            throw MarketDataError.transport("fixture exhausted")
        }
        switch queue.removeFirst() {
        case .success(let response): return response
        case .failure(let error): throw error
        }
    }

    func verifyToken(_ token: String) async throws -> Bool {
        verifyCalls.append(token)
        return verifyResult
    }
}

@MainActor
struct PriceOutlookViewModelTests {

    static func market(
        symbol: String, stale: Bool = false, lean: Double = 0
    ) -> MarketOutlook {
        MarketOutlook(
            symbol: symbol, price: 100, change24hPct: 0, rsi: 50,
            atrPct: 0.1, relativeVolume: 1, candles: 200, dataAgeMinutes: 1,
            stale: stale,
            forecasts: [PriceForecast(
                horizonMinutes: 60, label: "1h", centre: 100, low: 99, high: 101,
                changePct: 0, bandPct: 1, direction: .flat, lean: lean,
                confidence: 0.5)],
            direction: .flat, confidence: 0.5)
    }

    static func response(_ markets: [MarketOutlook], surges: [MarketSurge] = []) -> PriceOutlookResponse {
        PriceOutlookResponse(generatedAt: Date(), note: "test", markets: markets, surges: surges)
    }

    @Test func aSuccessfulRefreshPopulatesTheResponse() async {
        let client = FakeMarketDataClient(queue: [
            .success(response([market(symbol: "ETHUSDT")]))
        ])
        let viewModel = PriceOutlookViewModel(client: client)

        await viewModel.refresh()

        #expect(viewModel.response?.markets.count == 1)
        #expect(viewModel.lastError == nil)
        #expect(viewModel.lastUpdated != nil)
    }

    @Test func aFailedRefreshSetsLastErrorAndKeepsThePreviousResponse() async {
        let client = FakeMarketDataClient(queue: [
            .success(response([market(symbol: "ETHUSDT")])),
            .failure(.transport("offline")),
        ])
        let viewModel = PriceOutlookViewModel(client: client)

        await viewModel.refresh()
        await viewModel.refresh()

        // The screen must not go blank on a transient failure — the last
        // good picture stays up, with the error surfaced alongside it, not
        // instead of it.
        #expect(viewModel.response?.markets.count == 1)
        #expect(viewModel.lastError == .transport("offline"))
    }

    @Test func staleMarketsAlwaysSortLast() async {
        let client = FakeMarketDataClient(queue: [
            .success(response([
                market(symbol: "STALEUSDT", stale: true, lean: 0.9),
                market(symbol: "FRESHUSDT", stale: false, lean: 0.01),
            ]))
        ])
        let viewModel = PriceOutlookViewModel(client: client)
        await viewModel.refresh()

        // Note: STALEUSDT has the stronger lean but must still sort AFTER
        // the fresh market — a confident-looking read on stale data is
        // worth less than a weak read on live data, and the ordering must
        // reflect that rather than ranking on lean strength alone.
        #expect(viewModel.sortedMarkets.map(\.symbol) == ["FRESHUSDT", "STALEUSDT"])
    }

    @Test func amongFreshMarketsStrongerLeanSortsFirst() async {
        let client = FakeMarketDataClient(queue: [
            .success(response([
                market(symbol: "WEAKUSDT", lean: 0.05),
                market(symbol: "STRONGUSDT", lean: -0.4),
            ]))
        ])
        let viewModel = PriceOutlookViewModel(client: client)
        await viewModel.refresh()

        #expect(viewModel.sortedMarkets.map(\.symbol) == ["STRONGUSDT", "WEAKUSDT"])
    }

    @Test func surgesPassThroughUnmodified() async {
        let surge = MarketSurge(symbol: "SOLUSDT", kind: "volume",
                                detail: "volume 3.6x its recent average",
                                magnitude: 1.2, direction: 0)
        let client = FakeMarketDataClient(queue: [.success(response([], surges: [surge]))])
        let viewModel = PriceOutlookViewModel(client: client)
        await viewModel.refresh()

        #expect(viewModel.surges == [surge])
    }

    @Test func verifyTokenForwardsToTheClientExactlyOnce() async throws {
        let client = FakeMarketDataClient(queue: [], verifyResult: true)
        let ok = try await client.verifyToken("abc123")
        #expect(ok)
        #expect(await client.verifyCalls == ["abc123"])
    }
}
