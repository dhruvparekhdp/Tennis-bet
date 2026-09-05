import SwiftUI

/// Where the one shared API secret gets entered, verified against the
/// server, and stored. This is the closest thing this app has to a "login
/// screen" — but it is a single shared token for a single operator, not an
/// account system, so there is no username, no session, nothing to expire
/// on its own. See scheduler/security.py on the backend for why that is the
/// right amount of machinery here.
///
/// The token is verified with the server BEFORE it is written to Keychain —
/// storing an unverified string and discovering it is wrong the next time a
/// watchlist edit fails, with no clear error, would be worse than asking the
/// user to wait one round trip up front.
struct APITokenSettingsView: View {
    let client: MarketDataClient
    let keychain: KeychainStore

    @State private var input: String = ""
    @State private var isVerifying = false
    @State private var status: Status = .unknown

    enum Status: Equatable {
        case unknown, verifying, valid, invalid, error(String)
    }

    var body: some View {
        Form {
            Section {
                SecureField("API token", text: $input)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .disabled(isVerifying)

                Button("Verify & Save") {
                    verifyAndSave()
                }
                .disabled(input.isEmpty || isVerifying)
            } header: {
                Text("API Token")
            } footer: {
                Text("This is the API_AUTH_TOKEN set on the server. It's needed for "
                     + "actions that change something — Price Outlook and other "
                     + "readings work without it.")
            }

            statusSection

            if keychain.load(forKey: AppConfig.apiTokenKeychainKey) != nil {
                Section {
                    Button("Remove stored token", role: .destructive) {
                        try? keychain.delete(forKey: AppConfig.apiTokenKeychainKey)
                        status = .unknown
                    }
                }
            }
        }
        .navigationTitle("API Token")
    }

    @ViewBuilder
    private var statusSection: some View {
        switch status {
        case .unknown:
            EmptyView()
        case .verifying:
            Section {
                HStack {
                    ProgressView()
                    Text("Checking with the server…")
                }
            }
        case .valid:
            Section {
                Label("Saved and verified", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            }
        case .invalid:
            Section {
                Label("That token was rejected by the server", systemImage: "xmark.circle.fill")
                    .foregroundStyle(.red)
            }
        case .error(let message):
            Section {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        }
    }

    private func verifyAndSave() {
        let token = input
        isVerifying = true
        status = .verifying
        Task {
            do {
                let ok = try await client.verifyToken(token)
                if ok {
                    try keychain.saveBiometric(token, forKey: AppConfig.apiTokenKeychainKey)
                    status = .valid
                    input = ""
                } else {
                    status = .invalid
                }
            } catch {
                let marketError = error as? MarketDataError
                status = .error(marketError?.localizedDescription ?? error.localizedDescription)
            }
            isVerifying = false
        }
    }
}

#Preview {
    NavigationStack {
        APITokenSettingsView(
            client: LiveMarketDataClient(baseURL: AppConfig.baseURL),
            keychain: KeychainStore())
    }
}
