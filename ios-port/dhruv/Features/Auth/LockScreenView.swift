import SwiftUI

/// The gate shown before any app content. Requests Face ID / Touch ID /
/// passcode on appear, and again whenever the app returns from the
/// background — the caller (RootView) is responsible for flipping
/// `isUnlocked` back to false on backgrounding, since that decision depends
/// on `ScenePhase`, which belongs to the view that owns the whole app's
/// lifecycle, not to this screen.
struct LockScreenView: View {
    let biometrics: BiometricAuthService
    let onUnlock: () -> Void

    @State private var isAuthenticating = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: iconName)
                .font(.system(size: 56))
                .foregroundStyle(.secondary)

            Text("Locked")
                .font(.title2.bold())

            if let errorMessage {
                Text(errorMessage)
                    .font(.subheadline)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }

            Button(action: attemptUnlock) {
                Label(buttonLabel, systemImage: iconName)
                    .frame(maxWidth: 220)
            }
            .buttonStyle(.borderedProminent)
            .disabled(isAuthenticating)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
        .task {
            // Prompt immediately on first appearance — the user should not
            // have to find and tap a button just to see the lock screen do
            // the one thing it exists to do.
            attemptUnlock()
        }
    }

    private var iconName: String {
        switch biometrics.availableKind() {
        case .faceID: return "faceid"
        case .touchID: return "touchid"
        case .passcodeOnly, .unavailable: return "lock.fill"
        }
    }

    private var buttonLabel: String {
        switch biometrics.availableKind() {
        case .faceID: return "Unlock with Face ID"
        case .touchID: return "Unlock with Touch ID"
        default: return "Unlock"
        }
    }

    private func attemptUnlock() {
        guard !isAuthenticating else { return }
        isAuthenticating = true
        errorMessage = nil
        Task {
            let ok = await biometrics.authenticate(
                reason: "Unlock to view your watchlist and signals")
            isAuthenticating = false
            if ok {
                onUnlock()
            } else if let message = biometrics.lastError, message != "Cancelled" {
                // A user-initiated cancel is not an error worth showing —
                // they can just tap Unlock again. Anything else (lockout,
                // no passcode set, hardware unavailable) is worth surfacing
                // because it explains why the button keeps not working.
                errorMessage = message
            }
        }
    }
}

#Preview {
    LockScreenView(biometrics: BiometricAuthService(), onUnlock: {})
}
