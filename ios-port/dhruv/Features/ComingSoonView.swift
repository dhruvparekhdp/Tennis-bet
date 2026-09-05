import SwiftUI

/// Placeholder for a tab that mirrors a web dashboard screen not yet ported.
/// Named honestly rather than left blank — an empty tab looks broken, this
/// looks intentional and says what it will eventually show.
struct ComingSoonView: View {
    let title: String
    let webEquivalent: String
    let systemImage: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: systemImage)
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.title3.bold())
            Text("Not ported yet — see \(webEquivalent) on the web dashboard.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .navigationTitle(title)
    }
}

#Preview {
    NavigationStack {
        ComingSoonView(title: "Signals", webEquivalent: "/#crypto",
                      systemImage: "waveform.path.ecg")
    }
}
