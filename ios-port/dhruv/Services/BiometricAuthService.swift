import Foundation
import LocalAuthentication

/// Gates the app behind Face ID (or Touch ID, or the device passcode).
///
/// Uses `.deviceOwnerAuthentication`, not `.deviceOwnerAuthenticationWithBiometrics`
/// — biometrics with a passcode fallback, not biometrics only. Three reasons,
/// not one:
///
///   1. Right after a reboot, iOS has no cached biometric data at all and
///      REQUIRES the passcode first no matter which policy is requested —
///      `.WithBiometrics` in that state fails outright with no way in.
///   2. A user with no Face ID / Touch ID enrolled (or a face covered, wet
///      fingers, a broken sensor) still owns the device and still deserves a
///      way into their own app.
///   3. This mirrors exactly how iOS itself unlocks the phone. An app-level
///      lock that is stricter than the OS lock protecting it is friction
///      without added security — the passcode already gates everything.
///
/// A fresh `LAContext` per attempt, not a reused one: `LAContext` caches a
/// successful evaluation for a few minutes and will silently succeed a
/// second `evaluatePolicy` call on the SAME instance without re-prompting,
/// which is convenient for chaining several Keychain reads after one
/// unlock but wrong for the app-lock screen itself — re-opening the app
/// after backgrounding it must always re-check, not ride a cached grant.
@Observable
final class BiometricAuthService {
    enum Kind {
        case faceID, touchID, passcodeOnly, unavailable
    }

    private(set) var lastError: String?

    /// What the device is actually capable of, so the lock screen can show
    /// "Unlock with Face ID" rather than a generic label that's wrong on a
    /// Touch ID device — or explain plainly when neither is enrolled.
    func availableKind() -> Kind {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            return .unavailable
        }
        switch context.biometryType {
        case .faceID: return .faceID
        case .touchID: return .touchID
        default: return .passcodeOnly
        }
    }

    /// Prompts and returns whether the device owner was confirmed. Never
    /// throws outward — every failure (cancel, lockout, no hardware, wrong
    /// passcode too many times) collapses to `false` plus a human-readable
    /// `lastError`, because the lock screen has exactly one thing to decide
    /// either way: show the app, or don't.
    @MainActor
    func authenticate(reason: String) async -> Bool {
        let context = LAContext()
        // No touch-ID/Face-ID re-use across screens; see the type doc.
        context.touchIDAuthenticationAllowableReuseDuration = 0

        do {
            let success = try await context.evaluatePolicy(
                .deviceOwnerAuthentication, localizedReason: reason)
            lastError = nil
            return success
        } catch let error as LAError {
            lastError = Self.message(for: error)
            return false
        } catch {
            lastError = error.localizedDescription
            return false
        }
    }

    private static func message(for error: LAError) -> String {
        switch error.code {
        case .userCancel, .appCancel, .systemCancel:
            return "Cancelled"
        case .userFallback:
            return "Fallback requested"
        case .biometryNotAvailable:
            return "Face ID / Touch ID is not available on this device"
        case .biometryNotEnrolled:
            return "No Face ID / Touch ID is set up"
        case .biometryLockout:
            return "Too many failed attempts — enter your passcode"
        case .passcodeNotSet:
            return "Set a device passcode to use this app"
        default:
            return error.localizedDescription
        }
    }
}
