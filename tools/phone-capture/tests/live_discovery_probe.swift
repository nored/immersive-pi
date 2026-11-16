import Foundation

@main
struct DiscoveryProbe {
    @MainActor static func main() throws {
        let client=LiveAnalysisClient()
        client.discover()
        let deadline=Date().addingTimeInterval(10)
        while client.address.isEmpty && Date()<deadline {
            _ = RunLoop.current.run(mode:.default,before:Date().addingTimeInterval(0.1))
        }
        guard !client.address.isEmpty else { throw NSError(domain:"Discovery",code:1,userInfo:[NSLocalizedDescriptionKey:"Bonjour discovery timed out"]) }
        print("Native Bonjour discovery: \(client.address)")
    }
}
