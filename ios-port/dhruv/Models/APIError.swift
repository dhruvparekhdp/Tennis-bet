import Foundation

/// Every way a call to the backend can fail, named specifically enough that
/// the UI can say something more useful than "something went wrong".
///
/// Deliberately covers transport, decode, auth and rate-limit shapes as
/// DISTINCT cases rather than one generic `.networkError(Error)` — a decode
/// failure means the server changed shape and the app needs updating; a 429
/// means back off and retry later; a 401 means the stored token is gone or
/// wrong. Those need different UI responses and must not be collapsed into
/// one undifferentiated failure the caller has to re-inspect.
enum MarketDataError: Error, Equatable {
    /// The request never got a response — offline, DNS, timeout, TLS.
    case transport(String)

    /// A response came back but the body did not match what this app
    /// expects. Carries the underlying decoding error's description, since
    /// "the server sent something unexpected" without detail is exactly the
    /// kind of silent-shape-drift bug PORT_NOTES.md calls out twice on the
    /// Python side (a rolling 24h volume read as a 1-minute figure, 15-minute
    /// candles read as 1-minute ones — both parsed "successfully" into the
    /// wrong thing). A decode error here should be loud, not swallowed.
    case decode(String)

    /// HTTP 401 — missing or wrong bearer token. Distinct from `.transport`
    /// so the UI can specifically prompt for the token rather than showing a
    /// generic "couldn't reach the server" message when the server was
    /// reached fine and simply refused the request.
    case unauthorized

    /// HTTP 429, with the server's own Retry-After if it sent one. The rate
    /// limiter on the Python side (scheduler/security.py) always sends this
    /// header on a 429; honouring it rather than retrying immediately is
    /// what keeps a retry loop from making the limit worse.
    case rateLimited(retryAfterSeconds: Double?)

    /// Any other non-2xx status, with the code and whatever body came back —
    /// kept as a string rather than parsed, since an error body's shape is
    /// not a contract the way a success body is.
    case server(status: Int, body: String)

    var localizedDescription: String {
        switch self {
        case .transport(let detail):
            return "Couldn't reach the server (\(detail))"
        case .decode(let detail):
            return "The server sent something this app doesn't understand yet (\(detail))"
        case .unauthorized:
            return "That token isn't accepted — check it in Settings"
        case .rateLimited(let retryAfter):
            if let retryAfter {
                return "Too many requests — try again in \(Int(retryAfter.rounded()))s"
            }
            return "Too many requests — try again shortly"
        case .server(let status, let body):
            return "Server error \(status)\(body.isEmpty ? "" : ": \(body)")"
        }
    }
}
