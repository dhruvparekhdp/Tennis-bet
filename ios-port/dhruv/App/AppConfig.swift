import Foundation

/// Where the backend lives, and the Keychain key the API token is stored
/// under. Not a settings struct read from a plist on purpose — there is
/// exactly one server this app talks to, and hiding that behind a config
/// layer before there is a second server to switch between would be
/// building for a requirement that does not exist yet.
enum AppConfig {
    /// The deployed backend. Change this one line if the Render URL moves;
    /// nothing else in the app should ever hardcode it.
    static let baseURL = URL(string: "https://tennis-bet-izye.onrender.com")!

    /// Keychain account name for the API bearer token (see KeychainStore).
    static let apiTokenKeychainKey = "api_auth_token"
}
