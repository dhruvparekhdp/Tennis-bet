import Foundation

/// What this app needs from the backend, as a protocol — so the view models
/// can be built and tested against a fake without a network in the loop, and
/// so a future data source swap (a different host, a mock for SwiftUI
/// previews) is a new conformance rather than a rewrite of every call site.
///
/// Deliberately narrow for this phase: Price Outlook is the first feature
/// being ported (see PORT_NOTES.md §10.4 — it is stateless, always has
/// something to show, and has no staleness problem the way a signal does),
/// so this protocol covers exactly what that screen and the Face ID token
/// flow need. Watchlist editing, signals, paper trading and the rest come
/// with their own screens later and extend this protocol then rather than
/// being stubbed out now with methods nothing calls yet.
protocol MarketDataClient: Sendable {
    /// GET /api/predict — a band and a lean for every watchlist market, plus
    /// any active surges. No auth required: this is read-only market data,
    /// the same reasoning the server itself uses to leave it open (see
    /// scheduler/security.py).
    func fetchPriceOutlook() async throws -> PriceOutlookResponse

    /// POST /api/auth/verify — does this token work? Used exactly once, when
    /// a token is entered for the first time, before it is written to
    /// Keychain behind Face ID. This endpoint carries a much tighter
    /// server-side rate limit than everything else (its whole job is
    /// accepting attempts at the shared secret), so this method must never
    /// be called in a retry loop — a single explicit user action only.
    func verifyToken(_ token: String) async throws -> Bool
}

/// The live implementation, talking to the existing Python backend over
/// HTTPS. Not talking to Binance directly: the confluence voting, the cost
/// model, the forecast maths and every hard-won fix documented in this
/// project's git history already live server-side, tested, in one place.
/// Reimplementing that a second time in Swift would not make the app more
/// capable, it would make two implementations that drift.
actor LiveMarketDataClient: MarketDataClient {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    /// Retry budget for transient failures (transport errors, 5xx, 429).
    /// Three attempts total, not one endless loop — a fourth failure is
    /// treated as "the server is actually down right now" and surfaced to
    /// the UI rather than hidden behind a retry the user cannot see.
    private let maxAttempts = 3

    init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .useDefaultKeys  // CodingKeys are explicit throughout
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = Self.parseServerTimestamp(raw) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unrecognised timestamp format: \(raw)")
        }
        self.decoder = decoder
    }

    /// The server emits Python's `datetime.isoformat()` on a UTC-aware
    /// value: "2026-09-05T14:28:14.081550+00:00" — SIX-digit microseconds
    /// and a "+00:00" offset, not the three-digit milliseconds
    /// `ISO8601DateFormatter` expects. That mismatch is exactly the class of
    /// bug PORT_NOTES.md flags twice on the Python side: a parser that looks
    /// like it handles a timestamp but silently mis-reads its precision. A
    /// plain `ISO8601DateFormatter().date(from:)` call here would fail (or
    /// worse, on some OS versions, truncate silently) on every response.
    /// `DateFormatter` with an explicit six-`S` fractional-second pattern
    /// handles the real format; a fallback pattern with no fractional
    /// seconds covers the rare case where Python emits none (isoformat()
    /// omits the fraction entirely when microseconds happen to be zero).
    static func parseServerTimestamp(_ raw: String) -> Date? {
        for pattern in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSSxxxxx", "yyyy-MM-dd'T'HH:mm:ssxxxxx"] {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.dateFormat = pattern
            if let date = formatter.date(from: raw) {
                return date
            }
        }
        return nil
    }

    func fetchPriceOutlook() async throws -> PriceOutlookResponse {
        try await request(path: "/api/predict", method: "GET", token: nil)
    }

    func verifyToken(_ token: String) async throws -> Bool {
        do {
            let _: VerifyResponse = try await request(
                path: "/api/auth/verify", method: "POST", token: token, attempts: 1)
            return true
        } catch MarketDataError.unauthorized {
            return false
        }
        // Anything else (transport, decode, rate-limited, other server
        // error) is a real failure distinct from "the token is wrong", and
        // is left to propagate so the caller can tell the two apart.
    }

    private struct VerifyResponse: Decodable { let ok: Bool }

    // MARK: - Request plumbing

    private func request<T: Decodable>(
        path: String, method: String, token: String?, attempts: Int? = nil
    ) async throws -> T {
        let url = baseURL.appendingPathComponent(path)
        var lastError: MarketDataError = .transport("no attempt made")
        let budget = attempts ?? maxAttempts

        for attempt in 0..<budget {
            var urlRequest = URLRequest(url: url)
            urlRequest.httpMethod = method
            if let token {
                urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }

            do {
                let (data, response) = try await session.data(for: urlRequest)
                guard let http = response as? HTTPURLResponse else {
                    lastError = .transport("no HTTP response")
                    continue
                }

                switch http.statusCode {
                case 200..<300:
                    do {
                        return try decoder.decode(T.self, from: data)
                    } catch {
                        // A decode failure is a shape mismatch, not a
                        // transient fault — retrying the exact same request
                        // will fail the exact same way, so this does not
                        // loop; it is thrown immediately.
                        throw MarketDataError.decode(String(describing: error))
                    }

                case 401:
                    throw MarketDataError.unauthorized

                case 429:
                    let retryAfter = http.value(forHTTPHeaderField: "Retry-After")
                        .flatMap(Double.init)
                    lastError = .rateLimited(retryAfterSeconds: retryAfter)
                    if attempt < budget - 1 {
                        // Honour the SERVER's stated wait, not a guessed
                        // backoff — the server's rate limiter (see
                        // scheduler/security.py) always sets this header on
                        // a 429, and retrying sooner than it asked would
                        // extend, not shorten, the time until the next
                        // request succeeds.
                        try await Task.sleep(for: .seconds(retryAfter ?? Self.backoff(attempt)))
                        continue
                    }
                    throw lastError

                case 500..<600:
                    let body = String(data: data, encoding: .utf8) ?? ""
                    lastError = .server(status: http.statusCode, body: body)
                    if attempt < budget - 1 {
                        try await Task.sleep(for: .seconds(Self.backoff(attempt)))
                        continue
                    }
                    throw lastError

                default:
                    let body = String(data: data, encoding: .utf8) ?? ""
                    throw MarketDataError.server(status: http.statusCode, body: body)
                }
            } catch let error as MarketDataError {
                throw error
            } catch is CancellationError {
                // The view holding this call was torn down mid-request or
                // mid-backoff-sleep — propagate the cancellation as-is
                // rather than relabelling it a transport failure and
                // potentially spending another retry on a call nobody is
                // waiting for any more.
                throw CancellationError()
            } catch {
                lastError = .transport(error.localizedDescription)
                if attempt < budget - 1 {
                    try await Task.sleep(for: .seconds(Self.backoff(attempt)))
                    continue
                }
                throw lastError
            }
        }
        throw lastError
    }

    /// Exponential backoff with jitter: 1s, 2s, 4s as a base, each
    /// randomised ±30%. Jitter matters even for a single client — without
    /// it, a brief server hiccup causes every in-flight retry (across this
    /// app's own several concurrent widgets, e.g. Price Outlook polling
    /// alongside a background refresh) to retry in lockstep and re-create
    /// the same burst that triggered the backoff in the first place.
    private static func backoff(_ attempt: Int) -> Double {
        let base = pow(2.0, Double(attempt))
        let jitter = Double.random(in: 0.7...1.3)
        return base * jitter
    }
}
