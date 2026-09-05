import SwiftUI

/// The app's actual root content — everything below the Face ID gate.
/// Kept separate from `AppLockView` so the tab structure can be previewed
/// and iterated on without re-authenticating on every SwiftUI preview
/// refresh.
struct MainTabView: View {
    let client: MarketDataClient
    let keychain: KeychainStore

    var body: some View {
        TabView {
            NavigationStack {
                PriceOutlookView(client: client)
            }
            .tabItem { Label("Outlook", systemImage: "chart.line.uptrend.xyaxis") }

            NavigationStack {
                ComingSoonView(title: "Signals", webEquivalent: "the Signals tab",
                              systemImage: "waveform.path.ecg")
            }
            .tabItem { Label("Signals", systemImage: "waveform.path.ecg") }

            NavigationStack {
                ComingSoonView(title: "Watchlist", webEquivalent: "the Watchlist tab",
                              systemImage: "star")
            }
            .tabItem { Label("Watchlist", systemImage: "star") }

            NavigationStack {
                ComingSoonView(title: "Accuracy", webEquivalent: "the Accuracy tab",
                              systemImage: "checkmark.seal")
            }
            .tabItem { Label("Accuracy", systemImage: "checkmark.seal") }

            NavigationStack {
                APITokenSettingsView(client: client, keychain: keychain)
                    .navigationTitle("Settings")
            }
            .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}

/// The app's true root: everything is behind Face ID / Touch ID / passcode
/// before it renders. Re-locks whenever the app leaves the foreground —
/// `scenePhase` is watched here rather than in `LockScreenView` itself
/// because "what happens when this app backgrounds" is a whole-app policy,
/// not something a single screen should decide on its own.
struct AppLockView: View {
    @State private var isUnlocked = false
    @Environment(\.scenePhase) private var scenePhase

    private let biometrics = BiometricAuthService()
    private let keychain = KeychainStore()
    private let client: MarketDataClient = LiveMarketDataClient(baseURL: AppConfig.baseURL)

    var body: some View {
        Group {
            if isUnlocked {
                MainTabView(client: client, keychain: keychain)
            } else {
                LockScreenView(biometrics: biometrics) {
                    isUnlocked = true
                }
            }
        }
        .onChange(of: scenePhase) { _, newPhase in
            // .background, not .inactive — .inactive fires during entirely
            // routine transitions (a system alert, the app switcher
            // appearing) that are not the user leaving the app, and
            // re-locking on every one of those would be constant friction
            // for no security benefit.
            if newPhase == .background {
                isUnlocked = false
            }
        }
    }
}

#Preview {
    AppLockView()
}
