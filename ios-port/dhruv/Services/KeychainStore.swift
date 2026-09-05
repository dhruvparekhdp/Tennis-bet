import Foundation
import Security

/// Thin wrapper over the Keychain Services C API — the plan's Phase 1 asks
/// for "secrets in Keychain only, never in source or git", and this is the
/// one place that touches `Security.framework` so nothing else in the app
/// needs to know the API's calling convention.
///
/// Two access levels, not one:
///
///   `save`/`load` — `kSecAttrAccessibleAfterFirstUnlockThisDevice`. Readable
///   once the device has been unlocked since boot, so a background refresh
///   can use the stored API token without the app being in the foreground.
///   Never leaves the device (`ThisDevice`) — no iCloud Keychain sync, so a
///   token issued for one phone cannot silently reappear on another.
///
///   `saveBiometric`/`loadBiometric` — the same accessibility PLUS a
///   `SecAccessControl` requiring Face ID / Touch ID / passcode at the
///   moment of the read. This is what actually gates the stored API token:
///   the app-level Face ID lock screen controls whether the app's UI is
///   visible at all, but a background task or a jailbroken keychain dump
///   would otherwise still be able to read a plain `save`d secret. Wrapping
///   the token itself in a biometry-required entry means the secret is
///   protected even if the UI gate were ever bypassed.
struct KeychainStore {
    enum KeychainError: Error {
        case unexpectedStatus(OSStatus)
        case unableToEncode
    }

    private let service: String

    /// `service` scopes entries so this type can be reused for more than one
    /// secret (the API token today; a future per-market alert threshold or
    /// similar tomorrow) without them colliding on the same Keychain item.
    init(service: String = "dp.dhruv.secrets") {
        self.service = service
    }

    // MARK: - Standard (no biometric prompt)

    func save(_ value: String, forKey key: String) throws {
        guard let data = value.data(using: .utf8) else { throw KeychainError.unableToEncode }
        try delete(forKey: key)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDevice,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.unexpectedStatus(status) }
    }

    func load(forKey key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    func delete(forKey key: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        let status = SecItemDelete(query as CFDictionary)
        // errSecItemNotFound is not a failure here — deleting something
        // that is already gone is the caller's desired end state either way.
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(status)
        }
    }

    // MARK: - Biometry-required

    /// Stores `value` behind a `SecAccessControl` that demands Face ID,
    /// Touch ID, or the device passcode at read time. `.userPresence` rather
    /// than `.biometryCurrentSet` deliberately: the stricter option
    /// invalidates the item the moment ANY enrolled biometry changes (a new
    /// face added, a finger re-enrolled), which would silently lock the
    /// user out of their own stored token for a reason that has nothing to
    /// do with a real security event. Falling back to the passcode is the
    /// same trade-off iOS itself makes for unlocking the phone.
    func saveBiometric(_ value: String, forKey key: String) throws {
        guard let data = value.data(using: .utf8) else { throw KeychainError.unableToEncode }
        try delete(forKey: key)

        var accessError: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil,
            kSecAttrAccessibleAfterFirstUnlockThisDevice,
            .userPresence,
            &accessError
        ) else {
            throw KeychainError.unexpectedStatus(errSecParam)
        }

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessControl as String: access,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.unexpectedStatus(status) }
    }

    /// Reading this triggers the system Face ID / Touch ID / passcode
    /// sheet — there is no way to suppress that prompt and still honour the
    /// access control, by design. `nil` covers both "not present" and "user
    /// declined the prompt", which the caller cannot and should not try to
    /// tell apart: either way, there is no token to use.
    func loadBiometric(forKey key: String) -> String? {
        // No kSecUseOperationPrompt — it is deprecated in favour of passing
        // a pre-configured LAContext via kSecUseAuthenticationContext, and
        // the system's own default prompt text is perfectly adequate here;
        // not worth carrying a deprecated API for a custom string.
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
